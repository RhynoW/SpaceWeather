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


# ── 判定依據（inference）的完整列舉 ────────────────────────────────────
# 契約要求：這個欄位**永遠不得為 null**。null 對呼叫端有歧義——
# 可能是「直接觀測」「欄位缺失」「未計算」「未知」，而誤讀成觀測是最危險的一種。
INFERENCE_OBSERVED = "observed"        # 判定依據為直接觀測值
INFERENCE_MODELLED = "modelled"        # 依據為模型輸出或預報值（非當下觀測）
INFERENCE_PROXY = "proxy"              # 規則宣告為間接推估（如以 Dst 推 ΔH）
INFERENCE_UNAVAILABLE = "unavailable"  # 判定所需資料不存在——**不代表風險為零**

INFERENCE_VALUES = (
    INFERENCE_OBSERVED, INFERENCE_MODELLED, INFERENCE_PROXY, INFERENCE_UNAVAILABLE,
)

# 由模型或預報產生、而非直接觀測的參數。判定落在這些參數上時標 modelled。
MODEL_DERIVED_PARAMS = frozenset({
    "RHO_400", "RHO_RATIO",                          # MSIS 模型輸出
    "M_FLARE_PROB", "X_FLARE_PROB", "KP_STORM_PROB",  # 機率預報
    "KP_MAX_DAILY",                                   # 27 日展望（預報）
})


def classify_inference(param: str, rule_inference: str | None) -> str:
    """決定判定依據。規則自身的宣告優先於參數性質。"""
    if rule_inference:
        return rule_inference
    return INFERENCE_MODELLED if param in MODEL_DERIVED_PARAMS else INFERENCE_OBSERVED


def _domain_inference(episodes: list["Episode"], has_data: bool) -> str:
    """網域層級的判定依據。

    取該網域內**最弱**的一項而非最強：若任一分項是推估，整個網域的等級就
    不能宣稱為直接觀測所得。無資料時回 unavailable——這與「L0 沒事」不同。
    """
    if not has_data:
        return INFERENCE_UNAVAILABLE
    if not episodes:
        return INFERENCE_OBSERVED
    order = {INFERENCE_OBSERVED: 0, INFERENCE_MODELLED: 1,
             INFERENCE_PROXY: 2, INFERENCE_UNAVAILABLE: 3}
    return max((e.inference for e in episodes), key=lambda v: order.get(v, 0))


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
    inference: str = INFERENCE_OBSERVED
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
    def _series(self, param: str, start, end, as_of, region: str | None = None) -> pd.Series:
        """規則所需的單一序列。

        分區參數（如 e-GNSS 三個網的 I95）同一時刻有多個合法值。處理方式有二：

          規則宣告 region  取該分區的值。這是正確作法——本島的作業不該因為
                           澎湖網的尖峰而被判為嚴重。
          未宣告 region    取**同時刻的最大值**（最壞情況），而不是任意一列。
                           舊行為是 `duplicated(keep="last")`，等於讓 parquet
                           的列序決定判讀結果——不會報錯，只是安靜地看錯網。
        """
        s = self.store.series(param, start=start, end=end, as_of=as_of, grid_id=region)
        if s.index.has_duplicates:
            s = s.groupby(level=0).max()
        return s.sort_index()

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
        """回傳 (episodes, status)。status ∈ ok / partial / unavailable。

        三種狀態的意義不同，呼叫端必須分辨：
          ok           所有宣告的判據都有資料，「沒有告警」＝已確認未達門檻
          partial      部分判據有資料。仍會評估並可能發報，但**「沒有告警」
                       不等於確認平靜**——缺少的判據可能單獨觸發
          unavailable  一個判據都沒有，完全無法判定。**不代表風險為零**
        """
        series: dict[str, pd.Series] = {}
        for p in rule.params:
            series[p] = self._series(p, start, end, as_of, rule.region)

        needed = rule.requires_params or tuple(rule.params)
        missing = [p for p in needed
                   if series.get(p, pd.Series(dtype=float)).empty]

        if len(missing) == len(needed):
            # 一個判據都沒有 → 完全無法判定
            return [], "unavailable"

        status = "ok"
        if missing:
            # 部分判據有資料。**仍然評估**——手上已有的判據若超標就該發報，
            # 因為缺了另一個判據而整條規則噤聲，等於明明偵測得到卻不說。
            #
            # 但也不能報 ok：缺少的判據可能單獨觸發（本規則為 any/OR），
            # 所以「沒有告警」在此狀態下**不等於確認平靜**。
            # 這是「沒事」與「沒資料」之外的第三種情形，必須讓呼叫端分辨。
            status = "partial"

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
        return self._to_episodes(rule, active, aligned), status

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
                    inference=classify_inference(peak_param, rule.inference),
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

        # 已宣告於 params.yaml `impact_domains`、但 configs/rules 尚無任何規則的
        # 網域也必須列出來。少一列與「L0 綠燈」在畫面上難以分辨，讀者會把
        # 「還沒有判據」當成「查過沒事」——這與 P5「沒資料 ≠ 沒事」是同一條原則，
        # 只是缺口落在規則層而非資料層。此類網域的 criteria_total 為 0。
        rows = []
        for domain in sorted({r.domain for r in self.rules} | set(self.registry.impact_domains)):
            dom_eps = [e for e in episodes if e.domain == domain and e.end >= now - timedelta(hours=3)]
            dom_status = status[status["domain"] == domain]
            has_data = (dom_status["status"] == "ok").any()
            level = max_level([e.level for e in dom_eps]) if dom_eps else ("L0" if has_data else "—")

            # **判據涵蓋率必須一併回報。** has_data 只要任一規則可評估就為真，
            # 於是「代理查過了、實測沒查」會顯示成與「全部查過」相同的綠燈——
            # 綠燈代表已確認無異常，這個等號正是本系統最不該犯的錯。
            # 軌道預報網域尤其會常態落在此狀態：Kp 判據隨時可評估，
            # 而實測密度判據所依賴的精密定軌是手動來源。
            unevaluated = sorted(dom_status.loc[dom_status["status"] != "ok", "rule_id"])
            rows.append(
                {
                    "domain": domain,
                    "level": level,
                    "data_available": bool(has_data),
                    "active_rules": ",".join(sorted({e.rule_id for e in dom_eps})) or "—",
                    "criteria_total": int(len(dom_status)),
                    "criteria_ok": int(len(dom_status) - len(unevaluated)),
                    # 判據數為 0 時不算「已完整評估」——沒有規則不等於全部通過
                    "fully_evaluated": bool(len(dom_status) and not unevaluated),
                    "unevaluated_rules": ",".join(unevaluated),
                    # 網域層級的依據：無資料 → unavailable；全部推估 → proxy；
                    # 其餘取最弱的一項（proxy > modelled > observed），不回 null。
                    "inference": _domain_inference(dom_eps, has_data),
                }
            )
        return pd.DataFrame(rows)
