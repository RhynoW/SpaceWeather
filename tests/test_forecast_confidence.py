"""預報列 `confidence` 的值域與分層。

構想書把「可信度」與命中率、誤警率、提前量並列為預報 KPI，所以這個欄位
不能是佔位值。曾有的寫法是 `round(1 - abs(prob - 0.5) * 0 + 0.6, 2)`——
`* 0` 使機率項失效，結果恆為 1.6：既與預報無關，也超出 [0, 1]，
而資料層對 confidence 不做值域檢查，於是不會有任何一處報錯。
本測試把值域與單調性釘住，讓同類錯誤下次直接紅燈。
"""

from __future__ import annotations

import pytest

from services.forecast.features import HORIZONS, OPERATIONAL_HORIZON_LIMIT_H
from services.forecast.run import forecast_confidence


@pytest.mark.parametrize("horizon", HORIZONS)
def test_confidence_within_unit_interval(horizon):
    c = forecast_confidence(horizon)
    assert 0.0 <= c <= 1.0, f"horizon {horizon}h 的 confidence {c} 不在 [0, 1]"


def test_confidence_never_increases_with_horizon():
    """技巧隨 horizon 下降，可信度不得反向上升。"""
    values = [forecast_confidence(h) for h in sorted(HORIZONS)]
    assert values == sorted(values, reverse=True) or len(set(values)) == 1
    assert all(a >= b for a, b in zip(values, values[1:]))


def test_beyond_operational_limit_is_lower():
    """跨過作業界線後必須降級，否則 API 的 advisory 與資料欄位互相矛盾。"""
    inside = forecast_confidence(OPERATIONAL_HORIZON_LIMIT_H)
    outside = forecast_confidence(OPERATIONAL_HORIZON_LIMIT_H + 1)
    assert outside < inside


def test_operational_limit_matches_api_advisory():
    """`not_for_operational_use_beyond_h` 硬寫在 API，兩處不得各自漂移。"""
    from pathlib import Path

    src = Path("services/api/app.py").read_text(encoding="utf-8")
    assert f'"not_for_operational_use_beyond_h": {OPERATIONAL_HORIZON_LIMIT_H},' in src
