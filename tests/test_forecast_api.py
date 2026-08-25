"""`/v1/forecast`：預報值必須與該 horizon 的實測技巧綁在一起。

只回傳「Kp 3.2」而不回傳「這個 horizon 的誤警率 0.52、中位提前量 0 小時」，
呼叫端無從判斷該不該據以行動——構想書把命中率、誤警率、提前量、可信度
四項並列為 KPI，就是為了不讓預報值單獨旅行。

另一條守的是 horizon 的還原：觀測的 valid_time 可能落在格間（SWPC 估計 Kp
標在 00:05），不對齊到格點的話 3 小時預報會被還原成 2.92 小時，技巧查表查不到
——差五分鐘讓整組 KPI 從回應裡消失，而且不會報錯。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from services.api.app import create_app
from services.forecast.skill import horizon_entry, load_skill, skill_models, write_skill
from swx_core import SwxStore, normalize

ISSUED = pd.Timestamp("2026-08-18T00:00:00Z")


@pytest.fixture()
def app(tmp_path, monkeypatch):
    store = SwxStore(tmp_path / "data")
    rows = [
        # 觀測：刻意標在格間的 00:05，模擬 SWPC 估計 Kp
        {"valid_time": ISSUED + pd.Timedelta(minutes=5), "param_code": "KP_3H",
         "value": 2.0, "unit": "1", "source_id": "swpc_kp_estimated",
         "source_tier": 1, "data_type": "OBS"},
        {"valid_time": ISSUED + pd.Timedelta(hours=6), "param_code": "KP_3H",
         "value": 4.2, "unit": "1", "source_id": "swx_forecast",
         "source_tier": 1, "data_type": "FCS", "confidence": 0.6},
        {"valid_time": ISSUED + pd.Timedelta(hours=6), "param_code": "KP_STORM_PROB",
         "value": 0.31, "unit": "1", "source_id": "swx_forecast",
         "source_tier": 1, "data_type": "FCS", "confidence": 0.6},
    ]
    store.write(normalize(pd.DataFrame(rows)), source_id="test")

    skill = tmp_path / "forecast_skill.json"
    write_skill("kp", {"6": {"samples": 100, "event_rate": 0.03, "confidence": 0.6,
                             "models": [
                                 {"model": "persistence", "tier": 0, "MAE": 0.874,
                                  "POD": 0.394, "FAR": 0.59, "BSS": -0.107,
                                  "ep_recall": 0.319, "lead_h_med": 0.0, "lead_n": 59},
                                 {"model": "gbm", "tier": 2, "MAE": 0.809,
                                  "POD": 0.173, "FAR": 0.515, "BSS": 0.117,
                                  "ep_recall": 0.178, "lead_h_med": 0.0, "lead_n": 33},
                             ]}},
               {"label": "Kp", "splits": 4, "generated_utc": "2026-08-25T00:00:00Z"},
               path=skill)
    monkeypatch.setattr("services.forecast.skill.SKILL_PATH", skill)
    return create_app(store)


def _body(app, query=""):
    r = app.test_client().get(f"/v1/forecast{query}")
    assert r.status_code == 200, r.data
    return json.loads(r.data)


def test_horizon_is_floored_to_the_target_grid(app):
    """起報錨點要對齊格點，否則 horizon 還原成 2.92 小時。"""
    body = _body(app, "?target=kp")
    assert body["issued_utc"] == "2026-08-18T00:00:00Z"
    assert body["latest_observation_utc"] == "2026-08-18T00:05:00Z"
    assert [f["horizon_h"] for f in body["forecasts"]] == [6.0]


def test_each_forecast_carries_its_measured_skill(app):
    body = _body(app, "?target=kp")
    row = body["forecasts"][0]
    assert row["value"] == pytest.approx(4.2)
    assert row["storm_probability"] == pytest.approx(0.31)
    assert row["confidence"] == 0.6
    assert row["skill"]["model"] == "gbm"
    assert row["skill"]["FAR"] == 0.515
    assert row["skill"]["lead_h_med"] == 0.0


def test_baseline_is_returned_alongside_the_shipped_model(app):
    """基線一併回傳，否則呼叫端會誤以為上線模型全面較優。

    此例正是那個情況：gbm 的 MAE 與誤警率較好，但**基線的命中率更高**。
    """
    row = _body(app, "?target=kp")["forecasts"][0]
    assert row["skill_baseline"]["model"] == "persistence"
    assert row["skill_baseline"]["POD"] > row["skill"]["POD"]


def test_advisory_is_always_attached(app):
    body = _body(app, "?target=kp")
    assert body["advisory"]["code"] == "RESEARCH_GRADE_FORECAST"
    assert body["advisory"]["not_for_operational_use_beyond_h"] == 12


def test_unknown_target_is_rejected(app):
    r = app.test_client().get("/v1/forecast?target=dst")
    assert r.status_code == 404
    assert "hp30" in json.loads(r.data)["available"]


def test_missing_skill_file_degrades_to_null_not_zero(app, monkeypatch, tmp_path):
    """成績檔缺席時要回 null。回 0 會被讀成「命中率 0」而非「沒有量過」。"""
    monkeypatch.setattr("services.forecast.skill.SKILL_PATH", tmp_path / "nope.json")
    row = _body(app, "?target=kp")["forecasts"][0]
    assert row["skill"] is None
    assert row["skill_baseline"] is None
    assert row["value"] == pytest.approx(4.2)      # 預報本身仍要給


def test_skill_models_prefers_lowest_mae_and_reports_ties():
    """ML 未勝出時，上線模型與最佳基線會是同一列——那正是要看見的事實。"""
    entry = {"models": [{"model": "climatology", "tier": 0, "MAE": 1.0},
                        {"model": "gbm", "tier": 2, "MAE": 1.2}]}
    best, baseline = skill_models(entry)
    assert best["model"] == "climatology"
    assert baseline["model"] == "climatology"


def test_horizon_entry_accepts_int_and_float_hours(tmp_path):
    doc = load_skill()
    assert horizon_entry(doc, "kp", 6) == horizon_entry(doc, "kp", 6.0)


def test_only_the_latest_issue_batch_is_returned(tmp_path, monkeypatch):
    """資料層累積每一次起報；混批會產生不存在於任何產品的 horizon。

    真實症狀：上週錨點的 6 小時預報與本週的 1 小時預報同列，
    還原出 15.5 小時這種值——而 15.5 小時從來不是一個產品。
    """
    from services.forecast.skill import latest_forecast_batch

    store = SwxStore(tmp_path / "data")
    old_batch = pd.Timestamp("2026-08-10T00:00:00Z")
    for stamp, valid, value in ((old_batch, ISSUED - pd.Timedelta(hours=3), 1.0),
                                (None, ISSUED + pd.Timedelta(hours=6), 4.2)):
        rows = [{"valid_time": valid, "param_code": "KP_3H", "value": value,
                 "unit": "1", "source_id": "swx_forecast", "source_tier": 1,
                 "data_type": "FCS", "confidence": 0.6}]
        store.write(normalize(pd.DataFrame(rows)), source_id="swx_forecast",
                    ingest_time=stamp)

    fcs = store.query("KP_3H")
    fcs = fcs[fcs["source_id"] == "swx_forecast"]
    assert len(fcs) == 2
    kept = latest_forecast_batch(fcs)
    assert len(kept) == 1
    assert kept.iloc[0]["value"] == pytest.approx(4.2)


def test_latest_forecast_batch_tolerates_empty_input():
    from services.forecast.skill import latest_forecast_batch

    empty = pd.DataFrame(columns=["valid_time", "ingest_time", "value"])
    assert latest_forecast_batch(empty).empty
    assert latest_forecast_batch(None) is None
