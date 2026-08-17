"""services.risk_engine.engine — L0–L4 分級規則引擎（架構書 §9.1）。

宣告式規則（configs/rules/*.yaml）→ 事件段（episode）。核心行為有四：

  駐留時間 dwell_h    條件必須**連續成立**達指定時數才觸發，避免單點雜訊觸發告警
  遲滯 hysteresis     觸發後需低於 clear_below 並持續 clear_dwell_h 才解除，
                      避免指標在門檻附近抖動造成告警洗版（架構書 P4）
  資料可用性          規則宣告 requires_params 但無資料時回報 unavailable，
                      而不是回報 L0——「沒資料」與「沒事」必須分開（架構書 P5）
  推估標記 inference  以間接指標推得的等級會標 proxy，事件卡據此標示可信度

門檻校準邏輯借自 Sat_TraingDataExtension 的 fusion_fpr_sweep.py／ids_domain_fpr.py：
先能重播歷史、算出「這組門檻會發幾次警報」，門檻才有辦法跟需求單位討論。
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from swx_core import SwxStore, config_dir, registry

_OPS = {
    ">=": operator.ge,
    ">": operator.gt,
    "<=": operator.le,
    "<": operator.lt,
    "==": operator.eq,
    "!=": operator.ne,
}

LEVEL_ORDER = ("L0", "L1", "L2", "L3", "L4")


def level_rank(level: str) -> int:
    return LEVEL_ORDER.index(level) if level in LEVEL_ORDER else 0


def max_level(levels) -> str:
    levels = [l for l in levels if l in LEVEL_ORDER]
    return max(levels, key=level_rank) if levels else "L0"


@dataclass
class Condition:
    param: str
    op: str
    value: float
    dwell_h: float = 0.0

    @classmethod
    def from_dict(cls, d: dict) -> "Condition":
        return cls(
            param=d["param"],
            op=d.get("op", ">="),
            value=float(d["value"]),
            dwell_h=float(d.get("dwell_h", 0.0)),
        )


@dataclass
class Rule:
    rule_id: str
    domain: str
    name: str
    level: str
    conditions: list[Condition]
    combine: str = "any"                     # any / all
    clear_below: float | dict[str, float] | None = None
    clear_dwell_h: float = 0.0
    scale_hint: str | None = None
    region: str | None = None
    impact: str = ""
    action: str = ""
    notify: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    requires_params: tuple[str, ...] = ()
    inference: str | None = None
    pre_alert: bool = False

    @property
    def params(self) -> list[str]:
        return sorted({c.param for c in self.conditions})


@dataclass
class Episode:
    """一段持續成立的規則命中。"""

    rule_id: str
    domain: str
    level: str
    start: pd.Timestamp
    end: pd.Timestamp
    peak_value: float
    peak_time: pd.Timestamp
    peak_param: str
    n_samples: int
    inference: str | None = None
    scale_hint: str | None = None
    rule: Rule | None = field(default=None, repr=False)

    @property
    def duration_h(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "domain": self.domain,
            "level": self.level,
            "start": self.start,
            "end": self.end,
            "duration_h": round(self.duration_h, 2),
            "peak_param": self.peak_param,
            "peak_value": self.peak_value,
            "peak_time": self.peak_time,
            "n_samples": self.n_samples,
            "inference": self.inference,
            "scale_hint": self.scale_hint,
        }


def load_rules(path: Path | None = None) -> list[Rule]:
    """載入 configs/rules/*.yaml 的所有規則。"""
    root = Path(path) if path else config_dir() / "rules"
    rules: list[Rule] = []
    for f in sorted(root.glob("*.yaml")):
        doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        domain = doc.get("domain", f.stem.upper())
        for key, is_pre in (("rules", False), ("pre_alert_rules", True)):
            for item in doc.get(key) or []:
                when = item.get("when", {})
                combine = "all" if "all" in when else "any"
                conds = [Condition.from_dict(c) for c in when.get(combine, [])]
                hyst = item.get("hysteresis") or {}
                rules.append(
                    Rule(
                        rule_id=item["id"],
                        domain=domain,
                        name=item.get("name", item["id"]),
                        level=item.get("level", "L1"),
                        conditions=conds,
                        combine=combine,
                        clear_below=hyst.get("clear_below"),
                        clear_dwell_h=float(hyst.get("clear_dwell_h", 0.0)),
                        scale_hint=item.get("scale_hint"),
                        region=item.get("region"),
                        impact=str(item.get("impact", "")).strip(),
                        action=str(item.get("action", "")).strip(),
                        notify=tuple(item.get("notify") or ()),
                        exclusions=tuple(item.get("exclusions") or ()),
                        requires_params=tuple(item.get("requires_params") or ()),
                        inference=item.get("inference"),
                        pre_alert=is_pre,
                    )
                )
    return rules


class RiskEngine:
    """規則引擎。"""

    def __init__(self, store: SwxStore | None = None, rules: list[Rule] | None = None) -> None:
        self.store = store or SwxStore()
        self.rules = rules if rules is not None else load_rules()
        self.registry = registry()

    # ── 序列準備 ────────────────────────────────────────────────────────
    def _series(self, param: str, start, end, as_of) -> pd.Series:
        s = self.store.series(param, start=start, end=end, as_of=as_of)
        return s[~s.index.duplicated(keep="last")].sort_index()

    @staticmethod
    def _dwell_mask(cond_true: pd.Series, dwell_h: float) -> pd.Series:
        """條件需連續成立達 dwell_h。dwell_h=0 時直接回傳原遮罩。"""
        if dwell_h <= 0 or cond_true.empty:
            return cond_true
        idx = cond_true.index
        vals = cond_true.to_numpy()
        out = np.zeros(len(vals), dtype=bool)
        # 由每個 True 點往回看 dwell 視窗內是否全為 True
        window = pd.Timedelta(hours=dwell_h)
        start_ptr = 0
        for i in range(len(vals)):
            if not vals[i]:
                start_ptr = i + 1
                continue
            # start_ptr..i 皆為 True，檢查時間跨度是否足夠
            if idx[i] - idx[start_ptr] >= window:
                out[i] = True
        return pd.Series(out, index=idx)

    # ── 主流程 ──────────────────────────────────────────────────────────
    def evaluate_rule(
        self,
        rule: Rule,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        as_of: datetime | None = None,
    ) -> tuple[list[Episode], str]:
        """回傳 (episodes, status)。status ∈ ok / unavailable。"""
        series: dict[str, pd.Series] = {}
        for p in rule.params:
            series[p] = self._series(p, start, end, as_of)

        needed = rule.requires_params or tuple(rule.params)
        if all(series.get(p, pd.Series(dtype=float)).empty for p in needed):
            return [], "unavailable"
        if any(series.get(p, pd.Series(dtype=float)).empty for p in rule.requires_params):
            return [], "unavailable"

        # 共同時間軸：所有被引用參數的取樣時刻聯集，前向填補
        timeline = pd.DatetimeIndex(sorted(set().union(*[set(s.index) for s in series.values()])))
        if timeline.empty:
            return [], "unavailable"
        aligned = {p: s.reindex(timeline).ffill() for p, s in series.items()}

        masks = []
        for c in rule.conditions:
            v = aligned.get(c.param)
            if v is None or v.dropna().empty:
                masks.append(pd.Series(False, index=timeline))
                continue
            raw = _OPS[c.op](v, c.value).fillna(False)
            masks.append(self._dwell_mask(raw, c.dwell_h))

        if not masks:
            return [], "unavailable"
        stacked = pd.concat(masks, axis=1)
        on = stacked.any(axis=1) if rule.combine == "any" else stacked.all(axis=1)

        active = self._apply_hysteresis(rule, on, aligned, timeline)
        return self._to_episodes(rule, active, aligned), "ok"

    @staticmethod
    def clear_thresholds(rule: Rule) -> dict[str, float]:
        """每個參數各自的解除門檻。

        多參數規則不可共用同一個純量門檻——Kp（0–9）與 Ap（0–400）量級差兩個數量級，
        混在一起取極值會讓解除條件永遠不成立，事件段就會無限延長。
        故：
          · clear_below 為對照表  → 直接採用
          · clear_below 為純量    → 套用到第一個條件的參數，其餘參數依「觸發值比例」換算
                                     （例：KP 5.0→4.0 為 0.8 倍，則 AP 40 → 32）
        """
        cb = rule.clear_below
        if cb is None or not rule.conditions:
            return {}
        if isinstance(cb, dict):
            return {str(k): float(v) for k, v in cb.items()}
        base = rule.conditions[0]
        ratio = float(cb) / base.value if base.value else 1.0
        return {
            c.param: (float(cb) if c.param == base.param else c.value * ratio)
            for c in rule.conditions
        }

    def _apply_hysteresis(
        self, rule: Rule, on: pd.Series, aligned: dict[str, pd.Series], timeline
    ) -> pd.Series:
        """觸發後維持啟動，直到**所有**參數都低於各自的解除門檻並持續 clear_dwell_h。"""
        thresholds = self.clear_thresholds(rule)
        if not thresholds:
            return on

        rising = rule.conditions[0].op in (">=", ">")
        still_high = pd.Series(False, index=timeline)
        for c in rule.conditions:
            v = aligned.get(c.param)
            thr = thresholds.get(c.param)
            if v is None or thr is None:
                continue
            high = (v >= thr) if rising else (v <= thr)
            still_high = still_high | high.fillna(False)

        clear_ok = self._dwell_mask(~still_high, rule.clear_dwell_h)
        out = np.zeros(len(timeline), dtype=bool)
        state = False
        on_arr, clear_arr = on.to_numpy(), clear_ok.to_numpy()
        for i in range(len(timeline)):
            if not state and on_arr[i]:
                state = True
            elif state and clear_arr[i]:
                state = False
            out[i] = state
        return pd.Series(out, index=timeline)

    @staticmethod
    def _to_episodes(rule: Rule, active: pd.Series, aligned: dict[str, pd.Series]) -> list[Episode]:
        if not active.any():
            return []
        groups = (active != active.shift(fill_value=False)).cumsum()[active]
        episodes: list[Episode] = []
        for _, idx in active[active].groupby(groups).groups.items():
            idx = pd.DatetimeIndex(idx)
            peak_param, peak_value, peak_time = "", float("nan"), idx[0]
            for c in rule.conditions:
                s = aligned.get(c.param)
                if s is None:
                    continue
                seg = s.reindex(idx).dropna()
                if seg.empty:
                    continue
                cand_time = seg.idxmax() if c.op in (">=", ">") else seg.idxmin()
                cand = float(seg.loc[cand_time])
                better = (
                    np.isnan(peak_value)
                    or (c.op in (">=", ">") and cand > peak_value)
                    or (c.op in ("<=", "<") and cand < peak_value)
                )
                if better:
                    peak_param, peak_value, peak_time = c.param, cand, cand_time
            episodes.append(
                Episode(
                    rule_id=rule.rule_id,
                    domain=rule.domain,
                    level=rule.level,
                    start=idx[0],
                    end=idx[-1],
                    peak_value=peak_value,
                    peak_time=peak_time,
                    peak_param=peak_param,
                    n_samples=len(idx),
                    inference=rule.inference,
                    scale_hint=rule.scale_hint,
                    rule=rule,
                )
            )
        return episodes

    def evaluate(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        as_of: datetime | None = None,
        domains: list[str] | None = None,
    ) -> tuple[list[Episode], pd.DataFrame]:
        """評估全部規則，回傳 (episodes, 規則狀態表)。"""
        episodes: list[Episode] = []
        status_rows = []
        for rule in self.rules:
            if domains and rule.domain not in domains:
                continue
            eps, status = self.evaluate_rule(rule, start=start, end=end, as_of=as_of)
            episodes.extend(eps)
            status_rows.append(
                {
                    "rule_id": rule.rule_id,
                    "domain": rule.domain,
                    "level": rule.level,
                    "status": status,
                    "params": ",".join(rule.params),
                    "n_episodes": len(eps),
                    "pre_alert": rule.pre_alert,
                }
            )
        episodes.sort(key=lambda e: (e.start, -level_rank(e.level)))
        return episodes, pd.DataFrame(status_rows)

    def nowcast(self, *, as_of: datetime | None = None, lookback_h: float = 48.0) -> pd.DataFrame:
        """各網域當前等級（架構書 §11「太空環境總覽」的紅綠燈資料源）。"""
        now = pd.Timestamp(as_of, tz="UTC") if as_of else pd.Timestamp.now(tz="UTC")
        start = now - timedelta(hours=lookback_h)
        episodes, status = self.evaluate(start=start, end=now, as_of=as_of)

        rows = []
        for domain in sorted({r.domain for r in self.rules}):
            dom_eps = [e for e in episodes if e.domain == domain and e.end >= now - timedelta(hours=3)]
            dom_status = status[status["domain"] == domain]
            has_data = (dom_status["status"] == "ok").any()
            level = max_level([e.level for e in dom_eps]) if dom_eps else ("L0" if has_data else "—")
            rows.append(
                {
                    "domain": domain,
                    "level": level,
                    "data_available": bool(has_data),
                    "active_rules": ",".join(sorted({e.rule_id for e in dom_eps})) or "—",
                    "inference": "proxy" if dom_eps and all(e.inference == "proxy" for e in dom_eps) else None,
                }
            )
        return pd.DataFrame(rows)
