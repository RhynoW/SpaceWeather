"""驅動量目標（F10.7／Ap）與 45 日預報來源的契約。

這一組守的是三件在別處不會報錯、只會安靜給錯數字的事：

  1. 持續性基線必須持續**目標自己**，不是碰巧同名的另一個量；
  2. 沒有事件定義的目標不得印出 POD/FAR/BSS；
  3. SWPC 45 日預報的兩個區塊少一個時，另一個仍要進得來。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from services.forecast.features import (AP_TARGET, F107_TARGET, HP30_TARGET,
                                        KP_TARGET, TARGETS)
from services.forecast.models import PersistenceBaseline, default_models
from services.forecast.verify import evaluate


# ── 目標規格 ────────────────────────────────────────────────────────────
def test_driver_targets_have_no_invented_event_threshold():
    """F10.7 與 Ap 沒有任何作業單位公告的事件尺度，本案不得自創一個。"""
    for spec in (F107_TARGET, AP_TARGET):
        assert spec.storm_threshold is None, f"{spec.key} 被塞了一個自創門檻"
        assert spec.prob_code is None, "沒有事件定義卻宣告事件機率參數"
        assert not spec.has_events


def test_driver_horizons_reach_the_swpc_operational_length():
    """45 天是 SWPC 作業預報的長度；短於它就無法與作業基線同場比較。"""
    for spec in (F107_TARGET, AP_TARGET):
        assert max(spec.horizons) == 45 * 24
        assert spec.grid_h == 24.0


def test_horizon_label_uses_days_on_a_daily_grid():
    """1080 小時沒有人讀得出那是 45 天。"""
    assert F107_TARGET.horizon_label(1080) == "45 天"
    assert KP_TARGET.horizon_label(48) == "48 小時"


# ── 持續性基線：曾經悄悄持續了別的量 ────────────────────────────────────
def test_persistence_persists_the_target_not_whatever_column_exists():
    """`now_col` 必須跟著目標換。

    Hp30 與 F10.7 的特徵矩陣裡都有 `kp_3h_now`（Kp 是它們的輔助特徵），
    所以寫死 `kp_3h_now` 不會報錯——「持續性」會安靜地變成「持續 Kp」，
    而 skill_vs_persistence 拿它當分母。分母錯了，整欄技巧分數都錯。
    """
    assert KP_TARGET.now_col == "kp_3h_now"
    assert HP30_TARGET.now_col == "hp30_now"
    assert F107_TARGET.now_col == "f107_obs_now"
    assert AP_TARGET.now_col == "ap_avg_now"

    idx = pd.date_range("2026-01-01", periods=5, freq="30min", tz="UTC")
    X = pd.DataFrame({"kp_3h_now": [1.0] * 5, "hp30_now": [7.0] * 5}, index=idx)

    m = PersistenceBaseline()
    m.now_col = HP30_TARGET.now_col
    assert list(m.predict(X)) == [7.0] * 5, "持續了 Kp 而不是 Hp30"

    # 欄位不存在時要棄權（NaN），不得改用另一欄：安靜換一欄會照常印出
    # 一個「持續性」的分數，而它持續的是別的量。
    m.now_col = "not_here_now"
    assert np.isnan(m.predict(X)).all()


def test_default_models_wires_the_persistence_column():
    models = default_models(None, now_col=F107_TARGET.now_col)
    persistence = next(m for m in models if m.name == "persistence")
    assert persistence.now_col == "f107_obs_now"
    assert all(getattr(m, "storm_threshold", None) is None for m in models)


# ── 擂台：沒有事件定義就不得印事件指標 ──────────────────────────────────
def _toy(n: int = 900):
    idx = pd.date_range("2023-01-01", periods=n, freq="24h", tz="UTC")
    rng = np.random.default_rng(0)
    base = 120 + 30 * np.sin(np.arange(n) / 27.0) + rng.normal(0, 5, n)
    X = pd.DataFrame({"f107_obs_now": base,
                      "f107_recur27d": np.roll(base, 27),
                      "kp_3h_now": rng.normal(2, 1, n)}, index=idx)
    y = pd.Series(np.roll(base, -1), index=idx)
    return X, y


def test_continuous_target_reports_no_event_metrics():
    """硬給一個門檻，就會印出一整排看起來很專業、但沒有作業意義的數字。"""
    X, y = _toy()
    table = evaluate(default_models(None, now_col="f107_obs_now"), X, y,
                     n_splits=2, min_train_days=200, storm_threshold=None)
    ok = table[table["status"] == "ok"]
    assert not ok.empty
    for col in ("POD", "FAR", "CSI", "HSS", "BSS", "Brier", "ep_recall", "lead_h_med"):
        assert col not in table.columns, f"無事件定義卻報了 {col}"
    for col in ("MAE", "RMSE", "MAE_lo", "MAE_hi", "skill_vs_persistence"):
        assert col in table.columns


def test_event_target_still_reports_event_metrics():
    """對照組：有門檻時事件指標必須還在，證明上一條不是把功能關掉了。"""
    X, y = _toy()
    table = evaluate(default_models(140.0, now_col="f107_obs_now"), X, y,
                     n_splits=2, min_train_days=200, storm_threshold=140.0)
    assert "POD" in table.columns and "BSS" in table.columns


# ── SWPC 45 日預報 ──────────────────────────────────────────────────────
_SAMPLE = """:Product: 45 Day AP and F10.7cm Flux Forecast  45-day-forecast.txt
:Issued: 2026 Aug 28 0000 UTC
#
45-DAY AP FORECAST
28Aug26 034 29Aug26 025 30Aug26 016
31Aug26 012 01Sep26 006
45-DAY F10.7 CM FLUX FORECAST
28Aug26 122 29Aug26 125 30Aug26 120
31Aug26 110 01Sep26 120
FORECASTER:  AUTOMATED - SWPC Forecasting System
99999
"""


def _connector(monkeypatch=None):
    from swx_core import SourceSpec, SwxStore

    from services.ingest.forecast_sources import Swpc45DayForecastConnector

    spec = SourceSpec(
        source_id="swpc_45day_forecast", name="SWPC 45 day", connector="forecast_sources",
        tier=1, status="ready", provides=("F107_OBS", "AP_AVG"), cadence_s=86400,
        latency_budget_s=None, endpoint="https://example.invalid/45-day-forecast.txt",
        fmt="swpc_45day_txt", local_fallback=None, fallback=(), notes=None,
        publication_lag_s=0, raw={},
    )
    return Swpc45DayForecastConnector(spec, SwxStore.__new__(SwxStore))


def test_45day_parses_both_blocks_as_predictions():
    df = _connector().parse(_SAMPLE.encode())
    assert set(df["param_code"]) == {"AP_AVG", "F107_OBS"}
    assert set(df["data_type"]) == {"PRD"}, "來源自帶的預測必須標 PRD，不得混入觀測"
    assert len(df) == 10

    ap = df[df["param_code"] == "AP_AVG"].sort_values("valid_time")
    assert list(ap["value"]) == [34.0, 25.0, 16.0, 12.0, 6.0]
    assert f"{ap['valid_time'].iloc[0]:%Y-%m-%d}" == "2026-08-28"

    f107 = df[df["param_code"] == "F107_OBS"].sort_values("valid_time")
    assert list(f107["value"]) == [122.0, 125.0, 120.0, 110.0, 120.0]


def test_45day_keeps_one_block_when_the_other_is_missing():
    """少一個區塊就少半份預報。回空表會讓另一半也不見。"""
    text = _SAMPLE[:_SAMPLE.index("45-DAY F10.7")]
    conn = _connector()
    df = conn.parse(text.encode())
    assert set(df["param_code"]) == {"AP_AVG"}
    assert any("F10.7" in w for w in conn._warnings), "缺的區塊沒有出聲"


def test_all_targets_are_reachable_from_the_cli():
    assert set(TARGETS) == {"kp", "hp30", "f107", "ap"}
