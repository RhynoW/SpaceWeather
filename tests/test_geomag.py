"""地磁參考場測試（構想書議題二）。

守兩件事：
  1. IGRF 算出來的臺灣地區場值必須符合實際觀測量級——算錯了整個議題二都是錯的
  2. 推估值與實測值必須永遠可區分（is_proxy 旗標）
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from geomag import (
    STATIONS,
    TW_LAT,
    TW_LON,
    geomagnetic_latitude,
    igrf_field,
    regional_disturbance,
    regional_disturbance_proxy,
    station_fields,
    summary,
)

EPOCH = dt.datetime(2026, 8, 17)


def test_taiwan_field_matches_observed_magnitudes():
    """臺灣地區實測：總場約 45,000 nT、磁傾角約 35°、磁偏角約 −5°。"""
    fv = igrf_field(TW_LAT, TW_LON, EPOCH)
    assert 43_000 < fv.f < 47_000, f"總場 {fv.f:.0f} nT 偏離臺灣實測量級"
    assert 30 < fv.inclination_deg < 42, f"磁傾角 {fv.inclination_deg:.1f}° 不合理"
    assert -8 < fv.declination_deg < -2, f"磁偏角 {fv.declination_deg:.1f}° 不合理"
    assert fv.z_down > 0, "北半球磁場應下傾（Z 為正）"


def test_field_components_are_self_consistent():
    fv = igrf_field(TW_LAT, TW_LON, EPOCH)
    assert fv.h == pytest.approx(np.hypot(fv.x_north, fv.y_east))
    assert fv.f == pytest.approx(np.hypot(fv.h, fv.z_down))


def test_field_varies_with_latitude():
    """磁傾角隨緯度單調增加——若不成立代表座標軸弄反了。"""
    incs = [igrf_field(lat, TW_LON, EPOCH).inclination_deg for lat in (10, 25, 45, 60)]
    assert incs == sorted(incs), f"磁傾角未隨緯度遞增：{incs}"


def test_geomagnetic_latitude_is_lower_than_geographic():
    """臺灣地理緯度約 23.5°N，地磁緯度明顯較低——這是赤道異常影響臺灣的物理原因。"""
    mlat = geomagnetic_latitude(TW_LAT, TW_LON, EPOCH)
    assert 10 < mlat < TW_LAT, f"地磁緯度 {mlat:.1f}° 應低於地理緯度且為正"


def test_station_table_covers_all_stations():
    df = station_fields(EPOCH)
    assert set(df["station"]) == set(STATIONS)
    assert (df["F_nT"] > 40_000).all()


def test_proxy_is_always_flagged():
    """推估值必須永遠標記，否則報告會把推估當實測。"""
    idx = pd.date_range("2024-05-10", periods=6, freq="h", tz="UTC")
    dst = pd.Series([-20, -80, -200, -400, -300, -150], index=idx, dtype=float)
    out = regional_disturbance_proxy(dst=dst, epoch=EPOCH)
    assert out["is_proxy"].all()
    assert out["basis"].iloc[0] == "dst"
    # 低緯縮放因子接近 1：環電流壓抑在低緯與 Dst 相當接近
    assert 0.85 < out["scale_factor"].iloc[0] <= 1.0
    assert out["dH_est_nT"].min() < -300


def test_proxy_falls_back_to_kp_and_marks_basis():
    idx = pd.date_range("2024-05-10", periods=4, freq="3h", tz="UTC")
    kp = pd.Series([2.0, 5.0, 8.0, 6.0], index=idx)
    out = regional_disturbance_proxy(kp=kp, epoch=EPOCH)
    assert out["basis"].iloc[0] == "kp_fallback"
    assert out["is_proxy"].all()
    assert out["dH_est_nT"].is_monotonic_decreasing is False   # 隨 Kp 起伏


def test_proxy_empty_input_returns_empty():
    out = regional_disturbance_proxy()
    assert out.empty


def test_measured_disturbance_is_not_flagged_proxy():
    """實測路徑必須標 is_proxy=False，與推估明確區分。"""
    idx = pd.date_range("2024-05-10", periods=3, freq="h", tz="UTC")
    ref = igrf_field(*STATIONS["LNP"][:2], EPOCH)
    observed = pd.DataFrame(
        {
            "valid_time": idx,
            "x_north": [ref.x_north, ref.x_north - 200, ref.x_north - 50],
            "y_east": [ref.y_east] * 3,
            "z_down": [ref.z_down] * 3,
        }
    )
    out = regional_disturbance(observed, station="LNP", epoch=EPOCH)
    assert not out["is_proxy"].any()
    assert out["dH_nT"].iloc[0] == pytest.approx(0.0, abs=1.0)
    assert out["dH_nT"].iloc[1] < -150


def test_unknown_station_raises():
    with pytest.raises(KeyError):
        regional_disturbance(pd.DataFrame({"valid_time": [], "h": []}), station="XXX")


def test_summary_states_the_limitation():
    s = summary(EPOCH)
    assert s["model"].startswith("IGRF")
    assert "is_proxy" in s["note"], "摘要必須載明推估與實測的區別"
    assert len(s["stations"]) == len(STATIONS)
