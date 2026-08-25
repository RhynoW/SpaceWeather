"""兩組預報目標（Kp 3 小時格點／Hp30 30 分鐘格點）。

構想書要求 1／3／6 小時三種產品。1 小時 horizon 不能建在 Kp 上——Kp 是 3 小時
指數，1 小時的預報只是同一個值換個說法，提前量也量不出來。所以預報層有兩組
目標，本檔守住三件事：

  1. Kp 這組的欄位名稱與換算**不得改變**——docs/forecast_verification.md 的
     數字在該設定下產生，欄名一動就不再可複現。
  2. Hp30 這組確實走在 30 分鐘格點上，1 小時 horizon 真的是往前 1 小時。
  3. 基線不會因為欄名對不上而**靜默棄權**（曾發生：復現基線寫死
     `kp_recur27d`，換目標後預測全 NaN，整個模型從成績表上消失）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from services.forecast.features import (HP30_TARGET, KP_TARGET, TARGETS,
                                        build_dataset, build_features)
from services.forecast.models import RecurrenceBaseline, default_models


def test_hp30_is_the_only_target_with_one_hour_horizon():
    assert 1 in HP30_TARGET.horizons
    assert 1 not in KP_TARGET.horizons, "Kp 為 3 小時指數，不應宣稱 1 小時 horizon"
    assert set(TARGETS) == {"kp", "hp30"}


@pytest.mark.parametrize(
    ("spec", "hours", "expected"),
    [
        (KP_TARGET, 3, 1), (KP_TARGET, 24, 8), (KP_TARGET, 27 * 24, 216),
        (HP30_TARGET, 0.5, 1), (HP30_TARGET, 1, 2), (HP30_TARGET, 24, 48),
    ],
)
def test_steps_conversion(spec, hours, expected):
    assert spec.steps(hours) == expected


def test_sub_grid_hours_never_round_down_to_zero():
    """比一格還短的時距至少算一格——回傳 0 會讓 shift(0) 洩漏當下的值。"""
    assert KP_TARGET.steps(0.5) == 1
    assert HP30_TARGET.steps(0.1) == 1


def _panel(spec, n=4000):
    idx = pd.date_range("2021-01-01", periods=n, freq=spec.grid, tz="UTC")
    rng = np.random.default_rng(7)
    cols = {spec.target_col: rng.uniform(0, 9, n)}
    for c in (*spec.geomag_cols, "sw_v", "sw_n", "imf_bz", "f107_obs"):
        cols.setdefault(c, rng.uniform(0, 9, n))
    return pd.DataFrame(cols, index=idx)


def test_kp_feature_names_are_frozen():
    """既有驗證報告依賴這些欄名，重構不得更動。"""
    f = build_features(_panel(KP_TARGET), KP_TARGET)
    for name in ("kp_3h_now", "kp_3h_lag3h", "kp_3h_lag48h", "kp_3h_max24h",
                 "kp_3h_mean168h", "kp_3h_trend24h", "dst_recovery", "dst_min72h",
                 "imf_bz_min24h", "sw_v_max24h", "newell_mean24h",
                 "kp_recur27d", "kp_recur27d_max24h", "kp_recur54d",
                 "f107", "f107_mean81d", "semiannual"):
        assert name in f.columns, name


def test_hp30_features_include_sub_hour_lags():
    """30 分鐘格點才看得見 L1 太陽風那 30–60 分鐘的先導期。"""
    f = build_features(_panel(HP30_TARGET), HP30_TARGET)
    assert "hp30_lag0.5h" in f.columns
    assert "hp30_lag1h" in f.columns
    assert "imf_bz_lag1h" in f.columns, "細格點才加的太陽風滯後特徵不見了"
    assert "hp30_recur27d" in f.columns
    # 3 小時格點不該有這些
    kp = build_features(_panel(KP_TARGET), KP_TARGET)
    assert not [c for c in kp.columns if c.endswith("lag0.5h")]
    assert "imf_bz_lag1h" not in kp.columns


def test_hp30_one_hour_target_is_two_grid_steps_ahead():
    """1 小時 horizon 必須真的是 t+1h 的值，不是 t+30min 也不是 t+3h。"""
    panel = _panel(HP30_TARGET, n=500)
    X, y, _ = build_dataset(panel, 1, spec=HP30_TARGET)
    t = X.index[10]
    assert y.loc[t] == pytest.approx(panel[HP30_TARGET.target_col].loc[t + pd.Timedelta(hours=1)])


def test_recurrence_baseline_follows_the_target_column():
    """欄名對不上時基線會全 NaN 而被擂台丟掉——那是靜默棄權，不是輸。"""
    for spec in (KP_TARGET, HP30_TARGET):
        f = build_features(_panel(spec), spec)
        pred = RecurrenceBaseline().fit(f, pd.Series(1.0, index=f.index)).predict(f)
        assert np.isfinite(pred).any(), f"{spec.key}: 復現基線在此目標上算不出任何值"


def test_default_models_carry_the_event_threshold():
    """門檻要一路傳到模型：模型學的標籤與擂台計分的標籤必須是同一個定義。"""
    for m in default_models(HP30_TARGET.storm_threshold):
        assert m.storm_threshold == HP30_TARGET.storm_threshold
