"""leoOrb 阻力衰減率反演的性質測試。

全部以合成資料進行，不連網。守的是三個靜默失效：
SP3 的單位與時間慣例、短週期混疊、以及單顆機動污染整批。
"""

import numpy as np
import pandas as pd
import pytest

from services.ingest.tacc_leoorb import (
    B_MAX,
    B_MIN,
    BIN,
    DISPERSION_SUSPECT,
    GPS_MINUS_UTC_S,
    MU,
    decay_rates,
    enhancement_rows,
    fit_quiet_baseline,
    parse_sp3,
    rates_to_frame,
)

SP3_SAMPLE = """#cV2026  8 16 21 54  0.00000000     2       IGS08 FIT UCAR
## 2432  78840.00000000    60.00000000 61268 0.9124999999985
+    1   L76  0  0  0  0  0  0  0  0  0  0  0  0  0  0  0  0
%c    cc GPS ccc cccc cccc cccc cccc ccccc ccccc ccccc ccccc
*  2026  8 16 21 54  0.00000000
PL76   6480.000291   1941.290815  -1618.051741    105.204720
VL76 -13229.809993  65143.535347  25277.101684 999999.999999
*  2026  8 16 21 55  0.00000000
PL76   6400.000000   2330.000000  -1465.000000    105.204720
VL76 -13500.000000  64000.000000  25000.000000 999999.999999
EOF
"""


def test_parse_sp3_units_and_time():
    """速度須由 dm/s 轉為 km/s，時間須由 GPS 時轉為 UTC。"""
    sat, df = parse_sp3(SP3_SAMPLE)
    assert sat == "L76"
    assert len(df) == 2

    # dm/s -> km/s：檔內 -13229.809993 dm/s = -1.3229809993 km/s
    assert df["vx"].iloc[0] == pytest.approx(-1.3229809993, abs=1e-9)
    # 地固速率約 7.11 km/s（慣性為 7.57，差值即地球自轉）
    speed = np.linalg.norm(df[["vx", "vy", "vz"]].iloc[0].to_numpy())
    assert speed == pytest.approx(7.1117, abs=1e-3)

    # SP3 標頭時刻 21:54:00 為 GPS 時，UTC 應早 18 秒
    assert df["t_utc"].iloc[0] == pd.Timestamp("2026-08-16 21:53:42", tz="UTC")
    assert GPS_MINUS_UTC_S == 18.0


def test_parse_sp3_rejects_non_sp3():
    assert parse_sp3("not an sp3 file\nat all\n") is None
    assert parse_sp3("") is None


def _synthetic(rate_m_per_day: float, days: float = 4.0, n_sats: int = 5,
               amplitude_km: float = 1.7, offsets=None) -> dict[str, pd.Series]:
    """合成半長軸序列：線性衰減 + 大振幅短週期項。

    短週期振幅刻意設為實測值 1.7 km，遠大於每日數十公尺的衰減——
    這正是混疊會發生的條件。
    """
    a0 = 6960.0
    period_s = 2.0 * np.pi * np.sqrt(a0 ** 3 / MU)
    idx = pd.date_range("2024-05-08", periods=int(days * 1440), freq="60s", tz="UTC")
    t_s = (idx - idx[0]).total_seconds().to_numpy()
    out = {}
    for k in range(n_sats):
        phase = 2.0 * np.pi * k / n_sats            # 各衛星相位不同
        extra = (offsets or {}).get(k, 0.0)
        a = (a0
             - rate_m_per_day / 1000.0 * t_s / 86400.0
             + amplitude_km * np.sin(2 * np.pi * t_s / period_s + phase)
             + extra * (t_s > t_s[len(t_s) // 2]) / 1000.0)   # 後半段的階躍（機動）
        out[f"L{70+k}"] = pd.Series(a, index=idx)
    return out


def test_orbit_period_smoothing_defeats_aliasing():
    """1.7 km 的短週期不得混疊成長期趨勢。

    這是本模組最重要的守則。實測曾因以 6 小時取樣而把 7.7 m/日 讀成 39 m/日，
    也曾因此把一次明確的機動誤判為「無機動」。
    """
    truth = 60.0
    rate = decay_rates(_synthetic(truth))
    got = rate.stack().dropna()
    assert len(got) > 8
    assert got.mean() == pytest.approx(truth, rel=0.05)
    # 個別分箱也不該被短週期帶偏
    assert got.std() < truth * 0.25


def test_naive_resampling_would_alias():
    """反證：不以軌道週期為窗，短週期確實會污染斜率。

    此測試存在的理由是證明上一個測試守的東西是真的風險，
    而不是多餘的防護。
    """
    sma = _synthetic(60.0)
    naive = pd.DataFrame(sma).resample("6h").mean()
    naive_rate = -(naive.diff() / 0.25) * 1000.0
    spread = naive_rate.stack().dropna().std()
    proper = decay_rates(sma).stack().dropna().std()
    assert spread > proper * 3, "未以軌道週期為窗時應出現明顯更大的離散"


def test_median_is_robust_to_single_maneuver():
    """一顆衛星機動不應帶偏整批的衰減率。"""
    clean = rates_to_frame(decay_rates(_synthetic(60.0)))
    # 第 0 顆在後半段抬升 30 m
    maneuvered = rates_to_frame(decay_rates(_synthetic(60.0, offsets={0: 30.0})))
    assert not clean.empty and not maneuvered.empty
    a = clean.set_index("valid_time")["value"]
    b = maneuvered.set_index("valid_time")["value"]
    common = a.index.intersection(b.index)
    assert len(common) > 4
    # 中位數應幾乎不動；若改用平均會被 1/5 的離群拉走
    assert np.allclose(a[common], b[common], rtol=0.02)


def test_high_dispersion_is_flagged_not_dropped():
    """單顆離群須被標記，而非靜默吸收。

    這裡刻意只讓「一顆」偏離：中位數絕對偏差在 n=5 時對此完全無感
    （除非過半偏離否則恆為零），而那正是最需要偵測的情形。
    """
    rate = decay_rates(_synthetic(60.0)).copy()
    rate.iloc[:, 0] = rate.iloc[:, 0] * 4.0          # 單顆離群（機動的樣態）
    out = rates_to_frame(rate)
    assert not out.empty
    assert (out["quality_flag"] == "suspect").any()
    assert out["quality_reason"][out["quality_flag"] == "suspect"].str.contains(
        "dispersion").all()


def test_units_and_sign_convention():
    """DRAG_DECAY 為正值（衰減量），單位 m/day。"""
    out = rates_to_frame(decay_rates(_synthetic(60.0)))
    assert (out["value"] > 0).all()
    assert (out["unit"] == "m/day").all()
    assert (out["param_code"] == "DRAG_DECAY").all()
    assert (out["data_type"] == "OBS").all()


def test_insufficient_satellites_yields_nothing():
    """少於三顆無法用中位數排除離群，應回空表而非硬算。"""
    assert rates_to_frame(decay_rates(_synthetic(60.0, n_sats=2))).empty


def test_bin_cadence_matches_declared():
    out = rates_to_frame(decay_rates(_synthetic(60.0)))
    gaps = out["valid_time"].diff().dropna().unique()
    assert len(gaps) == 1
    assert pd.Timedelta(gaps[0]) == pd.Timedelta(BIN)
    assert DISPERSION_SUSPECT > 0.12, "門檻須高於平靜期實測的離散度上緣"


# ── 增強倍數：必須先扣掉太陽通量 ────────────────────────────────────
def _drivers(idx, f107_start=130.0, f107_end=240.0, kp=2.0):
    f = pd.Series(np.linspace(f107_start, f107_end, len(idx)), index=idx)
    k = pd.Series(kp, index=idx)
    return f, k


def test_enhancement_removes_solar_flux_trend():
    """F10.7 上升造成的衰減增加不得被當成事件效應。

    這是實測踩到的坑：2024-04-29 至 05-19 的 F10.7 由 132 漲到 238，
    以滾動分位數當基線時平靜期的「增強倍數」是 1.4–1.65 而非 1.0，
    L1 因此持續誤觸發。
    """
    idx = pd.date_range("2024-03-01", periods=200, freq="6h", tz="UTC")
    f107, kp = _drivers(idx)
    # 衰減率完全由 F10.7 驅動、無地磁事件 → 增強倍數應恆為 1
    decay_val = 20.0 * np.exp(0.0058 * f107.to_numpy())
    decay = pd.DataFrame({
        "valid_time": idx, "param_code": "DRAG_DECAY", "value": decay_val,
        "unit": "m/day", "data_type": "OBS",
        "quality_flag": "good", "quality_reason": "",
    })
    out = enhancement_rows(decay, None, f107, kp)
    assert not out.empty
    assert out["value"].mean() == pytest.approx(1.0, abs=0.02)
    assert out["value"].std() < 0.02


def test_enhancement_keeps_geomagnetic_signal():
    """扣掉太陽通量後，地磁造成的增強必須保留。"""
    idx = pd.date_range("2024-03-01", periods=200, freq="6h", tz="UTC")
    f107, kp = _drivers(idx)
    decay_val = 20.0 * np.exp(0.0058 * f107.to_numpy())
    storm = slice(150, 155)
    decay_val = decay_val.copy()
    decay_val[storm] *= 4.0
    kp = kp.copy()
    kp.iloc[storm] = 9.0                      # 暴時分箱不得進入寧靜擬合
    decay = pd.DataFrame({
        "valid_time": idx, "param_code": "DRAG_DECAY", "value": decay_val,
        "unit": "m/day", "data_type": "OBS",
        "quality_flag": "good", "quality_reason": "",
    })
    out = enhancement_rows(decay, None, f107, kp).set_index("valid_time")["value"]
    assert out.iloc[storm].mean() == pytest.approx(4.0, rel=0.05)
    quiet = out.drop(out.index[storm])
    assert quiet.mean() == pytest.approx(1.0, abs=0.03)


def test_enhancement_returns_empty_when_undercalibrated():
    """寧靜樣本不足時回空表，而不是退回 1.0。

    「算不出來」與「沒有增強」是兩件事——後者會讓下游誤以為已確認平靜。
    """
    idx = pd.date_range("2024-03-01", periods=20, freq="6h", tz="UTC")
    f107, kp = _drivers(idx)
    decay = pd.DataFrame({
        "valid_time": idx, "param_code": "DRAG_DECAY", "value": np.full(20, 50.0),
        "unit": "m/day", "data_type": "OBS",
        "quality_flag": "good", "quality_reason": "",
    })
    assert enhancement_rows(decay, None, f107, kp).empty
    assert fit_quiet_baseline(
        pd.Series(50.0, index=idx), f107, kp) is None


def test_baseline_fit_slope_is_clipped_to_physical_range():
    """擬合斜率被離群值帶走時須夾在物理範圍內。"""
    idx = pd.date_range("2024-03-01", periods=200, freq="6h", tz="UTC")
    f107, kp = _drivers(idx, 150.0, 150.6)     # F10.7 幾乎不變 → 斜率不可信
    noisy = pd.Series(50.0 * (1 + 0.5 * np.sin(np.arange(200))), index=idx)
    fit = fit_quiet_baseline(noisy, f107, kp)
    assert fit is not None
    assert B_MIN <= fit[1] <= B_MAX
