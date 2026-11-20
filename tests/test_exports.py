"""CSV／GeoJSON 匯出：格式約定與幾何規則。

守的是使用者拿到檔案後不會被誤導的性質：
  · CSV 有 BOM（Excel 開中文不亂碼）、時間為 ISO UTC、觀測與預報分得開
  · 不認得的 format 回 400，而不是默默給 JSON
  · 極光邊界逐經度取最靠赤道的格點、缺口斷線不插值、經度轉成 −180..180
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from services import products
from services.api.app import create_app
from services.exporter.aurora_geojson import boundary_from_payload
from swx_core import SwxStore, normalize


def _grid(cells: dict[tuple[float, float], float]) -> dict:
    """360×181 全零格網，指定格點設機率。cells: {(lon 0..359, lat): prob}"""
    coords = [[lon, lat, cells.get((lon, lat), 0.0)]
              for lon in range(360) for lat in range(-90, 91)]
    return {"Observation Time": "2026-11-19T22:00:00Z",
            "Forecast Time": "2026-11-19T23:00:00Z", "coordinates": coords}


def test_aurora_boundary_picks_equatorward_cell_and_leaves_gaps_open():
    cells = {}
    for lon in [x for x in range(0, 100) if not 20 <= x <= 29]:   # 0–99°E 中挖掉 20–29°
        cells[(lon, 60)] = 30.0      # 邊界 60°N（另有更高緯的格點不得被選）
        cells[(lon, 70)] = 80.0
    for lon in range(200, 360):         # 160°W–1°W：邊界 55°N；100–199° 無格點 → 缺口
        cells[(lon, 55)] = 20.0
    for lon in range(0, 50):            # 南半球：最靠赤道 = 最高南緯 −58
        cells[(lon, -58)] = 15.0
        cells[(lon, -70)] = 90.0
    cells[(150, 40)] = 5.0              # 低於門檻，不得算進邊界

    fc = boundary_from_payload(_grid(cells), threshold=10.0)
    north, south = fc["features"]
    segs = north["geometry"]["coordinates"]
    pts = [p for s in segs for p in s]
    assert all(-180 <= lon <= 180 for lon, _ in pts), "GeoJSON 經度須在 −180..180"
    assert {lat for _, lat in pts} == {55.0, 60.0}
    assert north["properties"]["most_equatorward_lat"] == 55.0
    assert north["properties"]["longitudes_with_boundary"] == 250
    # 缺口不得被補線：20–29°E 與 100–199°（即 100..180、−180..−161）都沒有點
    assert not [p for p in pts if 20 <= p[0] <= 29 or 100 <= p[0] <= 180 or p[0] <= -161]
    # 中段缺口（20–29°E）必須真的斷開；兩端的缺口在 −180..180 排序下落在線的頭尾
    assert len(segs) == 2
    assert any(s[-1][0] == 19.0 for s in segs) and any(s[0][0] == 30.0 for s in segs)
    assert south["properties"]["most_equatorward_lat"] == -58.0
    assert fc["properties"]["data_type"].startswith("model forecast")


def test_aurora_boundary_without_lit_cells_has_null_geometry():
    fc = boundary_from_payload(_grid({}), threshold=10.0)
    assert all(f["geometry"] is None for f in fc["features"])
    json.dumps(fc)                       # 必須可序列化


def test_to_csv_conventions():
    df = pd.DataFrame({"valid_time": pd.to_datetime(["2026-11-19 22:00"], utc=True),
                       "名稱": ["測試"], "value": [1.5]})
    text = products.to_csv(df)
    assert text.startswith("﻿")
    assert "2026-11-19T22:00:00Z" in text and "名稱" in text


@pytest.fixture
def store(tmp_path):
    s = SwxStore(tmp_path)
    t = pd.date_range("2026-11-19T00:00Z", periods=3, freq="3h")
    s.write(normalize(pd.DataFrame({"valid_time": t, "param_code": "KP_3H", "value": [2.0, 3.0, 4.0],
                                    "unit": "1", "source_id": "gfz_nowcast", "source_tier": 1,
                                    "data_type": "OBS"})), source_id="gfz_nowcast")
    s.write(normalize(pd.DataFrame({"valid_time": [t[-1] + pd.Timedelta(hours=3)],
                                    "param_code": "KP_3H", "value": [3.3], "unit": "1",
                                    "source_id": "swx_forecast", "source_tier": 1,
                                    "data_type": "FCS"})), source_id="swx_forecast")
    raw = s.raw_root / "swpc_ovation" / "2026" / "11" / "19"
    raw.mkdir(parents=True)
    (raw / "20261119T220000Z.json").write_text(
        json.dumps(_grid({(lon, 62): 25.0 for lon in range(360)})), encoding="utf-8")
    return s


def test_api_obs_csv_keeps_types_and_flags_forecast(store):
    r = create_app(store).test_client().get("/v1/obs?param=KP_3H&format=csv")
    assert r.status_code == 200 and r.headers["Content-Type"].startswith("text/csv")
    assert r.headers.get("X-SWX-Advisory") == "RESEARCH_GRADE_FORECAST"
    rows = pd.read_csv(pd.io.common.StringIO(r.get_data(as_text=True).lstrip("﻿")))
    assert set(rows["data_type"]) == {"OBS", "FCS"}
    assert "attachment" in r.headers["Content-Disposition"]


def test_api_rejects_unknown_format(store):
    c = create_app(store).test_client()
    for url in ("/v1/obs?param=KP_3H&format=xml", "/v1/flares?format=xlsx",
                "/v1/drag/outlook?format=pdf", "/v1/exports/drag-correction?format=txt"):
        assert c.get(url).status_code == 400, url


def test_api_aurora_geojson(store, tmp_path):
    c = create_app(store).test_client()
    r = c.get("/v1/aurora/boundary")
    assert r.status_code == 200 and r.headers["Content-Type"] == "application/geo+json"
    fc = r.get_json()
    assert fc["type"] == "FeatureCollection"
    assert fc["features"][0]["properties"]["most_equatorward_lat"] == 62.0
    assert c.get("/v1/aurora/boundary?threshold=0").status_code == 400
    empty = create_app(SwxStore(tmp_path / "empty")).test_client().get("/v1/aurora/boundary")
    assert empty.status_code == 503 and empty.get_json()["error"]["code"] == "NO_DATA"


def test_drag_outlook_table_is_flat(monkeypatch):
    outlook = {
        "metadata": {"ballistic_coefficient_m2_kg": 0.012, "issued_utc": "2026-11-19T22:00:00+00:00"},
        "daily": [{"date_utc": "2026-11-20", "alt0_km": a, "storm_ratio": 1.1, "cum_drop_km": 0.1}
                  for a in (350.0, 450.0)],
        "drivers": [{"date_utc": "2026-11-20", "f107": 120.0, "ap": 8.0, "active": False}],
    }
    t = products.drag_outlook_table(outlook)
    assert len(t) == 2 and {"f107_forecast", "ap_forecast", "forecast_issued_utc"} <= set(t.columns)
