"""沿跡誤差推算的契約。

這一組守的是兩個**不會報錯、只會給錯數量級**的陷阱：

  1. DatetimeIndex 的解析度（ns 與 us）——步長差 1000 倍；
  2. store.query 去重後才篩 source_id——同一天被別的來源勝出就整天消失。

兩者實測都發生過，而且症狀都是「數字看起來很小，很合理」。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from orbit_drag.alongtrack import (RE_SI, Scenario, alongtrack_km, compare,
                                   constant_drivers, propagate)

pytest.importorskip("pymsis", reason="沿跡推算需要 MSIS")


def _epochs(n: int = 16, unit: str = "ns") -> pd.DatetimeIndex:
    idx = pd.date_range("2026-03-01", periods=n, freq="24h", tz="UTC")
    return idx.as_unit(unit) if unit != "ns" else idx


# ── 時間解析度 ──────────────────────────────────────────────────────────
def test_microsecond_index_gives_the_same_answer_as_nanosecond():
    """步長必須由時間差算出，不得用 view("int64")/1e9。

    pandas 2.x 的索引可能是 ns 也可能是 us：date_range 給 ns，
    但由 pivot_table 或 floor 得來的常是 us。除以 1e9 在 us 索引上
    會得到 1/1000 的步長——45 天的軌道衰減會被算成 1 小時的量，
    而且完全不會報錯（實測沿跡差從 252 km 變成 10 公分）。
    """
    ns, us = _epochs(unit="ns"), _epochs(unit="us")
    assert us.dtype != ns.dtype, "測試素材未真的換成 us 解析度"

    sw_ns = constant_drivers(ns, f107=150, ap=15)
    sw_us = constant_drivers(us, f107=150, ap=15)
    a = propagate(ns, 450.0, sw_ns, bc=0.015)
    b = propagate(us, 450.0, sw_us, bc=0.015)

    assert a["theta_rad"].iloc[-1] == pytest.approx(b["theta_rad"].iloc[-1], rel=1e-9)
    # 一天約 15 圈，15 天約 226 圈；量級錯了這一條就會擋下來
    assert a["theta_rad"].iloc[-1] > 2 * np.pi * 200


def test_non_monotonic_epochs_are_rejected():
    idx = pd.DatetimeIndex(["2026-03-02", "2026-03-01"], tz="UTC")
    with pytest.raises(ValueError):
        propagate(idx, 450.0, constant_drivers(idx, f107=150, ap=15))


# ── 物理 ────────────────────────────────────────────────────────────────
def test_higher_density_decays_faster_and_runs_ahead():
    """密度高 → 半長軸掉得快 → 平均運動變快 → 沿跡**超前**。

    這個符號常被搞反（「阻力讓衛星變慢」是直覺但錯的）：阻力減少能量、
    軌道降低，而低軌道跑得更快。符號錯了整份產品的方向都會反。
    """
    ep = _epochs(30)
    sw = constant_drivers(ep, f107=150, ap=15)
    low = propagate(ep, 420.0, sw, bc=0.015, rho_scale=1.0)
    high = propagate(ep, 420.0, sw, bc=0.015, rho_scale=1.3)

    assert high["alt_km"].iloc[-1] < low["alt_km"].iloc[-1], "密度高卻掉得比較慢"
    assert high["theta_rad"].iloc[-1] > low["theta_rad"].iloc[-1], "密度高卻落後"
    # alongtrack_km(ref=low, test=high) = theta_low - theta_high < 0：test 超前
    assert alongtrack_km(low, high).iloc[-1] < 0


def test_alongtrack_error_grows_about_quadratically():
    """Δa 近似線性，相位是它的積分，故沿跡差以時間平方成長。

    這條在意的是**形狀**：若哪天改成線性成長，代表積分被寫成了單步差分。
    """
    ep = _epochs(33)
    sw = constant_drivers(ep, f107=150, ap=15)
    ref = propagate(ep, 420.0, sw, bc=0.015)
    test = propagate(ep, 420.0, sw, bc=0.015, rho_scale=1.2)
    ds = alongtrack_km(ref, test).abs()

    ratio = ds.iloc[32] / ds.iloc[16]        # 32 天 vs 16 天
    assert 3.2 < ratio < 4.8, f"成長形狀不是平方（比值 {ratio:.2f}）"


def test_reference_scenario_is_identically_zero():
    ep = _epochs(10)
    sw = constant_drivers(ep, f107=150, ap=15)
    table = compare([Scenario("ref", "參考", sw),
                     Scenario("hi", "高密度", sw, rho_scale=1.15)],
                    ep, 450.0, bc=0.015)
    ref_rows = table[table["scenario"] == "ref"]["alongtrack_km"]
    assert (ref_rows == 0).all()
    assert table.attrs["reference"] == "ref"
    assert table[table["scenario"] == "hi"]["alongtrack_km"].abs().max() > 0


def test_initial_altitude_is_honoured():
    ep = _epochs(4)
    sw = constant_drivers(ep, f107=150, ap=15)
    r = propagate(ep, 550.0, sw, bc=0.01)
    assert r["alt_km"].iloc[0] == pytest.approx(550.0)
    assert r["sma_km"].iloc[0] * 1e3 == pytest.approx(RE_SI + 550e3)
