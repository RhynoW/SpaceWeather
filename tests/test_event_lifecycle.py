"""事件卡生命週期與判定依據的契約測試。

README 宣稱兩件事，這裡把它們變成會紅燈的測試：

  1. 事件卡有狀態機 draft → issued → superseded，且 L3 以上須經人工確認發布。
  2. `inference` 永遠是明確列舉值，**絕不為 null**——
     null 對呼叫端有歧義（可能是「直接觀測」「欄位缺失」「未計算」），
     而誤讀成「已觀測證實」是其中最危險的一種。
"""

from __future__ import annotations

import pandas as pd
import pytest

from services.risk_engine.engine import (
    INFERENCE_MODELLED,
    INFERENCE_OBSERVED,
    INFERENCE_PROXY,
    INFERENCE_UNAVAILABLE,
    INFERENCE_VALUES,
    Episode,
    _domain_inference,
    classify_inference,
)
from services.risk_engine.eventcard import (
    STATUS_DRAFT,
    STATUS_ISSUED,
    STATUS_SUPERSEDED,
    EventStore,
)


# ── 判定依據 ────────────────────────────────────────────────────────────
def test_inference_is_never_null():
    for param in ("KP_3H", "RHO_RATIO", "TEC", "NO_SUCH_PARAM"):
        for declared in (None, "proxy"):
            got = classify_inference(param, declared)
            assert got is not None
            assert got in INFERENCE_VALUES, f"{param}/{declared} 產生非列舉值 {got}"


def test_inference_classification_rules():
    assert classify_inference("KP_3H", None) == INFERENCE_OBSERVED
    assert classify_inference("RHO_RATIO", None) == INFERENCE_MODELLED
    assert classify_inference("X_FLARE_PROB", None) == INFERENCE_MODELLED
    # 規則自身的宣告優先於參數性質
    assert classify_inference("KP_3H", "proxy") == INFERENCE_PROXY


def _ep(inference: str) -> Episode:
    t = pd.Timestamp("2024-05-10", tz="UTC")
    return Episode(rule_id="R", domain="D", level="L2", start=t, end=t,
                   peak_value=5.0, peak_time=t, peak_param="KP_3H",
                   n_samples=1, inference=inference)


def test_domain_inference_takes_the_weakest_not_the_strongest():
    """任一分項是推估，整個網域就不能宣稱為直接觀測所得。"""
    eps = [_ep(INFERENCE_OBSERVED), _ep(INFERENCE_PROXY)]
    assert _domain_inference(eps, has_data=True) == INFERENCE_PROXY


def test_domain_without_data_is_unavailable_not_observed():
    """無資料要回 unavailable——這與「L0 沒事」是不同的意思。"""
    assert _domain_inference([], has_data=False) == INFERENCE_UNAVAILABLE


# ── 事件卡狀態機 ────────────────────────────────────────────────────────
@pytest.fixture
def card_store(tmp_path):
    return EventStore(tmp_path / "ops.sqlite")


def _card(store, level="L3", event_id="SWX-TEST-0001"):
    from services.risk_engine.eventcard import EventCard

    t = pd.Timestamp("2024-05-10T00:00:00Z")
    card = EventCard(
        event_id=event_id, event_type="GEOMAGNETIC_STORM", mission_level=level,
        onset_utc=t, end_utc=None, peak_utc=None, confidence=0.85, revision=0,
    )
    return store.upsert(card, actor="system")


def test_new_card_starts_as_draft(card_store):
    """新事件卡一律 draft——L3 以上未經人工確認不得視為已發布。"""
    card = _card(card_store)
    assert card.status == STATUS_DRAFT


def test_issue_transitions_draft_to_issued_and_records_actor(card_store):
    _card(card_store)
    assert card_store.issue("SWX-TEST-0001", actor="值勤官A") is True
    got = card_store.latest("SWX-TEST-0001")
    assert got["status"] == STATUS_ISSUED
    trail = card_store.audit_trail()
    assert (trail["action"] == "issue_event_card").any(), "發布未留下稽核紀錄"
    assert "值勤官A" in set(trail["actor"]), "稽核紀錄未記下發布者"


def test_issue_is_not_repeatable(card_store):
    """已發布的卡不能再被發布一次，避免狀態機被繞過。"""
    _card(card_store)
    assert card_store.issue("SWX-TEST-0001", actor="A") is True
    assert card_store.issue("SWX-TEST-0001", actor="B") is False


def test_content_change_creates_revision_and_supersedes_previous(card_store):
    from services.risk_engine.eventcard import EventCard

    _card(card_store)
    t = pd.Timestamp("2024-05-10T00:00:00Z")
    updated = EventCard(
        event_id="SWX-TEST-0001", event_type="GEOMAGNETIC_STORM", mission_level="L4",
        onset_utc=t, end_utc=None, peak_utc=None, confidence=0.9, revision=0,
    )
    new = card_store.upsert(updated, actor="system")
    assert new.revision == 1
    history = card_store.history("SWX-TEST-0001")
    prior = [h for h in history if h["revision"] == 0]
    assert prior and prior[0]["status"] == STATUS_SUPERSEDED
