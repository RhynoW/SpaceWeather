"""TEME ↔ ITRF 轉換的性質測試。

不連網——EOP 以合成表代入。這些測試守的是**靜默算錯**的失敗型態：
框架轉換弄錯不會拋例外，只會產生看似合理但偏差數百公里的軌道。
"""

import numpy as np
import pandas as pd
import pytest

from orbit_drag.frames import (
    OMEGA,
    gmst82,
    itrf_to_teme,
    ric,
    teme_to_itrf,
)

MU = 398600.4418


@pytest.fixture
def eop():
    """涵蓋 2026 年的合成 EOP，數量級取自 CelesTrak 實測值。"""
    mjd = np.arange(61000, 61600, dtype=float)
    return pd.DataFrame(
        {"X": 0.2225, "Y": 0.3520, "UT1-UTC": 0.0078},
        index=pd.Index(mjd, name="MJD"),
    )


@pytest.fixture
def sample():
    """福衛七號 leoOrb 的一筆實際 SP3 記錄（ITRF，速度為地固速度）。"""
    t = pd.DatetimeIndex(["2026-08-16T21:53:42Z"])
    r = np.array([[6480.000291, 1941.290815, -1618.051741]])
    v = np.array([[-1.3229809993, 6.5143535347, 2.5277101684]])
    return t, r, v


def test_gmst82_at_j2000():
    """J2000.0 的 GMST 為 280.46061837°，此為該式的定義常數項。"""
    assert gmst82(np.array([2451545.0]))[0] == pytest.approx(
        np.deg2rad(280.46061837), abs=1e-9
    )


def test_rotation_preserves_radius(eop, sample):
    """框架轉換是旋轉，必須保長度。若不保長度即為矩陣寫錯。"""
    t, r, v = sample
    r_teme, _ = itrf_to_teme(r, v, t, eop)
    assert np.linalg.norm(r_teme) == pytest.approx(np.linalg.norm(r), abs=1e-6)


def test_inertial_velocity_includes_earth_rotation(eop, sample):
    """地固速度轉慣性必須加 ω×r。

    這是本模組最容易靜默出錯的地方：SP3 的速度是地固速度，
    直接當慣性速度用，vis-viva 反算的半長軸會偏低數百公里。
    """
    t, r, v = sample
    _, v_teme = itrf_to_teme(r, v, t, eop)
    speed_ecef = np.linalg.norm(v)
    speed_eci = np.linalg.norm(v_teme)
    radius = np.linalg.norm(r)

    # 地固速度顯著小於慣性速度——差值即地球自轉貢獻
    assert speed_ecef == pytest.approx(7.1117, abs=1e-3)
    assert speed_eci == pytest.approx(7.5730, abs=1e-3)

    # 慣性速度應接近該半徑的圓軌道速度
    assert speed_eci == pytest.approx(np.sqrt(MU / radius), rel=2e-3)

    # 若誤用地固速度，vis-viva 半長軸會低到離譜
    a_ok = 1.0 / (2.0 / radius - speed_eci ** 2 / MU)
    a_bad = 1.0 / (2.0 / radius - speed_ecef ** 2 / MU)
    assert a_ok == pytest.approx(6960.0, abs=5.0)
    assert a_ok - a_bad > 500.0


def test_round_trip(eop, sample):
    """ITRF → TEME → ITRF 應還原到數值精度。"""
    t, r, v = sample
    r_t, v_t = itrf_to_teme(r, v, t, eop)
    r_back, v_back = teme_to_itrf(r_t, v_t, t, eop)
    assert np.allclose(r_back, r, atol=1e-9)
    assert np.allclose(v_back, v, atol=1e-12)


def test_eop_omission_is_not_free(sample):
    """省略 EOP 不會報錯，但會偏移——確保它確實產生可量測的差異。

    這個測試存在的理由是防止有人「簡化」掉 EOP 相依：
    若哪天 EOP 被無聲忽略，此測試會失敗。
    """
    t, r, v = sample
    mjd = np.arange(61000, 61600, dtype=float)
    with_eop = pd.DataFrame({"X": 0.2225, "Y": 0.3520, "UT1-UTC": -0.1856},
                            index=pd.Index(mjd, name="MJD"))
    zero_eop = pd.DataFrame({"X": 0.0, "Y": 0.0, "UT1-UTC": 0.0},
                            index=pd.Index(mjd, name="MJD"))
    r_a, _ = itrf_to_teme(r, v, t, with_eop)
    r_b, _ = itrf_to_teme(r, v, t, zero_eop)
    diff_m = float(np.linalg.norm(r_a - r_b)) * 1000.0
    # ΔUT1 取 2021–2027 的最壞值 −0.1856 s，在 LEO 應造成數十公尺
    assert 30.0 < diff_m < 300.0


def test_ric_basis_is_orthonormal_and_signed(eop):
    """RIC 分解的方向約定：徑向朝外、法向沿角動量。"""
    r_ref = np.array([[7000.0, 0.0, 0.0]])
    v_ref = np.array([[0.0, 7.5, 0.0]])                 # 沿 +y 前進 → 角動量沿 +z
    assert ric(r_ref, v_ref, r_ref + np.array([[1.0, 0, 0]]))[0] == pytest.approx(
        [1.0, 0.0, 0.0])
    assert ric(r_ref, v_ref, r_ref + np.array([[0, 1.0, 0]]))[0] == pytest.approx(
        [0.0, 1.0, 0.0])
    assert ric(r_ref, v_ref, r_ref + np.array([[0, 0, 1.0]]))[0] == pytest.approx(
        [0.0, 0.0, 1.0])


def test_ric_with_earth_fixed_velocity_leaks_into_cross_track():
    """以地固速度建 RIC 基底會把沿跡誤差洩漏到法向。

    這說明 ric() docstring 的警告不是空話：同一個位置差，
    用錯速度會得到不同的分量歸屬。

    構造刻意只改動速度、不動框架——兩者都在同一慣性框架內，
    差別僅在其中一個少了 ω×r。若把 ITRF 速度直接餵進來，
    測到的會是 GMST 旋轉（數十度），那是另一個錯誤，不是這裡要守的。
    """
    r = np.array([[6480.000291, 1941.290815, -1618.051741]])
    v_eci = np.array([[-1.46454, 6.98688, 2.52771]])
    v_ecef = v_eci - np.cross([0.0, 0.0, OMEGA], r)

    def cross_axis(rr, vv):
        c = np.cross(rr, vv)
        return (c / np.linalg.norm(c))[0]

    angle_deg = np.degrees(np.arccos(np.clip(
        np.dot(cross_axis(r, v_eci), cross_axis(r, v_ecef)), -1.0, 1.0)))
    assert angle_deg == pytest.approx(1.36, abs=0.1)     # 基底確實偏轉

    offset = r + np.array([[0.5, 0.5, 0.5]])
    good = ric(r, v_eci, offset)[0]
    bad = ric(r, v_ecef, offset)[0]
    assert good[0] == pytest.approx(bad[0], abs=1e-9)    # 徑向不受影響
    assert abs(good[2] - bad[2]) * 1000.0 > 5.0          # 法向被洩漏數公尺
