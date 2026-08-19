"""tools.density_obs_vs_model — 熱氣層密度：實測增強倍數 vs MSIS 模式。

回答一個問題：**專案交付的阻力修正因子，在事件期間錯多少。**

`services/exporter/drag_correction.py` 交付的修正因子取自 MSIS 的 `storm_ratio`
（同一 F10.7、地磁寧靜為基準）。福衛七號精密定軌反演的 `DRAG_ENHANCEMENT`
定義完全對應——同樣是「相對同一 F10.7 之寧靜期望值」的倍數，但**分子分母
皆為觀測量**。兩者相除即得 `RHO_RATIO`（觀測／模型），也就是修正因子本身的誤差。

    RHO_RATIO = DRAG_ENHANCEMENT / storm_ratio

實測 2024-04-29 至 05-20（含 Gannon），依觀測增強倍數分層：

    觀測 < 1.5   n=72   觀測 1.01   模式 1.09   比值 0.85
    1.5 – 2.5    n= 8   觀測 1.80   模式 1.21   比值 1.40
    >= 3.5       n= 2   觀測 3.77   模式 2.30   比值 1.70

即 **MSIS 的暴時響應被壓縮**：平靜時略微高估，實際增強愈大就愈低估。

**能宣稱的是趨勢，不是絕對量。** 兩條基線（觀測側的 F10.7 迴歸與模式側的
storm_ratio）之間可能存在常數偏移，該偏移會平移整欄比值。但比值隨增強倍數
**單調上升**這件事對常數偏移免疫，故「響應被壓縮」的結論成立，
而「平靜時高估 15%」則不可單獨引用。

## 為何是工具而不是擷取層

基線窗與評估座標都是**分析決策**，不同用途會有不同選擇；把它固定進擷取層
等於替所有下游做了決定。`DRAG_ENHANCEMENT` 是觀測量，放在儲存層；
與模式的比較是解讀，放在這裡。

## 座標會影響結果

`storm_ratio` 隨評估座標變動可達約 15%（見 tools/density_cross_check.py）。
預設取臺灣位置；比較掩星或全球平均時須自行指定。

用法:
    python tools/density_obs_vs_model.py --start 2024-05-08 --end 2024-05-20
    python tools/density_obs_vs_model.py --window 30 --alt 550
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TW_LAT, TW_LON = 23.5, 121.0
DEFAULT_ALT_KM = 550.0          # 福衛七號任務軌道


def compare(start: datetime, end: datetime, *, alt_km: float = DEFAULT_ALT_KM,
            lat: float = TW_LAT, lon: float = TW_LON) -> pd.DataFrame:
    """回傳逐時刻的觀測倍數、模式倍數與兩者之比。"""
    from orbit_drag.atmospheric import density_ratio
    from swx_core import SwxStore

    store = SwxStore()
    obs = store.series("DRAG_ENHANCEMENT", start=start, end=end, observed_only=True)
    if obs.empty:
        raise SystemExit(
            "儲存層無 DRAG_ENHANCEMENT。請先執行：\n"
            "  python -m services.ingest.run --source tacc_leoorb --date YYYY.DDD")

    model = density_ratio(obs.index, alt_km, lat=lat, lon=lon)
    out = pd.DataFrame({
        "valid_time": obs.index,
        "observed": obs.to_numpy(dtype=float),
        "model": model["storm_ratio"].to_numpy(dtype=float),
        "f107": model["f107"].to_numpy(dtype=float),
        "ap": model["ap"].to_numpy(dtype=float),
    })
    out["rho_ratio"] = np.where(out["model"] > 0, out["observed"] / out["model"], np.nan)
    return out


QUIET_AP = 20.0        # 分層用的地磁寧靜門檻（ap 日均）


def summarize(df: pd.DataFrame) -> dict[str, float]:
    """分層統計。**平靜期與事件期必須分開看**——平靜期的一致性驗證的是
    方法本身，事件期的偏離才是模式誤差。

    **以 ap 分層，不以 observed 分層。** 用被檢驗的量本身來分組會出錯：
    恢復期的模式值仍偏高（MSIS 的 ap 歷史項有長達 57 小時的記憶）而觀測已回落，
    若按 observed < 1.5 分組，這些箱會被歸進「平靜」並把該組中位數拉低——
    實測曾因此得到 0.71，看起來像校準失敗，其實是分層方式的問題。
    """
    quiet = df[df["ap"] < QUIET_AP]
    event = df[df["ap"] >= QUIET_AP]
    def _med(s: pd.Series) -> float:
        return float(s.median()) if len(s) else float("nan")
    return {
        "n": float(len(df)),
        "n_quiet": float(len(quiet)),
        "n_event": float(len(event)),
        "quiet_rho_ratio_median": _med(quiet["rho_ratio"]),
        "event_rho_ratio_median": _med(event["rho_ratio"]),
        "peak_observed": float(df["observed"].max()),
        "peak_model": float(df["model"].max()),
        "max_rho_ratio": float(df["rho_ratio"].max()),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--start", help="起始日期 YYYY-MM-DD")
    ap.add_argument("--end", help="結束日期 YYYY-MM-DD")
    ap.add_argument("--window", type=int, default=30, help="未指定日期時往回取的天數")
    ap.add_argument("--alt", type=float, default=DEFAULT_ALT_KM, help="評估高度 km")
    ap.add_argument("--lat", type=float, default=TW_LAT)
    ap.add_argument("--lon", type=float, default=TW_LON)
    ap.add_argument("--csv", help="另存明細至此路徑")
    a = ap.parse_args()

    now = datetime.now(timezone.utc)
    end = pd.Timestamp(a.end, tz="UTC") if a.end else pd.Timestamp(now)
    start = pd.Timestamp(a.start, tz="UTC") if a.start else end - timedelta(days=a.window)

    df = compare(start.to_pydatetime(), end.to_pydatetime(),
                 alt_km=a.alt, lat=a.lat, lon=a.lon)
    s = summarize(df)

    print(f"評估高度 {a.alt:.0f} km，座標 ({a.lat:.1f}, {a.lon:.1f})")
    print(f"區間 {start.date()} → {end.date()}，{int(s['n'])} 個時刻\n")
    show = df.set_index("valid_time")[["observed", "model", "rho_ratio", "f107", "ap"]]
    print(show.round(2).to_string())

    print(f"\n地磁寧靜（ap < 20，n={int(s['n_quiet'])}）"
          f"  觀測/模式 中位 {s['quiet_rho_ratio_median']:.2f}")
    print(f"地磁擾動（ap >= 20，n={int(s['n_event'])}）"
          f"  觀測/模式 中位 {s['event_rho_ratio_median']:.2f}")
    print(f"峰值：觀測 {s['peak_observed']:.2f}x，模式 {s['peak_model']:.2f}x，"
          f"最大比值 {s['max_rho_ratio']:.2f}")

    if s["n_quiet"] >= 10 and not 0.8 <= s["quiet_rho_ratio_median"] <= 1.25:
        print("\n⚠ 平靜期兩者已不一致——先查基線與座標設定，"
              "不要直接把事件期的偏離解讀為模式誤差。")
    if s["event_rho_ratio_median"] > 1.3:
        print(f"\n模式在事件期低估約 {s['event_rho_ratio_median']:.1f} 倍。"
              "交付的 rho_correction 應據此標註不確定度，"
              "或於 calibrated_by_observation 的說明中載明。")

    if a.csv:
        df.to_csv(a.csv, index=False, encoding="utf-8")
        print(f"\n明細已寫入 {a.csv}")


if __name__ == "__main__":
    main()
