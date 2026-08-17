"""tools/density_cross_check.py — MSIS 2.1 與 NRLMSISE-00 的同條件交叉比對。

`storm_ratio` 是模型內部的相對量，無法自我驗證。在取得實測反演密度之前，
換一個模型算同一件事，至少能量化「模型選擇本身造成多少差異」。

輸出的百分比是**模型間分歧**，不是誤差——兩個模型可能同向偏離實測。
它給的是模型系統誤差的**下界**，不是誤差本身。
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MSIS 2.1 vs NRLMSISE-00 交叉比對")
    ap.add_argument("--start", default="2024-05-08")
    ap.add_argument("--end", default="2024-05-14")
    ap.add_argument("--alts", default="350,450,550", help="高度 km，逗號分隔")
    args = ap.parse_args(argv)

    import pymsis

    from orbit_drag.atmospheric import _sw_arrays, build_ap_history, load_space_weather

    start = pd.Timestamp(args.start, tz="UTC")
    end = pd.Timestamp(args.end, tz="UTC")
    sw = load_space_weather(SwxStore(), start=start, end=end)
    ep = pd.date_range(start, end, freq="3h", tz="UTC")
    if len(ep) == 0 or sw.empty:
        print("此期間無驅動參數資料，請先執行擷取")
        return 1

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

    print("\n此為模型間分歧，非誤差；真實誤差不會小於此量級，但也不等於此量級。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
