"""tools.calibrate_density_uncertainty — 由實測反演校準密度不確定度。

把 `drag_correction._uncertainty()` 的手訂常數換成量到的散布。方法：

    RHO_RATIO = DRAG_ENHANCEMENT / storm_ratio      （觀測 / 模式）

`DRAG_ENHANCEMENT` 來自福衛七號精密定軌反演的軌道衰減率，`storm_ratio` 來自
MSIS 2.1。兩者的定義對應（同為「相對同一 F10.7 之寧靜期望值」的倍數），
相除即得修正因子本身的誤差。

**校準的是散布，不是偏差。** 兩側基線之間可能有常數偏移，會平移整欄比值；
中位數因此不可單獨引用為「MSIS 高估幾 %」。但常數偏移不改變散布，
所以 1σ 可以宣稱。散布中也含精密定軌反演自身的雜訊，兩者無法在此分離，
故量到的 σ 是模式誤差的**上界**——用它當不確定度是保守的方向。

以 **ap 分層而非以 observed 分層**：用被檢驗的量本身分組，恢復期會被錯分
（MSIS 的 ap 歷史項有 57 小時記憶，模式仍高而觀測已回落），
整組中位數被拉低，看起來像校準失敗。

用法：
    python -m tools.calibrate_density_uncertainty            # 只看，不寫檔
    python -m tools.calibrate_density_uncertainty --write    # 寫 docs/density_calibration.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orbit_drag.calibration import CALIBRATION_PATH, MIN_SAMPLES  # noqa: E402

#: ap 分層邊界（上界，最後一帶為 None 代表無上界）
AP_BANDS = ((10.0, "ap<10"), (20.0, "ap 10-20"), (50.0, "ap 20-50"), (None, "ap>=50"))


def build(start: datetime, end: datetime, *, alt_km: float, lat: float, lon: float
          ) -> tuple[pd.DataFrame, dict]:
    from tools.density_obs_vs_model import compare

    df = compare(start, end, alt_km=alt_km, lat=lat, lon=lon).dropna(subset=["rho_ratio"])
    if df.empty:
        raise SystemExit("無可用的 RHO_RATIO 樣本")

    median = float(df["rho_ratio"].median())
    bands = []
    lo = 0.0
    for hi, label in AP_BANDS:
        m = (df["ap"] >= lo) & ((df["ap"] < hi) if hi is not None else True)
        sub = df.loc[m, "rho_ratio"]
        entry = {"label": label, "ap_min": lo, "ap_max": hi, "n": int(len(sub))}
        if len(sub) >= 3:
            # 以**全域中位數**正規化，不是各帶自己的中位數：常數偏移是全域的，
            # 各帶自行置中會把帶間的系統差異一併吸收掉，散布因而被低估。
            norm = np.log((sub / median).to_numpy(dtype=float))
            entry.update({
                "median": round(float(sub.median()), 3),
                "sigma_log": round(float(np.std(norm)), 3),
                "p16": round(float(np.percentile(sub / median, 16)), 3),
                "p84": round(float(np.percentile(sub / median, 84)), 3),
                "p95": round(float(np.percentile(sub / median, 95)), 3),
                "usable": bool(len(sub) >= MIN_SAMPLES),
            })
        else:
            entry["usable"] = False
        bands.append(entry)
        lo = hi if hi is not None else lo

    doc = {
        "schema": 1,
        "source": "DRAG_ENHANCEMENT（福衛七號 TACC leoOrb 精密定軌反演） / MSIS 2.1 storm_ratio",
        "generated_utc": f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}",
        "sample_span_utc": [f"{df['valid_time'].min():%Y-%m-%dT%H:%M:%SZ}",
                            f"{df['valid_time'].max():%Y-%m-%dT%H:%M:%SZ}"],
        "n_total": int(len(df)),
        "global_median": round(median, 3),
        "eval_alt_km": alt_km,
        "eval_coords": [lat, lon],
        "bands": bands,
        "note": ("校準的是散布不是偏差：兩側基線可能有常數偏移，中位數不可單獨引用。"
                 "散布含精密定軌反演自身的雜訊，故 sigma_log 是模式誤差的上界。"),
        "command": "python -m tools.calibrate_density_uncertainty --write",
    }
    return df, doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="由實測反演校準密度不確定度")
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default=None, help="預設為今天")
    ap.add_argument("--alt", type=float, default=550.0, help="評估高度 km（福衛七號任務軌道）")
    ap.add_argument("--lat", type=float, default=23.5)
    ap.add_argument("--lon", type=float, default=121.0)
    ap.add_argument("--write", action="store_true", help="寫入 docs/density_calibration.json")
    a = ap.parse_args(argv)

    end = pd.Timestamp(a.end, tz="UTC") if a.end else pd.Timestamp.now(tz="UTC")
    start = pd.Timestamp(a.start, tz="UTC")
    df, doc = build(start.to_pydatetime(), end.to_pydatetime(),
                    alt_km=a.alt, lat=a.lat, lon=a.lon)

    print(f"樣本 {doc['n_total']} 筆，{doc['sample_span_utc'][0][:10]} "
          f"→ {doc['sample_span_utc'][1][:10]}，"
          f"評估高度 {a.alt:.0f} km、座標 ({a.lat:g}, {a.lon:g})")
    print(f"全域中位數 {doc['global_median']:.3f}"
          "（**不可引用為模式偏差**：兩側基線可能有常數偏移）\n")

    rows = []
    for b in doc["bands"]:
        rows.append({
            "ap 帶": b["label"], "n": b["n"],
            "中位數": b.get("median"), "1sigma(log)": b.get("sigma_log"),
            "倍率下界": (round(float(np.exp(-b["sigma_log"])), 2)
                       if b.get("sigma_log") else None),
            "倍率上界": (round(float(np.exp(b["sigma_log"])), 2)
                       if b.get("sigma_log") else None),
            "可用": b.get("usable"),
        })
    print(pd.DataFrame(rows).to_string(index=False))

    usable = [b for b in doc["bands"] if b.get("usable")]
    if usable:
        lo_s = usable[0]["sigma_log"]
        hi_s = usable[-1]["sigma_log"]
        print(f"\n量到的 1σ：平靜 {lo_s:.3f}、擾動 {hi_s:.3f}（對數空間）。")
        print(f"對照原本手訂的經驗式：ap=1 給 0.15、ap=100 給 0.35。")
        print("即**平靜期原本過於樂觀、暴時原本過於保守**——兩個方向都錯，"
              "而且錯的方向相反。")
    else:
        print("\n沒有任何 ap 帶達到樣本下限，不足以校準。")

    if a.write:
        CALIBRATION_PATH.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        print(f"\n已寫入 {CALIBRATION_PATH}")
    else:
        print("\n（未寫檔；加 --write 以更新 docs/density_calibration.json）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
