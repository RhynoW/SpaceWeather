"""30 日低軌阻力展望：物理方向、誤差帶、來源界線與 API 行為。

數值本身取決於 MSIS 與預報，不寫死；這裡守的是**不會錯方向**的性質——
高度越高掉越少、B 加倍掉的約加倍、寧靜基準不多於預報、誤差帶包住中央值、
活躍日的地磁增強大於平常日。任何一條反了，產品就是錯的。
"""

from __future__ import annotations

import pandas as pd
import pytest

from services import products
from services.api.app import create_app
from swx_core import SwxStore, normalize

NOW = pd.Timestamp("2026-11-20T00:00Z")


def _write(store, code, days, values, *, source, dtype, ingest="2026-11-19T22:00Z"):
    store.write(normalize(pd.DataFrame({
        "valid_time": days, "param_code": code, "value": values,
        "unit": "sfu" if code.startswith("F107") else "nT",
        "source_id": source, "source_tier": 1, "data_type": dtype,
    })), source_id=source, ingest_time=pd.Timestamp(ingest).to_pydatetime(), dedupe=False)


@pytest.fixture
def store(tmp_path):
    s = SwxStore(tmp_path)
    days = pd.date_range(NOW, periods=45, freq="D")
    ap = [8.0] * 45
    ap[7] = 40.0                       # 第 8 天一個活躍日
    _write(s, "F107_OBS", days, [120.0] * 45, source="swpc_45day_forecast", dtype="PRD")
    _write(s, "AP_AVG", days, ap, source="swpc_45day_forecast", dtype="PRD")
    _write(s, "F107_OBS_C81", [NOW - pd.Timedelta(days=1)], [118.0],
           source="celestrak_sw_all", dtype="OBS")
    # 27 日展望同日同參數、較晚入庫：不得混進 45 日驅動量
    _write(s, "AP_AVG", days[:27], [80.0] * 27, source="swpc_27day_outlook", dtype="PRD",
           ingest="2026-11-19T23:00Z")
    return s


def test_physics_directions(store):
    out = products.drag_outlook(store, now=NOW, bc=0.012)
    s = {r["alt0_km"]: r for r in out["summary"]}
    assert s[350.0]["alt_loss_km"] > s[450.0]["alt_loss_km"] > s[550.0]["alt_loss_km"] > 0
    for r in s.values():
        assert r["alt_loss_low_km"] < r["alt_loss_km"] < r["alt_loss_high_km"]
        assert r["alt_loss_quiet_km"] <= r["alt_loss_km"]
        assert 0 <= r["geomag_share"] < 1
        assert r["peak_date_utc"] == "2026-11-27", "活躍日應是增強倍率的峰值"
    assert len(out["drivers"]) == 30 and len(out["daily"]) == 90


def test_drop_scales_with_ballistic_coefficient(store):
    a = products.drag_outlook(store, now=NOW, bc=0.006)["summary"][2]["alt_loss_km"]
    b = products.drag_outlook(store, now=NOW, bc=0.012)["summary"][2]["alt_loss_km"]
    assert b / a == pytest.approx(2.0, rel=0.05)


def test_drivers_come_only_from_45day_forecast(store):
    """去重前限定來源：27 日展望（Ap 80）同日較晚入庫，也不得滲入。"""
    drv = products.drag_outlook(store, now=NOW)["drivers"]
    assert max(d["ap"] for d in drv) == 40.0
    assert [d["date_utc"] for d in drv if d["active"]] == ["2026-11-27"]


def test_missing_forecast_and_bad_input(tmp_path, store):
    with pytest.raises(products.NoForecastData):
        products.drag_outlook(SwxStore(tmp_path / "empty"), now=NOW)
    for kw in ({"days": 0}, {"days": 60}, {"bc": 1.0}, {"lat": 120.0}):
        with pytest.raises(ValueError):
            products.drag_outlook(store, now=NOW, **kw)


def test_api_drag_outlook(tmp_path, store, monkeypatch):
    monkeypatch.setattr(products._drag_outlook.pd.Timestamp, "now",
                        classmethod(lambda cls, tz=None: NOW))
    c = create_app(store).test_client()
    body = c.get("/v1/drag/outlook?days=10&bc=0.022").get_json()
    assert body["metadata"]["days"] == 10 and body["metadata"]["ballistic_coefficient_m2_kg"] == 0.022
    assert body["metadata"]["caveats"] and "data" in body
    assert c.get("/v1/drag/outlook?bc=abc").status_code == 400
    assert c.get("/v1/drag/outlook?bc=9").status_code == 400
    empty = create_app(SwxStore(tmp_path / "empty")).test_client().get("/v1/drag/outlook")
    assert empty.status_code == 503 and empty.get_json()["error"]["code"] == "NO_DATA"
