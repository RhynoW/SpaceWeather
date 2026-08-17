"""tools/density_cross_check.py — MSIS 2.1 與 NRLMSISE-00 的同條件交叉比對。

`storm_ratio` 是模型內部的相對量，無法自我驗證。在取得實測反演密度之前，
換一個模型算同一件事，至少能量化「模型選擇本身造成多少差異」。

輸出的百分比是**模型間分歧**，不是誤差，**也不是誤差的下界**：
兩個模型同屬 MSIS 系列、共用大量假設，可能同向偏離實測或同時漏掉同一項物理，
此時分歧小而誤差大。它只回答「換模型會差多少」，
不回答「離真值有多遠」——後者非有觀測校準不可得。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from swx_core import SwxStore  # noqa: E402



def _coord_sensitivity(ep, sw, density_ratio) -> int:
    """評估座標對 storm_ratio 的影響。

    交付的密度修正因子是在**固定參考點**算出來的。這一項回答：
    「換一個地點，這個倍率會差多少？」——若差異可觀，就不能把單一座標的
    結果當成全球適用，尤其不能直接套用到特定任務區域。
    """
    sites = [(0.0, 0.0, "0°N,0°E（交付預設）"),
             (25.0, 121.0, "25°N,121°E（臺灣）"),
             (-45.0, 0.0, "45°S,0°E"),
             (60.0, 300.0, "60°N,300°E（極區）")]
    print("評估座標對 storm_ratio 峰值的影響　高度 450 km\n")
    print(f"{'座標':<24}{'峰值':>9}{'相對預設':>11}")
    base = None
    vals = []
    for lat, lon, tag in sites:
        r = density_ratio(ep, 450.0, sw=sw, lat=lat, lon=lon)
        v = float(np.nanmax(r["storm_ratio"]))
        vals.append(v)
        if base is None:
            base = v
        print(f"{tag:<24}{v:>8.3f}×{100 * (v - base) / base:>10.2f}%")
    print(f"\n座標造成的全距：{100 * (max(vals) - min(vals)) / base:.2f}%")
    print("→ 交付值在固定參考點上取得，**不是全球代表值**；"
          "對應特定任務區域時須以該區座標重算。")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MSIS 2.1 vs NRLMSISE-00 交叉比對")
    ap.add_argument("--start", default="2024-05-08")
    ap.add_argument("--end", default="2024-05-14")
    ap.add_argument("--alts", default="350,450,550", help="高度 km，逗號分隔")
    ap.add_argument("--coords", action="store_true",
                    help="改測評估座標對 storm_ratio 的影響")
    args = ap.parse_args(argv)

    import pymsis

    from orbit_drag.atmospheric import (
        _sw_arrays, build_ap_history, density_ratio, load_space_weather,
    )

    start = pd.Timestamp(args.start, tz="UTC")
    end = pd.Timestamp(args.end, tz="UTC")
    sw = load_space_weather(SwxStore(), start=start, end=end)
    ep = pd.date_range(start, end, freq="3h", tz="UTC")
    if len(ep) == 0 or sw.empty:
        print("此期間無驅動參數資料，請先執行擷取")
        return 1

    if args.coords:
        return _coord_sensitivity(ep, sw, density_ratio)

    dates = np.array([e.to_pydatetime().replace(tzinfo=None) for e in ep])
    f, fa, _a = _sw_arrays(ep, sw)
    aps = build_ap_history(ep, sw)
    lon = np.zeros(len(ep))
    lat = np.zeros(len(ep))

    print(f"期間 {args.start} → {args.end}　lat 0°／lon 0°　每 3 小時　暴時 ap 模式")
    print(f"pymsis {pymsis.__version__}\n")
    print(f"{'高度':>7} {'MSIS 2.1':>14} {'NRLMSISE-00':>14} {'相對差':>9}")
    for alt_km in [float(x) for x in args.alts.split(",")]:
        alt = np.full(len(ep), alt_km)
        r21 = np.asarray(pymsis.calculate(dates, lon, lat, alt, f, fa, aps,
                                          geomagnetic_activity=-1))[..., 0].ravel()
        r00 = np.asarray(pymsis.calculate(dates, lon, lat, alt, f, fa, aps,
                                          geomagnetic_activity=-1, version=0))[..., 0].ravel()
        p21, p00 = float(np.nanmax(r21)), float(np.nanmax(r00))
        print(f"{alt_km:>6.0f}km {p21:>14.3e} {p00:>14.3e} {100 * (p21 - p00) / p00:>8.1f}%")

    print("\n此為模型間分歧，非觀測誤差，亦不可視為誤差下界（兩模型同源，可能同向偏離實測）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
