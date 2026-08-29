"""密度不確定度校準的契約。

守的是三件事：

  1. 校準值必須真的被用到（不能悄悄退回手訂常數）；
  2. 校準檔缺席時必須**標明未校準**，而不是假裝有實測依據；
  3. 樣本不足的 ap 帶不得被當成量測結果。
"""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from orbit_drag.calibration import (FALLBACK_BASE, MIN_SAMPLES, band_factors,
                                    load_calibration, sigma_log, summary)

_CAL = {
    "bands": [
        {"label": "ap<10", "ap_min": 0.0, "ap_max": 10.0, "n": 300, "sigma_log": 0.20},
        {"label": "ap 10-20", "ap_min": 10.0, "ap_max": 20.0, "n": 5, "sigma_log": 0.99},
        {"label": "ap>=20", "ap_min": 20.0, "ap_max": None, "n": 80, "sigma_log": 0.30},
    ]
}


def test_calibrated_sigma_is_used_and_flagged():
    s, ok = sigma_log(3.0, _CAL)
    assert (s, ok) == (0.20, True)
    s, ok = sigma_log(120.0, _CAL)
    assert (s, ok) == (0.30, True)


def test_undersized_band_is_skipped_not_reported():
    """n=5 的 1σ 不是量測，是巧合；必須落到下一個有樣本的帶。"""
    assert _CAL["bands"][1]["n"] < MIN_SAMPLES
    s, ok = sigma_log(15.0, _CAL)
    assert s == 0.30, "樣本不足的帶被當成量測結果採用了"
    assert ok is True


def test_missing_calibration_falls_back_and_says_so():
    """沒有實測就該說是猜的，不可讓產品看起來已校準。"""
    s, ok = sigma_log(1.0, {})
    assert ok is False
    assert s == pytest.approx(FALLBACK_BASE)
    assert summary({})["calibrated"] is False


def test_band_factors_are_multiplicative_and_asymmetric():
    """密度比值是乘性量：±σ 在線性空間必然不對稱。

    用 1±σ 而非 exp(±σ) 會在 σ 大時給出負密度下界。
    """
    lo, hi, ok = band_factors(3.0, _CAL)
    assert ok
    assert lo == pytest.approx(math.exp(-0.20))
    assert hi == pytest.approx(math.exp(0.20))
    assert (hi - 1.0) > (1.0 - lo), "上下界對稱代表用了線性而非對數空間"


def test_shipped_calibration_file_is_self_consistent():
    """版控裡的校準檔必須說得出樣本、期間與方法。"""
    cal = load_calibration()
    if not cal:
        pytest.skip("尚未產生 docs/density_calibration.json")

    assert cal["n_total"] >= MIN_SAMPLES
    assert cal["source"].startswith("DRAG_ENHANCEMENT")
    assert "常數偏移" in cal["note"], "缺少「校準的是散布不是偏差」的但書"
    usable = [b for b in cal["bands"] if b.get("usable")]
    assert usable, "沒有任何 ap 帶達到樣本下限"
    for b in usable:
        assert 0.0 < b["sigma_log"] < 1.0
        assert b["n"] >= MIN_SAMPLES


def test_drag_correction_uses_the_calibrated_sigma():
    from services.exporter.drag_correction import _uncertainty

    cal = load_calibration()
    if not cal:
        pytest.skip("尚未產生 docs/density_calibration.json")
    quiet = [b for b in cal["bands"] if b.get("usable")][0]
    assert _uncertainty(1.0, 1.0) == pytest.approx(quiet["sigma_log"], abs=1e-3)


def test_product_metadata_reports_calibration_state():
    from services.exporter.drag_correction import product_metadata

    meta = product_metadata()
    assert "uncertainty_calibration" in meta
    assert meta["calibrated_by_observation"] == meta["uncertainty_calibration"]["calibrated"]
    assert "log space" in meta["uncertainty_definition"]


def test_model_band_widens_with_geomagnetic_activity():
    """暴時的模式散布比平靜期大；用單一常數會在兩端都錯。"""
    from tools.alongtrack_drivers import model_band

    cal = load_calibration()
    if not cal:
        pytest.skip("尚未產生 docs/density_calibration.json")

    epochs = pd.date_range("2026-09-01", periods=4, freq="24h", tz="UTC")
    fc = pd.DataFrame({"f107": 120.0, "ap": [5.0, 5.0, 200.0, 200.0]}, index=epochs)
    hi, lo, ok, sig = model_band(fc, epochs)
    assert ok
    assert sig[2] > sig[0], "暴時的 1σ 未大於平靜期"
    assert np.all(hi > 1.0) and np.all(lo < 1.0)

    hi2, _lo2, ok2, sig2 = model_band(fc, epochs, override=0.15)
    assert ok2 is False and np.allclose(sig2, 0.15)
