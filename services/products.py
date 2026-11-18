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
from typing import Any

import pandas as pd

from swx_core import SwxStore
from swx_core.flare import flux_to_class, r_scale
from swx_core.schema import OBSERVED_TYPES

from services.exporter import drag_correction, stk_spaceweather
from services.risk_engine.engine import Episode, Rule, RiskEngine, load_rules
from services.risk_engine.eventcard import EventStore, build_event_cards

__all__ = [
    "nowcast", "evaluate", "rule_status", "event_cards", "stored_event_cards",
    "event_store", "event_notifications", "rules", "risk_engine", "flare_list",
    "ForecastView", "forecast_view", "forecast_targets",
    "stk_cssi_frame", "stk_cssi_text", "stk_cssi_summary", "STK_MODE_SOURCE",
    "drag_correction_table", "drag_correction_metadata",
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
    df = store.query(codes, start=now - timedelta(days=window_days),
                     end=now + timedelta(days=3), as_of=as_of)
    if df.empty:
        own = reference = observed = df
    else:
        own = latest_forecast_batch(df[df["source_id"] == "swx_forecast"])
        reference = df[df["source_id"] == "swpc_geomag_forecast"]
        observed = df[(df["param_code"] == spec.code)
                      & df["data_type"].isin(OBSERVED_TYPES)]
    latest = None if observed.empty else pd.Timestamp(observed["valid_time"].max())
    issued = None if latest is None else latest.floor(spec.grid)
    return ForecastView(spec, own, reference, observed, latest, issued)


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
