"""services.exporter.drag_outlook — 30 日低軌阻力展望（國際對標：KSWC／NICT 的 LEO 阻力產品）。

回答作業單位的前瞻問題：**未來 30 天，各高度帶的衛星預期掉多少高度、
其中多少是地磁活動造成的、這個數字有多可信。**

作法（全部沿用既有、已驗證的元件）：
  驅動量   SWPC 45 日預報的逐日 F10.7 與 Ap（取前 N 天）
  密度     MSIS 2.1（日均 Ap 模式——預報沒有 3 小時 ap 歷史）
  衰減     近圓軌道能量法封閉解（orbit_drag.propagate），逐 3 小時積分
  誤差帶   密度模型 ±1σ，σ 依當日 Ap 取福衛七號實測校準值（orbit_drag.calibration）
  地磁貢獻 同一 F10.7、地磁寧靜（Ap=4）再跑一次，兩者之差即地磁活動所致

**這不是地磁暴預報。** 預報擂台實測（docs/forecast_verification.md 結果三）：
45 日 Ap 預報在 3 天以後與氣候平均同一量級、F10.7 以持續性最佳。
所以這張表給的是「依目前已知的太陽活動水準，平常會掉多少」的包絡，
不會預先告訴你哪天有 G3——那要看 3 日預報與即時監測。
誤差帶只含密度模型誤差，不含驅動量預報誤差；後者見密度頁的沿跡差分析。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from swx_core import SwxStore

FORECAST_SOURCE = "swpc_45day_forecast"
DEFAULT_DAYS = 30
DEFAULT_ALTS_KM = (350.0, 450.0, 550.0)
STEP = "3h"
QUIET_AP = 4.0
#: 「預報較活躍」的日 Ap 門檻：Ap 15 約當 Kp 3（unsettled），僅作標註，不是告警
ACTIVE_AP = 15.0


class NoForecastData(RuntimeError):
    """資料層沒有可用的 45 日預報（API 據此回 503，而非把其他錯誤也說成缺資料）。"""


def forecast_drivers(store: SwxStore, *, now: pd.Timestamp,
                     days: int = DEFAULT_DAYS) -> tuple[pd.DataFrame, pd.Timestamp]:
    """SWPC 45 日預報的逐日驅動量（今天起 `days` 天）與該批的取得時刻。

    source_id 必須進查詢、不能事後過濾：同一天的 F10.7 預測可能由 27 日展望
    在去重時勝出，事後再篩會讓那幾天整天消失（tools/alongtrack_drivers 實測過）。
    f107a（81 日中心平均）預報端沒有，沿用最後一筆觀測——30 天內它的變化遠小於
    f107 本身。
    """
    day0 = now.floor("D")
    fc = store.query(["F107_OBS", "AP_AVG"], source_id=FORECAST_SOURCE,
                     start=day0, end=day0 + timedelta(days=days, microseconds=-1))
    if fc.empty:
        raise NoForecastData(
            "資料層沒有 SWPC 45 日預報；請先執行 "
            f"`python -m services.ingest.run --source {FORECAST_SOURCE}`")
    issued = pd.Timestamp(fc["ingest_time"].max())
    wide = (fc.pivot_table(index=fc["valid_time"].dt.floor("D"), columns="param_code",
                           values="value", aggfunc="last")
            .rename(columns={"F107_OBS": "f107", "AP_AVG": "ap"}).sort_index())
    for col in ("f107", "ap"):
        if col not in wide:
            raise NoForecastData(f"45 日預報缺 {col}")
    c81 = store.series("F107_OBS_C81", observed_only=True)
    wide["f107a"] = float(c81.iloc[-1]) if not c81.empty else float(wide["f107"].mean())
    return wide[["f107", "f107a", "ap"]].ffill(), issued


def build(store: SwxStore | None = None, *, now: datetime | pd.Timestamp | None = None,
          days: int = DEFAULT_DAYS, alts_km=DEFAULT_ALTS_KM, bc: float | None = None,
          lat: float = 0.0, lon: float = 0.0) -> dict:
    """30 日阻力展望。回傳 {metadata, drivers, daily, summary}（皆可 JSON 化）。"""
    from orbit_drag import DEFAULT_BC, propagate
    from orbit_drag.calibration import sigma_log

    store = store or SwxStore()
    now = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    now = now.tz_convert("UTC") if now.tzinfo else now.tz_localize("UTC")
    bc = float(bc if bc is not None else DEFAULT_BC)

    sw, issued = forecast_drivers(store, now=now, days=days)
    calm = sw.assign(ap=QUIET_AP)
    epochs = pd.date_range(sw.index.min(), sw.index.max() + timedelta(days=1), freq=STEP)

    ap_ep = sw["ap"].reindex(epochs.floor("D")).to_numpy(dtype=float)
    sig_pairs = [sigma_log(float(a)) for a in ap_ep]
    sig = np.array([p[0] for p in sig_pairs])
    calibrated = bool(sig_pairs) and all(p[1] for p in sig_pairs)

    daily_rows, summary = [], []
    for alt0 in alts_km:
        kw = dict(bc=bc, lat=lat, lon=lon)
        mid = propagate(epochs, alt0, sw, **kw)
        hi = propagate(epochs, alt0, sw, rho_scale=np.exp(sig), **kw)
        lo = propagate(epochs, alt0, sw, rho_scale=np.exp(-sig), **kw)
        quiet = propagate(epochs, alt0, calm, **kw)

        ratio = (mid["rho"] / quiet["rho"]).groupby(epochs.floor("D")).mean()
        drop = alt0 - mid["alt_km"]
        day_end = drop.groupby(epochs.floor("D")).last()
        for d in sw.index:
            daily_rows.append({
                "date_utc": d.strftime("%Y-%m-%d"), "alt0_km": alt0,
                "storm_ratio": round(float(ratio.get(d, np.nan)), 3),
                "cum_drop_km": round(float(day_end.get(d, np.nan)), 3),
            })
        loss = float(alt0 - mid["alt_km"].iloc[-1])
        loss_quiet = float(alt0 - quiet["alt_km"].iloc[-1])
        summary.append({
            "alt0_km": alt0,
            "alt_loss_km": round(loss, 2),
            "alt_loss_low_km": round(float(alt0 - lo["alt_km"].iloc[-1]), 2),
            "alt_loss_high_km": round(float(alt0 - hi["alt_km"].iloc[-1]), 2),
            "alt_loss_quiet_km": round(loss_quiet, 2),
            # 地磁貢獻占比：預報 Ap 相對寧靜基準多掉的那一段
            "geomag_share": round((loss - loss_quiet) / loss, 3) if loss > 0 else None,
            "peak_storm_ratio": round(float(ratio.max()), 3),
            "peak_date_utc": ratio.idxmax().strftime("%Y-%m-%d"),
        })

    drivers = [{"date_utc": d.strftime("%Y-%m-%d"), "f107": float(r["f107"]),
                "ap": float(r["ap"]), "active": bool(r["ap"] >= ACTIVE_AP)}
               for d, r in sw.iterrows()]
    return {
        "metadata": {
            "product": "leo_drag_outlook",
            "schema_version": "1.0",
            "issued_utc": issued.isoformat(),
            "generated_utc": now.isoformat(),
            "days": len(sw),
            "drivers_source": f"NOAA SWPC 45-day forecast ({FORECAST_SOURCE}), daily F10.7 & Ap",
            "model": "MSIS 2.1 (daily-Ap mode) + near-circular energy-method decay",
            "ballistic_coefficient_m2_kg": bc,
            "evaluated_at": {"lat": lat, "lon": lon},
            "quiet_baseline": f"same F10.7, Ap={QUIET_AP:g}",
            "uncertainty": "density-model ±1σ only (log space, per-day Ap); excludes driver-forecast error",
            "uncertainty_calibrated_by_observation": calibrated,
            "active_ap_threshold": ACTIVE_AP,
            "caveats": [
                "Not a storm forecast: SWPC 45-day Ap is no better than climatology beyond ~3 days "
                "(forecast arena, docs/forecast_verification.md).",
                "Altitude loss scales linearly with the ballistic coefficient, which is an input.",
                "Density sampled at a fixed reference point; storm-time ratios vary ~15% with location.",
            ],
        },
        "drivers": drivers,
        "daily": daily_rows,
        "summary": summary,
    }
