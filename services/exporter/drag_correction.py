"""services.exporter.drag_correction — 密度修正因子產品（架構書 §7.1、§10.3 階段 C）。

議題四的最終交付形態之一。刻意設計成**不綁定 STK**：只交付「高度帶 × 時間 →
密度倍率 + 不確定度」的表與格式規範，未來 SDA 平臺自建軌道模組時可直接採用。

修正因子取 storm_ratio（同一 F10.7、地磁寧靜為基準），不是對太陽極小的比值——
後者會把太陽週期當成事件效應，在太陽極大期產生十倍以上的假修正。

不確定度目前以「Ap 強度的經驗函數」給出保守區間，尚未由觀測反演校準。
接上 Sat_TraingDataExtension 的密度反演（A9）後，應改以 ρ_obs/ρ_model 的實測散布取代。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from swx_core import SwxStore

# 預設高度帶（涵蓋主要低軌族群：ISS/遙測、Starlink、太陽同步）
DEFAULT_ALT_BANDS = ((300, 400), (400, 500), (500, 600))


def _uncertainty(storm_ratio: float, ap: float) -> float:
    """修正因子的 1σ 不確定度（對數空間，可直接用作 exp(±σ) 的倍率）。

    **改為由實測校準**（docs/density_calibration.json）：福衛七號精密定軌反演的
    DRAG_ENHANCEMENT 除以 MSIS 的 storm_ratio，其散布就是這個修正因子的誤差。
    校準檔缺席時退回原本的經驗式，並在產品中繼資料標示 calibrated=false——
    沒有實測就該說是猜的，不可讓使用者誤以為是實測誤差棒。

    實測（799 筆，2023-02→2026-06，550 km）顯示原本的經驗式**兩個方向都錯**：
    平靜期給 0.15 而實測 0.223（過於樂觀），ap≥50 給 0.35 而實測 0.282
    （過於保守）。憑直覺調保守係數，會同時在兩端調錯方向。
    """
    if not np.isfinite(storm_ratio):
        return float("nan")
    from orbit_drag.calibration import sigma_log

    sigma, _calibrated = sigma_log(float(ap))
    return round(float(sigma), 3)


def build(
    store: SwxStore | None = None,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    as_of: datetime | None = None,
    alt_bands=DEFAULT_ALT_BANDS,
    freq: str = "3h",
) -> pd.DataFrame:
    """產生密度修正因子表。"""
    from orbit_drag import density_ratio, load_space_weather

    store = store or SwxStore()
    end = end or datetime.now(timezone.utc)
    start = start or (end - timedelta(days=7))

    epochs = pd.date_range(start, end, freq=freq, tz="UTC")
    if len(epochs) == 0:
        return pd.DataFrame()

    sw = load_space_weather(store, start=start, end=end, as_of=as_of)

    rows = []
    for lo, hi in alt_bands:
        mid = (lo + hi) / 2.0
        r = density_ratio(epochs, mid, sw=sw)
        for _, row in r.iterrows():
            rows.append(
                {
                    "valid_time": row["valid_time"],
                    "alt_band_km": f"{lo}-{hi}",
                    "alt_ref_km": mid,
                    "rho_kg_m3": row["rho"],
                    "rho_calm_kg_m3": row["rho_calm"],
                    "storm_ratio": round(float(row["storm_ratio"]), 4),
                    "uncertainty": _uncertainty(row["storm_ratio"], row["ap"]),
                    "f107": row["f107"],
                    "ap": row["ap"],
                }
            )
    return pd.DataFrame(rows).sort_values(["valid_time", "alt_ref_km"]).reset_index(drop=True)


def to_event_products(df: pd.DataFrame) -> list[dict]:
    """壓縮成事件卡 orbit_products.rho_correction 的形式（取各高度帶峰值）。"""
    if df.empty:
        return []
    out = []
    for band, grp in df.groupby("alt_band_km"):
        peak = grp.loc[grp["storm_ratio"].idxmax()]
        lo, hi = (int(x) for x in str(band).split("-"))
        out.append(
            {
                "alt_band_km": [lo, hi],
                "ratio": float(peak["storm_ratio"]),
                "unc": float(peak["uncertainty"]),
                "peak_utc": peak["valid_time"].isoformat().replace("+00:00", "Z"),
                "method": "MSIS 2.1 storm-ratio (same F10.7, quiet Ap baseline, storm-time ap mode)",
                "calibrated_by_observation": _calibration_summary()["calibrated"],
            }
        )
    return sorted(out, key=lambda d: d["alt_band_km"][0])


def product_metadata() -> dict:
    """修正因子產品的基準定義（單一來源，API 與檔案匯出共用）。

    `baseline` 必須寫死「7 個 ap 歷史元素全部換成寧靜值」——只換日均 Ap 而留著
    真實 ap 歷史的話，暴時模式下基準與擾動態會變成同一件事，比值恆為 1。
    這個錯誤發生過且不易察覺，故把實際作法寫進交付 metadata 而非僅寫在註解。
    """
    import pymsis

    return {
        "product": "drag_density_correction",
        "schema_version": "1.0",
        "model": "MSIS 2.1",
        "model_impl": f"pymsis {pymsis.__version__}",
        "ap_mode": "storm-time (geomagnetic_activity=-1, 7-element ap history)",
        "baseline": (
            "same epoch / location / altitude / F10.7; geomagnetic inputs replaced by "
            "quiet values — daily Ap=4 AND all 7 elements of the ap history set to 4"
        ),
        "evaluated_at": "lat 0.0, lon 0.0 (fixed reference point; see docs)",
        # 不確定度是否由實測校準，與比值本身是否校準是兩件事：
        # 比值仍是純模式輸出，只有它的誤差棒有了實測依據。混為一談會讓
        # 使用者以為 storm_ratio 已被觀測修正過。
        "calibrated_by_observation": _calibration_summary()["calibrated"],
        "uncertainty_calibration": _calibration_summary(),
        "uncertainty_definition": (
            "1-sigma in log space; multiply/divide the ratio by exp(sigma) "
            "for the band. Calibrated against FORMOSAT-7 POD-derived "
            "DRAG_ENHANCEMENT / MSIS storm_ratio; the spread is an upper bound "
            "on model error because it also contains POD retrieval noise."
        ),
    }


def _calibration_summary() -> dict:
    from orbit_drag.calibration import summary

    return summary()


def export(
    df: pd.DataFrame,
    path: str | Path,
    *,
    fmt: str = "csv",
) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        payload = {
            **product_metadata(),
            "generated_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "records": json.loads(df.to_json(orient="records", date_format="iso")),
        }
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        df.to_csv(p, index=False, encoding="utf-8-sig")
    return p


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="產生大氣密度修正因子產品")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--fmt", default="csv", choices=["csv", "json"])
    args = ap.parse_args(argv)

    def _ts(v):
        return pd.Timestamp(v, tz="UTC").to_pydatetime() if v else None

    store = SwxStore()
    df = build(store, start=_ts(args.start), end=_ts(args.end), as_of=_ts(args.as_of))
    if df.empty:
        print("無資料可產生修正因子")
        return 1

    out = Path(args.out) if args.out else store.root / "exports" / f"drag_correction.{args.fmt}"
    export(df, out, fmt=args.fmt)

    peak = df.loc[df["storm_ratio"].idxmax()]
    print(f"已輸出 {out}（{len(df)} 列）")
    print(f"  期間 {df['valid_time'].min()} → {df['valid_time'].max()}")
    print(
        f"  最大修正倍率 {peak['storm_ratio']:.2f}×（{peak['alt_band_km']} km，"
        f"{peak['valid_time']:%Y-%m-%d %H:%M}Z，Ap={peak['ap']:.0f}）"
    )
    print("  基準：同一 F10.7、地磁寧靜（Ap=4）；尚未由觀測反演校準")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
