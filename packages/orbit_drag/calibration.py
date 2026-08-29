"""orbit_drag.calibration — 密度不確定度的實測校準（讀寫 docs/density_calibration.json）。

**這個模組取代的是一個手訂的常數。** `drag_correction._uncertainty()` 原本以
「0.15 起跳、隨 ap 對數成長」的經驗式給不確定度，程式碼裡自己註明那是保守猜測、
不是實測。福衛七號精密定軌反演的 `DRAG_ENHANCEMENT` 與 MSIS 的 `storm_ratio`
相除即得 ρ_obs/ρ_model，它的散布就是修正因子本身的誤差——這裡把猜測換成它。

**校準的是散布，不是偏差。** 觀測側與模式側的基線定義之間可能存在常數偏移，
該偏移會平移整欄比值，因此中位數不可單獨引用為「MSIS 高估幾 %」。
但常數偏移**不會改變散布**，所以 1σ 是可以宣稱的量。

**散布是模式誤差的上界。** 比值裡也含精密定軌反演本身的雜訊，
兩者無法在此分離；用它當不確定度是保守的方向。

檔案放在 `docs/` 而非 `data/`：它是宣稱的證據，必須與引用它的報告同進版控。
"""

from __future__ import annotations

import json
from pathlib import Path

CALIBRATION_PATH = Path(__file__).resolve().parents[2] / "docs" / "density_calibration.json"

#: 校準檔缺席時的退回值（原 drag_correction 的經驗式）。
#: **刻意保留**：沒有實測就該說「這是猜的」，而不是讓產品看起來已校準。
FALLBACK_BASE = 0.15
FALLBACK_AP_TERM = 0.10

#: 一個 ap 帶要能宣稱，至少要這麼多樣本。低於此值的帶退回鄰近帶，
#: 並在中繼資料裡標明——n=3 的 1σ 不是量測，是巧合。
MIN_SAMPLES = 30


def load_calibration(path: Path | None = None) -> dict:
    """讀回校準表；缺席或損毀時回空 dict（呼叫端須退回經驗式）。"""
    try:
        return json.loads((path or CALIBRATION_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def sigma_log(ap: float, calibration: dict | None = None) -> tuple[float, bool]:
    """該地磁活動度下的 1σ（對數空間），回傳 (sigma, 是否為實測校準值)。

    對數空間是正確的框架：密度比值是乘性量，線性空間的 ±σ 會在下界產生
    負密度、上界又太窄。回傳值可直接用作 exp(±σ) 的乘除倍率。
    """
    # `calibration is None` 才去讀檔。用 `or` 會把明確傳入的空 dict
    # （意思是「沒有校準」）當成沒傳，於是又去讀檔——呼叫端想測的
    # 退回路徑因此永遠測不到。
    cal = load_calibration() if calibration is None else calibration
    bands = cal.get("bands") or []
    if bands:
        usable = [b for b in bands if (b.get("n") or 0) >= MIN_SAMPLES]
        for band in usable:
            hi = band.get("ap_max")
            if hi is None or ap < hi:
                return float(band["sigma_log"]), True
        if usable:
            return float(usable[-1]["sigma_log"]), True
    # 退回經驗式：與原 _uncertainty() 的形狀一致，但明確標示未校準
    import math

    return FALLBACK_BASE + FALLBACK_AP_TERM * max(0.0, math.log10(max(ap, 1.0))), False


def band_factors(ap: float, calibration: dict | None = None) -> tuple[float, float, bool]:
    """回傳 (下界倍率, 上界倍率, 是否實測) = (exp(-σ), exp(+σ), calibrated)。"""
    import math

    s, ok = sigma_log(ap, calibration)
    return math.exp(-s), math.exp(s), ok


def summary(calibration: dict | None = None) -> dict:
    """給產品中繼資料用的一行摘要。"""
    cal = calibration if calibration is not None else load_calibration()
    bands = cal.get("bands") or []
    return {
        "calibrated": bool(bands),
        "source": cal.get("source"),
        "sample_span_utc": cal.get("sample_span_utc"),
        "n_total": cal.get("n_total"),
        "generated_utc": cal.get("generated_utc"),
        "note": cal.get("note"),
    }
