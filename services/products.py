"""services.products — 產品層：API 與儀表板共用的唯一產品入口（架構書 §4、§16.2 R1）。

為何存在：在此之前，API 與儀表板各自 import 規則引擎與匯出器、各自組參數。
同一個產品有兩條呼叫路徑，只改一邊就會出現「儀表板與 API 說法不同」——
例如 CSSI 匯出在兩處各自組 build_frame／write_text 的參數，事件卡頁則把
同一段規則回放算了兩次（其中一次未快取）。

規則：
  · 展示層（apps/）與介接層（services/api）只透過本模組取得 nowcast、
    規則評估、事件卡與匯出產品；不直接 import `services.risk_engine`、
    `services.exporter`。由 `tests/test_products_boundary.py` 守住。
  · 本模組只做**組裝**，不含領域邏輯：分級在 risk_engine、格式在 exporter。
  · 回傳 pandas／dict 等中性型別；序列化（JSON、Streamlit 元件）留給呼叫端。

作業版前端若改為純 API client，本模組即是 API 的實作層，不需再搬一次。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from swx_core import SwxStore
from swx_core.flare import flux_to_class, r_scale
from swx_core.schema import OBSERVED_TYPES

from services.exporter import drag_correction, stk_spaceweather
from services.exporter import drag_outlook as _drag_outlook
from services.risk_engine.engine import Episode, Rule, RiskEngine, load_rules
from services.risk_engine.eventcard import EventStore, build_event_cards

__all__ = [
    "nowcast", "evaluate", "rule_status", "event_cards", "stored_event_cards",
    "event_store", "event_notifications", "rules", "risk_engine", "flare_list",
    "ForecastView", "forecast_view", "forecast_targets", "rsg_status",
    "forecast_snapshot_dir", "forecast_snapshot_view",
    "stk_cssi_frame", "stk_cssi_text", "stk_cssi_summary", "STK_MODE_SOURCE",
    "drag_correction_table", "drag_correction_metadata", "drag_outlook", "NoForecastData",
]

STK_MODE_SOURCE = stk_spaceweather.MODE_SOURCE


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── 規則與現況 ──────────────────────────────────────────────────────────
def risk_engine(store: SwxStore) -> RiskEngine:
    """給需要逐規則操作的工具（如門檻 what-if）。一般產品請用下列函式。"""
    return RiskEngine(store)


def rules() -> list[Rule]:
    return load_rules()


def nowcast(store: SwxStore) -> pd.DataFrame:
    """各網域當前任務風險等級。"""
    return RiskEngine(store).nowcast()


def evaluate(
    store: SwxStore,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    days: float | None = None,
    as_of: datetime | None = None,
) -> tuple[list[Episode], pd.DataFrame]:
    """規則回放：回傳 (事件段, 各規則狀態)。

    給 `days` 時以 `end`（預設現在）往回推；`start` 明示時優先。
    """
    if days is not None and start is None:
        end = end or _utcnow()
        start = end - timedelta(days=days)
    return RiskEngine(store).evaluate(start=start, end=end, as_of=as_of)


def rule_status(store: SwxStore, *, days: float = 2.0) -> pd.DataFrame:
    """近 N 日各規則狀態（含 `unavailable` 者）。"""
    _, status = evaluate(store, days=days)
    return status


# ── 事件卡 ──────────────────────────────────────────────────────────────
def event_cards(
    store: SwxStore,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    days: float | None = None,
    as_of: datetime | None = None,
    episodes: list[Episode] | None = None,
) -> list[dict]:
    """由規則回放**即時產生**的事件卡（尚未入作業狀態庫，status 為 draft）。

    已有事件段時可傳 `episodes` 省去重算。
    """
    eps = episodes if episodes is not None else evaluate(
        store, start=start, end=end, days=days, as_of=as_of)[0]
    if not eps:
        return []
    return [c.to_dict() for c in build_event_cards(eps, store=store)]


def event_store() -> EventStore:
    return EventStore()


def event_notifications(event_id: str, *, store: EventStore | None = None) -> dict | None:
    """某事件的通報送達狀態，附 §14 KPI 所需的兩段時間。

      issue_latency_s     事件起始 → 人工發布（§14「L3 以上 ≤30 分鐘」量的是這段）
      delivery_s（逐筆）  發布 → 該通道送達

    兩段分開：慢在值勤判斷還是慢在投遞，處置不同。
    回放產生的歷史事件，issue_latency 會是數月——那是回放的性質，不是延誤。
    """
    store = store or EventStore()
    card = store.latest(event_id)
    if card is None:
        return None
    msgs = store.notifications(event_id)
    issued_at = min((m["created_utc"] for m in msgs), default=None)
    onset = (card.get("timeline") or {}).get("onset_utc")
    latency = None
    if issued_at and onset:
        latency = round((pd.Timestamp(issued_at) - pd.Timestamp(onset)).total_seconds(), 1)
    counts: dict[str, int] = {}
    for m in msgs:
        counts[m["status"]] = counts.get(m["status"], 0) + 1
    return {
        "event_id": event_id,
        "status": card.get("status"),
        "mission_level": card.get("mission_level"),
        "onset_utc": onset,
        "issued_at_utc": issued_at,
        "issue_latency_s": latency,
        "summary": counts,
        "messages": msgs,
    }


def stored_event_cards(*, min_level: str | None = None,
                       store: EventStore | None = None) -> list[dict]:
    """作業狀態庫內各事件的最新版（status 以 DB 欄位為準）。"""
    return (store or EventStore()).list_events(min_level=min_level)


# ── 太陽閃焰 ────────────────────────────────────────────────────────────
def flare_list(store: SwxStore, *, start: datetime | None = None,
               end: datetime | None = None) -> list[dict]:
    """閃焰事件（新到舊），含 GOES 分級與 NOAA R 級。"""
    df = store.query("FLARE_PEAK", start=start, end=end)
    if df.empty:
        return []
    out = [
        {
            "peak_utc": row["valid_time"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "peak_flux_w_m2": row["value"],
            "flare_class": flux_to_class(row["value"]),
            "noaa_r_scale": r_scale(row["value"]),
            "source_id": row["source_id"],
        }
        for _, row in df.iterrows()
    ]
    out.sort(key=lambda d: d["peak_utc"], reverse=True)
    return out


# ── 預報 ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ForecastView:
    """某預報目標的最新一批預報與其起報錨點。"""

    spec: Any                    # services.forecast.features.TargetSpec
    own: pd.DataFrame            # 本系統最新一批預報（含機率列）
    reference: pd.DataFrame      # SWPC 官方預報（對照基準）
    observed: pd.DataFrame       # 目標參數的觀測列
    latest_observed: pd.Timestamp | None
    issued: pd.Timestamp | None  # 起報錨點：最新觀測對齊到目標格點


def forecast_targets() -> dict:
    from services.forecast.features import TARGETS

    return TARGETS


def forecast_view(store: SwxStore, target: str, *, as_of: datetime | None = None,
                  now: datetime | pd.Timestamp | None = None,
                  window_days: float = 30.0) -> ForecastView:
    """預報目標的資料視圖。API 的 /v1/forecast 與儀表板預報頁共用。

    兩個曾在其中一邊踩過、另一邊沒修的坑，集中在此：
      · 連續型目標（F10.7、Ap）沒有機率參數，`prob_code` 為 None，
        不可放進查詢清單。
      · 起報錨點須對齊格點：SWPC 估計 Kp 標在 00:05，不對齊會把 3 h
        還原成 2.92 h，技巧查表因此落空。
    視窗放寬到 30 天：資料落後時仍要看得到最後一批預報與它的錨點。
    """
    from services.forecast.skill import latest_forecast_batch

    spec = forecast_targets()[target]
    now = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    codes = [c for c in (spec.code, spec.prob_code) if c]
    win = dict(start=now - timedelta(days=window_days), end=now + timedelta(days=3),
               as_of=as_of)
    # 三種列分開查（source_id 在去重之前過濾）。資料層同一時刻觀測優先，
    # 若混在一次查詢裡再篩，過去時段的預報列會被觀測吃掉，對照線就斷了。
    own = latest_forecast_batch(store.query(codes, source_id="swx_forecast", **win))
    reference = store.query(codes, source_id="swpc_geomag_forecast", **win)
    observed = store.query(spec.code, observed_only=True, **win)
    latest = None if observed.empty else pd.Timestamp(observed["valid_time"].max())
    issued = None if latest is None else latest.floor(spec.grid)
    return ForecastView(spec, own, reference, observed, latest, issued)


# ── 預報快照（雲端無法自行產生預報時的退路）─────────────────────────────
def forecast_snapshot_dir() -> Path:
    """隨版本發布的預報快照目錄（`tools/make_forecast_snapshot.py` 產生）。

    為什麼需要：預報模型要多年的 Hp30／Kp 歷史才能訓練，雲端是空容器、
    只會擷取近幾天的資料，**永遠無法自己產生預報**（冷啟動實測：預報頁恆為
    「尚未產生」）。快照與示範快照分開存放——示範快照停在另一個時刻，
    混進去會讓 CSSI 快照比對與快照時刻都失真。
    """
    from swx_core.config import project_root

    return project_root() / "data" / "demo" / "forecast_snapshot"


def forecast_snapshot_view(target: str) -> tuple[ForecastView, dict] | None:
    """快照裡該目標的預報視圖與中繼資料；無快照或無此目標時回 None。

    視圖以快照的產生時刻為「現在」，畫面上的觀測脈絡、起報錨點、horizon
    都與當時一致——不拿今天的觀測去對一份幾週前的預報。
    """
    import json

    root = forecast_snapshot_dir()
    meta_path = root / "meta.json"
    if not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    entry = (meta.get("targets") or {}).get(target)
    if not entry:
        return None
    view = forecast_view(SwxStore(root), target, now=pd.Timestamp(entry["generated_utc"]))
    if view.own.empty:
        return None
    return view, {**entry, "note": meta.get("note")}


# ── R/S/G 狀態與 72 小時展望 ────────────────────────────────────────────
#: 「現在」容許的資料齡期。超過即回報無資料，不拿舊值當現況。
_CURRENT_MAX_AGE = {"R": timedelta(minutes=30), "S": timedelta(minutes=30),
                    "G": timedelta(hours=6)}


def _window_level(df: pd.DataFrame, codes: tuple[str, ...], fn,
                  lo: pd.Timestamp, hi: pd.Timestamp) -> dict:
    sub = df[df["param_code"].isin(codes) & (df["valid_time"] >= lo)
             & (df["valid_time"] <= hi)] if not df.empty else df
    if sub.empty:
        # 無資料與未達門檻是兩件事：前者 has_data=False、level=None
        return {"level": None, "value": None, "has_data": False}
    v = float(sub["value"].max())
    return {"level": fn(v), "value": v, "has_data": True}


def rsg_status(store: SwxStore, *, now: datetime | pd.Timestamp | None = None) -> dict:
    """NOAA R/S/G 三尺度的觀測狀態與 72 小時展望（參考 KSWC／SWPC 首頁呈現）。

    觀測（皆只取觀測列，不含預測）：
      past_48_24h  [now-48h, now-24h) 內最大值的等級
      past_24h     [now-24h, now] 內最大值的等級
      current      最新一筆；超過容許齡期（R/S 30 分鐘、G 6 小時）視為無資料
    R 取 GOES X 射線（XRAY_LONG 與閃焰峰值 FLARE_PEAK 取大者）、S 取 ≥10 MeV 質子、
    G 取 Kp。

    展望（NOAA SWPC 預報，逐 **UTC 日**，非「從現在起 24 小時」）：
      G   該日 3 小時 Kp 預報最大值換算的等級（swpc_geomag_forecast）
      R   R1–R2、R3 以上機率；S   S1 以上機率（swpc_3day_forecast）
    展望是 NOAA 的預報、不是本系統的；本系統自己的 Kp 預報另見 /v1/forecast。
    """
    from swx_core.scales import g_scale, r_scale, s_scale

    now = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    now = now.tz_convert("UTC") if now.tzinfo else now.tz_localize("UTC")
    obs = store.query(["XRAY_LONG", "FLARE_PEAK", "PROT10", "KP_3H"],
                      start=now - timedelta(hours=48), end=now, observed_only=True)

    scales = {"R": (("XRAY_LONG", "FLARE_PEAK"), r_scale),
              "S": (("PROT10",), s_scale),
              "G": (("KP_3H",), g_scale)}
    observed: dict[str, dict] = {}
    for key, (codes, fn) in scales.items():
        cur_codes = codes[:1]          # 「現在」只看連續量測，不看事後判定的閃焰峰值
        cur = obs[obs["param_code"].isin(cur_codes)] if not obs.empty else obs
        current = {"level": None, "value": None, "has_data": False, "valid_time": None}
        if not cur.empty:
            last = cur.sort_values("valid_time").iloc[-1]
            if now - pd.Timestamp(last["valid_time"]) <= _CURRENT_MAX_AGE[key]:
                current = {"level": fn(float(last["value"])), "value": float(last["value"]),
                           "has_data": True,
                           "valid_time": pd.Timestamp(last["valid_time"]).isoformat()}
        observed[key] = {
            "past_48_24h": _window_level(obs, codes, fn, now - timedelta(hours=48),
                                         now - timedelta(hours=24, microseconds=1)),
            "past_24h": _window_level(obs, codes, fn, now - timedelta(hours=24), now),
            "current": current,
        }

    # 展望的三天跟著「最新一份」NOAA 3 日預報走，而不是固定從今天排起：
    # 該產品於 22:00 UTC 發布、涵蓋**隔天起**三天。固定從今天排的話，
    # 發布後到 UTC 換日前，第一欄是已被取代的舊預報或空白，真正的第三天反被切掉。
    day0 = now.floor("D")
    probs_all = store.query(["R12_PROB", "R3_PROB", "S1_PROB"], source_id="swpc_3day_forecast",
                            start=day0, end=day0 + timedelta(days=4))
    first = day0
    if not probs_all.empty:
        newest = probs_all[probs_all["ingest_time"] == probs_all["ingest_time"].max()]
        first = pd.Timestamp(newest["valid_time"].min()).floor("D")
    days = [first + timedelta(days=i) for i in range(3)]
    probs = probs_all[(probs_all["valid_time"] >= first)
                      & (probs_all["valid_time"] < first + timedelta(days=3))] \
        if not probs_all.empty else probs_all
    kp_fc = store.query("KP_3H", source_id="swpc_geomag_forecast",
                        start=first, end=first + timedelta(days=3, microseconds=-1))

    def _prob(code: str, day: pd.Timestamp) -> float | None:
        if probs.empty:
            return None
        hit = probs[(probs["param_code"] == code) & (probs["valid_time"] == day)]
        return None if hit.empty else round(float(hit["value"].iloc[0]), 3)

    outlook = []
    for d in days:
        k = kp_fc[(kp_fc["valid_time"] >= d) & (kp_fc["valid_time"] < d + timedelta(days=1))] \
            if not kp_fc.empty else kp_fc
        kp_max = None if k.empty else round(float(k["value"].max()), 2)
        outlook.append({
            "date_utc": d.strftime("%Y-%m-%d"),
            "G": {"level": g_scale(kp_max), "kp_max": kp_max, "has_data": kp_max is not None},
            "R": {"r1_r2_prob": _prob("R12_PROB", d), "r3_plus_prob": _prob("R3_PROB", d)},
            "S": {"s1_plus_prob": _prob("S1_PROB", d)},
        })

    issued = [f["ingest_time"].max() for f in (kp_fc, probs) if not f.empty]
    return {
        "as_of_utc": now.isoformat(),
        "observed": observed,
        "outlook": outlook,
        "outlook_source": "NOAA SWPC 3-day forecast（逐 UTC 日）",
        "outlook_retrieved_utc": max(issued).isoformat() if issued else None,
        "scale_reference": "https://www.swpc.noaa.gov/noaa-scales-explanation",
    }


# ── 匯出 ────────────────────────────────────────────────────────────────
def stk_cssi_frame(store: SwxStore, *, start: datetime | None = None,
                   as_of: datetime | None = None,
                   mode: str = STK_MODE_SOURCE) -> pd.DataFrame:
    return stk_spaceweather.build_frame(store, start=start, as_of=as_of, mode=mode)


def stk_cssi_text(store: SwxStore, *, start: datetime | None = None,
                  as_of: datetime | None = None, mode: str = STK_MODE_SOURCE,
                  updated: datetime | None = None,
                  frame: pd.DataFrame | None = None) -> str:
    """STK/GMAT 用 CSSI 太空天氣檔全文。`updated` 預設為現在。"""
    from swx_core import cssi

    wide = frame if frame is not None else stk_cssi_frame(
        store, start=start, as_of=as_of, mode=mode)
    return cssi.write_text(wide, updated=updated or _utcnow())


def stk_cssi_summary(frame: pd.DataFrame) -> dict:
    return stk_spaceweather.summary(frame)


def drag_correction_table(store: SwxStore, *, start: datetime | None = None,
                          end: datetime | None = None,
                          as_of: datetime | None = None) -> pd.DataFrame:
    return drag_correction.build(store, start=start, end=end, as_of=as_of)


def drag_correction_metadata() -> dict:
    return drag_correction.product_metadata()


#: 彈道係數的合理範圍（m²/kg）：密實大型平臺約 0.003，立方衛星／大帆板約 0.03
BC_RANGE = (0.001, 0.05)
NoForecastData = _drag_outlook.NoForecastData


def drag_outlook(store: SwxStore, *, days: int = _drag_outlook.DEFAULT_DAYS,
                 bc: float | None = None, lat: float = 0.0, lon: float = 0.0,
                 now: datetime | None = None) -> dict:
    """30 日低軌阻力展望（SWPC 45 日預報驅動）。範圍檢查在此，API 與儀表板共用。"""
    if not 1 <= int(days) <= 45:
        raise ValueError("days 須在 1–45（SWPC 45 日預報的範圍）")
    if bc is not None and not BC_RANGE[0] <= float(bc) <= BC_RANGE[1]:
        raise ValueError(f"bc 須在 {BC_RANGE[0]}–{BC_RANGE[1]} m²/kg")
    if not (-90 <= lat <= 90 and -180 <= lon <= 360):
        raise ValueError("lat／lon 超出範圍")
    return _drag_outlook.build(store, now=now, days=int(days), bc=bc, lat=lat, lon=lon)
