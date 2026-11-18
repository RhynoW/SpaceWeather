"""作業面強化的契約測試：人工發布、稽核不可竄改、碎檔合併不改變查詢結果。

架構書寫「L3 以上須人工確認」與「稽核表 append-only」。在本檔之前，
前者只有程式內的 `EventStore.issue()`、沒有任何操作入口；後者只靠慣例。
這裡把兩者變成會紅燈的行為。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pandas as pd
import pytest

from services.api.app import create_app
from services.risk_engine.eventcard import STATUS_DRAFT, STATUS_ISSUED, EventCard, EventStore
from swx_core import SwxStore


def _card(event_id: str = "EVT-TEST-1") -> EventCard:
    now = pd.Timestamp("2024-05-10T17:00:00Z")
    return EventCard(
        event_id=event_id, event_type="GEOMAGNETIC_STORM", mission_level="L3",
        onset_utc=now, end_utc=None, peak_utc=None, confidence=0.9,
        international_scale="G3",
    )


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("SWX_DATA_DIR", str(tmp_path))
    app = create_app(SwxStore(tmp_path))
    EventStore().upsert(_card())
    return app.test_client()


# ── 人工發布 ────────────────────────────────────────────────────────────
def test_issue_disabled_without_token(api, monkeypatch):
    monkeypatch.delenv("SWX_API_ISSUE_TOKEN", raising=False)
    r = api.post("/v1/events/EVT-TEST-1/issue", headers={"X-SWX-Actor": "duty"})
    assert r.status_code == 403


def test_issue_rejects_wrong_token(api, monkeypatch):
    monkeypatch.setenv("SWX_API_ISSUE_TOKEN", "s3cret")
    r = api.post("/v1/events/EVT-TEST-1/issue",
                 headers={"Authorization": "Bearer nope", "X-SWX-Actor": "duty"})
    assert r.status_code == 401


def test_issue_requires_named_actor(api, monkeypatch):
    monkeypatch.setenv("SWX_API_ISSUE_TOKEN", "s3cret")
    r = api.post("/v1/events/EVT-TEST-1/issue", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 400


def test_issue_flow_and_audit(api, monkeypatch):
    monkeypatch.setenv("SWX_API_ISSUE_TOKEN", "s3cret")
    h = {"Authorization": "Bearer s3cret", "X-SWX-Actor": "duty-officer-A"}

    r = api.post("/v1/events/EVT-TEST-1/issue", headers=h)
    assert r.status_code == 200
    assert r.get_json()["status"] == STATUS_ISSUED
    assert api.get("/v1/events/EVT-TEST-1").get_json()["status"] == STATUS_ISSUED

    # 第二次發布：已非 draft
    assert api.post("/v1/events/EVT-TEST-1/issue", headers=h).status_code == 409
    assert api.post("/v1/events/NO-SUCH/issue", headers=h).status_code == 404

    trail = EventStore().audit_trail()
    issued = trail[trail["action"] == "issue_event_card"]
    assert issued["actor"].tolist() == ["duty-officer-A"]


# ── 稽核不可竄改 ────────────────────────────────────────────────────────
def test_audit_log_is_append_only_at_db_level(tmp_path):
    store = EventStore(tmp_path / "ops.sqlite")
    store.upsert(_card())
    con = sqlite3.connect(tmp_path / "ops.sqlite")
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            con.execute("UPDATE audit_log SET actor='someone-else'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            con.execute("DELETE FROM audit_log")
    finally:
        con.close()


def test_issue_requires_actor_in_library(tmp_path):
    store = EventStore(tmp_path / "ops.sqlite")
    store.upsert(_card())
    with pytest.raises(ValueError):
        store.issue("EVT-TEST-1", actor="  ")


# ── 碎檔合併 ────────────────────────────────────────────────────────────
def _obs(values, ingest: str) -> pd.DataFrame:
    t = pd.date_range("2026-01-01", periods=len(values), freq="3h", tz="UTC")
    return pd.DataFrame({
        "valid_time": t, "param_code": "KP_3H", "value": values, "unit": "",
        "source_id": "test", "source_tier": 1, "quality_flag": "good",
        "data_type": "OBS", "ingest_time": pd.Timestamp(ingest),
    })


def test_compact_preserves_query_and_replay(tmp_path):
    store = SwxStore(tmp_path)
    store.write(_obs([1.0, 2.0, 3.0], "2026-01-02T00:00Z"),
                ingest_time=datetime(2026, 1, 2, tzinfo=timezone.utc))
    store.write(_obs([1.0, 5.0, 3.0, 4.0], "2026-01-03T00:00Z"),
                ingest_time=datetime(2026, 1, 3, tzinfo=timezone.utc))
    store.write(_obs([1.0, 5.0, 6.0, 4.0], "2026-01-04T00:00Z"),
                ingest_time=datetime(2026, 1, 4, tzinfo=timezone.utc))

    as_of = datetime(2026, 1, 3, 12, tzinfo=timezone.utc)
    before_now = store.query("KP_3H")
    before_replay = store.query("KP_3H", as_of=as_of)
    files_before = int(store.storage_summary()["files"].sum())
    assert files_before == 3

    report = store.compact()
    assert report and report[0]["files_in"] == 3
    assert int(store.storage_summary()["files"].sum()) == 1

    pd.testing.assert_frame_equal(store.query("KP_3H"), before_now)
    pd.testing.assert_frame_equal(store.query("KP_3H", as_of=as_of), before_replay)
    assert before_replay["value"].tolist() == [1.0, 5.0, 3.0, 4.0]


def test_compact_dry_run_touches_nothing(tmp_path):
    store = SwxStore(tmp_path)
    store.write(_obs([1.0], "2026-01-02T00:00Z"),
                ingest_time=datetime(2026, 1, 2, tzinfo=timezone.utc))
    store.write(_obs([2.0], "2026-01-03T00:00Z"),
                ingest_time=datetime(2026, 1, 3, tzinfo=timezone.utc))
    store.compact(dry_run=True)
    assert int(store.storage_summary()["files"].sum()) == 2
