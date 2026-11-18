"""事件卡通報（transactional outbox）的契約測試。架構書 §11、§16.2 G7。

守的事：
  · 規則裡的每個通報角色都有路由——否則要到真的發事件時才發現送不出去。
  · 只有人工發布才通報；發布與待送訊息同生同滅（同一交易）。
  · 送不出去會重試、留痕、最後標 failed——絕不靜默消失。
  · 示範資料觸發的通報在訊息本身標明 is_demo。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from services.api.app import create_app
from services.notify import dispatch
from services.notify.routing import UNROUTED, Channel, Routing, load_routing
from services.risk_engine.engine import load_rules
from services.risk_engine.eventcard import (
    NOTIFY_FAILED, NOTIFY_PENDING, NOTIFY_SENDING, NOTIFY_SENT, STATUS_DRAFT,
    EventCard, EventStore,
)
from swx_core import SwxStore

ONSET = pd.Timestamp("2024-05-10T17:00:00Z")


def _card(notify=("通信單位", "值班主管"), event_id="EVT-N-1") -> EventCard:
    return EventCard(event_id=event_id, event_type="GEOMAGNETIC_STORM", mission_level="L3",
                     onset_utc=ONSET, end_utc=None, peak_utc=None, confidence=0.9,
                     international_scale="G3", notify=list(notify),
                     recommendations=["延後高精度作業"])


def _routing(**kw) -> Routing:
    return Routing(
        channels={"f": Channel("f", "file", {"dir": "exports/notifications"}),
                  "w": Channel("w", "webhook", {"url_env": "TEST_SWX_HOOK"})},
        recipients={"通信單位": ("f",), "值班主管": ("f", "w")},
        **{"max_attempts": 3, "backoff_s": 0.0, **kw},
    )


@pytest.fixture
def es(tmp_path, monkeypatch):
    monkeypatch.setenv("SWX_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TEST_SWX_HOOK", raising=False)
    store = EventStore()
    store.upsert(_card())
    return store


# ── 路由設定 ────────────────────────────────────────────────────────────
def test_every_rule_recipient_is_routed():
    routing = load_routing()
    used = {who for r in load_rules() for who in r.notify}
    missing = sorted(used - set(routing.recipients))
    assert not missing, f"規則用到但 configs/notify.yaml 未定義的角色：{missing}"


def test_webhook_url_must_come_from_env(tmp_path):
    p = tmp_path / "notify.yaml"
    p.write_text("channels:\n  h: {type: webhook, url: 'https://x/?token=1'}\n",
                 encoding="utf-8")
    with pytest.raises(ValueError, match="url_env"):
        load_routing(p)


# ── 發布即入佇列 ────────────────────────────────────────────────────────
def test_draft_does_not_notify(es):
    assert es.notifications() == []


def test_issue_enqueues_one_message_per_recipient_channel(es):
    assert es.issue("EVT-N-1", actor="duty-A", routing=_routing())
    msgs = es.notifications("EVT-N-1")
    pairs = sorted((m["recipient"], m["channel"]) for m in msgs)
    assert pairs == [("值班主管", "f"), ("值班主管", "w"), ("通信單位", "f")]
    assert {m["status"] for m in msgs} == {NOTIFY_PENDING}


def test_unrouted_recipient_is_visible_failure_not_silent_drop(es, tmp_path):
    es.upsert(_card(notify=("不存在的單位",), event_id="EVT-N-2"))
    es.issue("EVT-N-2", actor="duty-A", routing=_routing())
    (m,) = es.notifications("EVT-N-2")
    assert m["channel"] == UNROUTED and m["status"] == NOTIFY_FAILED
    assert "不存在的單位" in m["last_error"]


def test_issue_and_enqueue_are_one_transaction(es):
    class Broken(Routing):
        def channels_for(self, recipient):
            raise RuntimeError("routing exploded")

    with pytest.raises(RuntimeError):
        es.issue("EVT-N-1", actor="duty-A", routing=Broken(channels={}, recipients={}))
    # 發布被回滾：仍是 draft、沒有待送訊息，可以修好路由後重新發布
    assert es.latest("EVT-N-1")["status"] == STATUS_DRAFT
    assert es.notifications() == []


# ── 投遞 ────────────────────────────────────────────────────────────────
def test_file_channel_delivers_and_marks_demo_origin(es, tmp_path):
    es.upsert(_card(notify=("通信單位",), event_id="EVT-N-3"))
    es.issue("EVT-N-3", actor="duty-A", routing=_routing())
    res = dispatch.run_once(es, _routing())
    assert res.sent == 1

    files = list((tmp_path / "exports" / "notifications").rglob("*.json"))
    assert len(files) == 1
    body = json.loads(files[0].read_text(encoding="utf-8"))
    assert body["event_id"] == "EVT-N-3" and body["issued_by"] == "duty-A"
    assert body["mission_level"] == "L3"
    assert "is_demo" in body and body["operational"] is False

    (m,) = es.notifications("EVT-N-3")
    assert m["status"] == NOTIFY_SENT and m["delivery_s"] is not None
    trail = es.audit_trail()
    assert "notification_sent" in set(trail["action"])


def test_webhook_without_url_retries_then_fails(es):
    es.issue("EVT-N-1", actor="duty-A", routing=_routing())
    r = _routing(max_attempts=2)
    first = dispatch.run_once(es, r)
    assert first.retry == 1 and first.sent == 2
    second = dispatch.run_once(es, r)
    assert second.failed == 1
    (w,) = [m for m in es.notifications("EVT-N-1") if m["channel"] == "w"]
    assert w["status"] == NOTIFY_FAILED and w["attempts"] == 2
    assert "TEST_SWX_HOOK" in w["last_error"]
    assert "notification_failed" in set(es.audit_trail()["action"])


def test_webhook_success_sends_idempotency_key(es, monkeypatch):
    monkeypatch.setenv("TEST_SWX_HOOK", "http://notify-gw.internal/hook")
    seen = {}

    class Resp:
        status_code = 202

    def fake_post(url, json=None, timeout=None, headers=None):
        seen.update(url=url, body=json, headers=headers)
        return Resp()

    monkeypatch.setattr(dispatch.requests, "post", fake_post)
    es.issue("EVT-N-1", actor="duty-A", routing=_routing())
    assert dispatch.run_once(es, _routing()).sent == 3
    assert seen["headers"]["Idempotency-Key"] == "EVT-N-1@r1/值班主管"
    assert seen["body"]["recipient"] == "值班主管"


def test_stale_sending_lease_is_reclaimed(es):
    es.issue("EVT-N-1", actor="duty-A", routing=_routing())
    claimed = es.claim_due(lease_s=60)
    assert len(claimed) == 3
    assert es.claim_due() == []                        # 租約內不會重複取出
    later = datetime.now(timezone.utc) + timedelta(seconds=120)
    again = es.claim_due(now=later)                   # 投遞程序當掉：租約到期後收回
    assert len(again) == 3
    assert {m["status"] for m in es.notifications()} == {NOTIFY_SENDING}


# ── API ────────────────────────────────────────────────────────────────
def test_api_reports_issue_latency_and_delivery(es, tmp_path, monkeypatch):
    monkeypatch.setenv("SWX_API_ISSUE_TOKEN", "t")
    client = create_app(SwxStore(tmp_path)).test_client()
    r = client.post("/v1/events/EVT-N-1/issue",
                    headers={"Authorization": "Bearer t", "X-SWX-Actor": "duty-A"})
    assert r.status_code == 200

    body = client.get("/v1/events/EVT-N-1/notifications").get_json()
    assert body["status"] == "issued"
    assert body["issue_latency_s"] > 0                 # 回放事件：起始在過去
    assert body["summary"] == {NOTIFY_PENDING: len(body["messages"])}
    assert all("payload" not in m for m in body["messages"])
    assert client.get("/v1/events/NO-SUCH/notifications").status_code == 404
