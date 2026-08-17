"""熱氣層密度測試（議題四核心產品）。

守三件事：
  1. 暴時 ap 模式確實生效——MSIS 只在 geomagnetic_activity=-1 時讀 3 小時 ap 歷史
  2. ap 歷史只取當下之前的值（物理層不得有前視洩漏）
  3. 平靜基準必須連 ap 歷史一起換掉——只換日均值會讓修正倍率恆為 1，
     且產品看起來仍正常運作，屬於不易察覺的失效
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from orbit_drag import density, density_ratio
from orbit_drag.atmospheric import build_ap_history


def make_sw(ap_daily: float, ap3_values: list[float], *, f107: float = 180.0) -> pd.DataFrame:
    days = pd.date_range("2024-05-08", periods=7, freq="D", tz="UTC")
    sw = pd.DataFrame({"f107": f107, "f107a": f107, "ap": ap_daily}, index=days)
    idx = pd.date_range("2024-05-08", periods=len(ap3_values), freq="3h", tz="UTC")
    sw.attrs["ap3"] = pd.Series(ap3_values, index=idx, dtype=float)
    return sw


def test_ap_history_has_seven_elements_in_msis_order():
    sw = make_sw(100.0, list(np.linspace(5, 300, 40)))
    epochs = pd.DatetimeIndex(["2024-05-11T00:00Z"])
    aps = build_ap_history(epochs, sw)
    assert aps is not None and aps.shape == (1, 7)
    assert aps[0, 0] == pytest.approx(100.0), "第 0 元素必須是日均 Ap"


def test_ap_history_never_looks_ahead():
    """核心不變式：ap 歷史只能取當下之前的值。

    構造一個「前段極平靜、後段極擾動」的序列，在轉折前取樣，
    7 個元素都不該沾到後段的高值。
    """
    quiet, storm = [4.0] * 24, [400.0] * 24
    sw = make_sw(4.0, quiet + storm)
    boundary = sw.attrs["ap3"].index[24]
    aps = build_ap_history(pd.DatetimeIndex([boundary - pd.Timedelta(hours=1)]), sw)
    assert aps.max() <= 4.0 + 1e-9, f"取樣點之前全為平靜，卻讀到 {aps.max()}"


def test_storm_time_mode_differs_from_daily_mode():
    """暴時模式若沒生效，兩者會完全相同——這個測試就是在確認它真的有作用。"""
    sw = make_sw(105.0, [4.0] * 20 + [400.0] * 20)
    epochs = pd.date_range("2024-05-08", periods=8, freq="3h", tz="UTC")
    alt = [400.0] * len(epochs)
    storm = density(epochs, alt, sw, storm_time=True)
    daily = density(epochs, alt, sw, storm_time=False)
    assert np.isfinite(storm).all() and np.isfinite(daily).all()
    rel = np.abs(storm / daily - 1.0)
    assert rel.max() > 0.05, "暴時模式與日均模式差異過小，可能未實際生效"


def test_falls_back_to_daily_when_no_ap3():
    sw = pd.DataFrame(
        {"f107": 180.0, "f107a": 180.0, "ap": 50.0},
        index=pd.date_range("2024-05-08", periods=3, freq="D", tz="UTC"),
    )
    epochs = pd.DatetimeIndex(["2024-05-09T00:00Z"])
    assert build_ap_history(epochs, sw) is None
    rho = density(epochs, [400.0], sw)
    assert np.isfinite(rho).all(), "無 3 小時 ap 時應退回日均模式而非失敗"


def test_calm_baseline_replaces_ap_history_too():
    """平靜基準若只換日均 Ap、沒換 ap 歷史，暴時模式會讓比值恆為 1。

    這個錯誤不會拋例外、產品照樣輸出，只是修正因子全部變成 1.00——
    屬於不易察覺的失效，必須有測試守住。
    """
    sw = make_sw(271.0, [4.0] * 8 + [300.0] * 24)
    epochs = pd.date_range("2024-05-09", periods=6, freq="3h", tz="UTC")
    out = density_ratio(epochs, 400.0, sw=sw)
    assert (out["storm_ratio"] > 1.05).any(), (
        f"擾動期 storm_ratio 應明顯大於 1，實得最大 {out['storm_ratio'].max():.3f}"
        "——平靜基準可能沿用了擾動的 ap 歷史"
    )


def test_density_ratio_separates_solar_cycle_from_storm():
    """storm_ratio 與 ratio_vs_solar_min 用途不同，不可混用。"""
    sw = make_sw(271.0, [300.0] * 32, f107=230.0)
    epochs = pd.date_range("2024-05-09", periods=4, freq="3h", tz="UTC")
    out = density_ratio(epochs, 400.0, sw=sw)
    assert (out["ratio_vs_solar_min"] > out["storm_ratio"]).all(), (
        "對太陽極小的比值必然大於暴時比值；若否，代表兩個基準被算混了"
    )


def test_density_decreases_with_altitude():
    sw = make_sw(10.0, [10.0] * 24)
    epochs = pd.DatetimeIndex(["2024-05-09T00:00Z"] * 4)
    rho = density(epochs, [300.0, 400.0, 500.0, 600.0], sw)
    assert (np.diff(rho) < 0).all(), f"密度應隨高度遞減：{rho}"
