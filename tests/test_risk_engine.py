"""規則引擎測試（架構書 §9.1）。

守的是三個容易出錯、且錯了會直接影響作業的行為：
  駐留時間  單點雜訊不該觸發告警
  遲滯      指標在門檻附近抖動不該產生告警洗版
  可用性    沒資料要回報 unavailable，不能回報 L0（「沒資料」≠「沒事」）
"""

from __future__ import annotations

import pandas as pd
import pytest

from services.risk_engine.engine import (
    Condition,
    RiskEngine,
    Rule,
    level_rank,
    load_rules,
    max_level,
)
from services.risk_engine.eventcard import EventStore, build_event_cards
from swx_core import SwxStore, normalize, registry


@pytest.fixture
def store(tmp_path):
    return SwxStore(tmp_path)


def seed(store: SwxStore, param: str, values, *, freq="3h", start="2024-05-10"):
    times = pd.date_range(start, periods=len(values), freq=freq, tz="UTC")
    df = normalize(
        pd.DataFrame(
            {
                "valid_time": times,
                "param_code": param,
                "value": values,
                "unit": registry()[param].unit,
                "source_id": "test",
                "data_type": "OBS",
            }
        )
    )
    store.write(df, source_id="test")
    return times


def rule(**kw) -> Rule:
    defaults = dict(
        rule_id="TEST-L2",
        domain="TEST",
        name="測試規則",
        level="L2",
        conditions=[Condition(param="KP_3H", op=">=", value=5.0)],
    )
    defaults.update(kw)
    return Rule(**defaults)


# ── 基本觸發 ────────────────────────────────────────────────────────────
def test_rule_fires_when_threshold_exceeded(store):
    seed(store, "KP_3H", [1.0, 2.0, 6.0, 7.0, 1.0])
    eps, status = RiskEngine(store, []).evaluate_rule(rule())
    assert status == "ok"
    assert len(eps) == 1
    assert eps[0].peak_value == pytest.approx(7.0)


def test_missing_data_reports_unavailable_not_l0(store):
    """沒有資料時必須明說不可判定，否則儀表板會顯示綠燈誤導判讀。"""
    seed(store, "KP_3H", [1.0, 2.0])
    r = rule(conditions=[Condition(param="S4", op=">=", value=0.6)],
             requires_params=("S4",))
    eps, status = RiskEngine(store, []).evaluate_rule(r)
    assert status == "unavailable"
    assert eps == []


# ── 駐留時間 ────────────────────────────────────────────────────────────
def test_dwell_suppresses_single_sample_spike(store):
    seed(store, "KP_3H", [1.0, 9.0, 1.0, 1.0])       # 單點尖峰
    r = rule(conditions=[Condition(param="KP_3H", op=">=", value=5.0, dwell_h=6.0)])
    eps, _ = RiskEngine(store, []).evaluate_rule(r)
    assert eps == [], "單點尖峰不應觸發需駐留 6 小時的規則"


def test_dwell_allows_sustained_exceedance(store):
    seed(store, "KP_3H", [1.0, 6.0, 6.0, 6.0, 6.0, 1.0])   # 連續 12 小時
    r = rule(conditions=[Condition(param="KP_3H", op=">=", value=5.0, dwell_h=6.0)])
    eps, _ = RiskEngine(store, []).evaluate_rule(r)
    assert len(eps) == 1


# ── 遲滯 ────────────────────────────────────────────────────────────────
def test_hysteresis_prevents_alert_flapping(store):
    # 在門檻上下抖動：無遲滯會產生多段告警
    seed(store, "KP_3H", [5.5, 4.5, 5.5, 4.5, 5.5, 4.5, 1.0, 1.0, 1.0, 1.0])
    plain, _ = RiskEngine(store, []).evaluate_rule(rule())
    damped, _ = RiskEngine(store, []).evaluate_rule(
        rule(clear_below=3.0, clear_dwell_h=6.0)
    )
    assert len(plain) > len(damped), "遲滯應把抖動合併成較少的告警段"
    assert len(damped) == 1


def test_multi_param_hysteresis_uses_per_param_thresholds(store):
    """Kp（0–9）與 Ap（0–400）量級差兩個數量級，共用純量門檻會讓告警永不解除。"""
    seed(store, "KP_3H", [6.0, 6.0, 1.0, 1.0, 1.0, 1.0])
    seed(store, "AP_AVG", [80.0, 80.0, 5.0, 5.0, 5.0, 5.0], freq="24h")
    r = rule(
        conditions=[
            Condition(param="KP_3H", op=">=", value=5.0),
            Condition(param="AP_AVG", op=">=", value=40.0),
        ],
        clear_below={"KP_3H": 4.0, "AP_AVG": 30.0},
        clear_dwell_h=3.0,
    )
    eps, _ = RiskEngine(store, []).evaluate_rule(r)
    assert len(eps) == 1
    assert eps[0].duration_h < 72, "解除條件必須真的解除，否則事件段會無限延長"


def test_clear_thresholds_scale_proportionally_for_scalar():
    r = rule(
        conditions=[
            Condition(param="KP_3H", op=">=", value=5.0),
            Condition(param="AP_AVG", op=">=", value=40.0),
        ],
        clear_below=4.0,
    )
    thr = RiskEngine.clear_thresholds(r)
    assert thr["KP_3H"] == pytest.approx(4.0)
    assert thr["AP_AVG"] == pytest.approx(32.0)   # 40 × (4/5)


# ── 等級運算 ────────────────────────────────────────────────────────────
def test_level_ordering():
    assert level_rank("L4") > level_rank("L3") > level_rank("L0")
    assert max_level(["L1", "L3", "L0"]) == "L3"
    assert max_level([]) == "L0"


# ── 設定檔完整性 ────────────────────────────────────────────────────────
def test_shipped_rules_are_wellformed():
    rules = load_rules()
    assert rules, "應載入 configs/rules/*.yaml"
    reg = registry()
    ids = [r.rule_id for r in rules]
    assert len(ids) == len(set(ids)), "規則 ID 必須唯一"
    for r in rules:
        assert r.level in ("L0", "L1", "L2", "L3", "L4")
        assert r.conditions, f"{r.rule_id} 沒有任何條件"
        for c in r.conditions:
            assert c.param in reg, f"{r.rule_id} 引用未註冊參數 {c.param}"
        assert r.impact, f"{r.rule_id} 缺少影響說明"
        assert r.action, f"{r.rule_id} 缺少處置建議"


def test_pre_alert_rules_never_exceed_l1():
    """閃焰無法提前偵測；機率型提示不得升級為事件等級。"""
    for r in load_rules():
        if r.pre_alert:
            assert r.level == "L1", f"{r.rule_id} 是機率提示，不應高於 L1"


# ── 事件卡 ──────────────────────────────────────────────────────────────
def test_event_card_from_episodes(store):
    seed(store, "KP_3H", [1.0, 6.0, 8.5, 8.5, 6.0, 1.0])
    eps, _ = RiskEngine(store, [rule(level="L3")]).evaluate()
    cards = build_event_cards(eps, store=store)
    assert len(cards) == 1
    d = cards[0].to_dict()
    assert d["type"] == "GEOMAGNETIC_STORM"
    assert d["mission_level"] == "L3"
    assert d["sda_hooks"]["record_in_sda"] is True
    assert len({x["param"] for x in d["drivers"]}) == len(d["drivers"]), "驅動參數不應重複"


def test_event_card_revision_only_on_change(tmp_path, store):
    seed(store, "KP_3H", [1.0, 6.0, 8.5, 1.0])
    eps, _ = RiskEngine(store, [rule(level="L3")]).evaluate()
    card = build_event_cards(eps, store=store)[0]

    es = EventStore(tmp_path / "ops.sqlite")
    es.upsert(card)
    es.upsert(card)
    assert len(es.history(card.event_id)) == 1, "內容未變不應產生新版次"

    card.mission_level = "L4"
    es.upsert(card)
    history = es.history(card.event_id)
    assert len(history) == 2
    assert history[-1]["supersedes"].endswith("@r1")


# ── 部分可用（partial）────────────────────────────────────────────────
def test_partial_status_still_evaluates_available_criteria():
    """部分判據有資料時仍須評估，不可整條規則噤聲。

    實際情境：GNSS-L3-SCINT 宣告 requires_params = [S4, ROTI]。
    福衛七號掩星接上後 S4 有了、ROTI 仍無來源。舊行為是整條規則回
    `unavailable`——於是實測到 S4=1.17（遠超 0.6 門檻）卻不發報，
    **明明偵測得到卻不說**，比沒有資料更糟。

    但也不能回 `ok`：本規則為 any/OR，缺少的 ROTI 可能單獨觸發，
    所以此狀態下「沒有告警」**不等於確認平靜**。故第三種狀態 `partial`。
    """
    from datetime import datetime, timedelta, timezone

    import pandas as pd

    from services.risk_engine.engine import RiskEngine, load_rules

    rules = [r for r in load_rules() if r.rule_id == "GNSS-L3-SCINT"]
    assert rules, "找不到 GNSS-L3-SCINT 規則"
    rule = rules[0]
    assert set(rule.requires_params) == {"S4", "ROTI"}

    import tempfile

    store = SwxStore(tempfile.mkdtemp())
    t0 = datetime(2026, 8, 17, tzinfo=timezone.utc)
    times = pd.date_range(t0, periods=8, freq="15min", tz="UTC")
    store.write(
        normalize(pd.DataFrame({
            "valid_time": times,
            "param_code": "S4",
            "value": [0.1, 0.2, 0.9, 1.1, 1.0, 0.9, 0.2, 0.1],
            "unit": "1",
            "source_id": "tacc_scn1c2",
            "data_type": "OBS",
        })),
        source_id="tacc_scn1c2",
    )

    episodes, status = RiskEngine(store, rules=[rule]).evaluate(
        start=t0 - timedelta(hours=1), end=t0 + timedelta(hours=4))

    row = status[status["rule_id"] == "GNSS-L3-SCINT"].iloc[0]
    assert row["status"] == "partial", f"應為 partial，實得 {row['status']}"
    assert episodes, "S4 已超過門檻卻未產生事件段"
    assert all(e.level == "L3" for e in episodes)


def test_unavailable_only_when_no_criterion_has_data():
    """一個判據都沒有時才是 unavailable。"""
    import tempfile
    from datetime import datetime, timedelta, timezone

    from services.risk_engine.engine import RiskEngine, load_rules

    rule = next(r for r in load_rules() if r.rule_id == "GNSS-L3-SCINT")
    store = SwxStore(tempfile.mkdtemp())
    t0 = datetime(2026, 8, 17, tzinfo=timezone.utc)

    _, status = RiskEngine(store, rules=[rule]).evaluate(
        start=t0, end=t0 + timedelta(hours=4))
    assert status[status["rule_id"] == "GNSS-L3-SCINT"].iloc[0]["status"] == "unavailable"


def test_declared_domain_without_rules_is_listed_as_no_criteria():
    """已宣告但尚無規則的網域必須出現在 nowcast 上，且不得被當成已評估。

    VHF/UHF、SATCOM（S/X/Ka）、衛星操作是構想書影響矩陣要求的欄位，
    已宣告於 params.yaml 的 `impact_domains`，但 configs/rules 還沒有對應規則。
    若讓它們從表上消失，畫面上「少一列」與「綠燈」難以分辨——讀者會把
    「還沒訂門檻」讀成「查過沒事」。這與參數層級的「沒資料 ≠ 沒事」同一條原則。
    """
    import tempfile

    from services.risk_engine.engine import RiskEngine

    engine = RiskEngine(SwxStore(tempfile.mkdtemp()))
    now = engine.nowcast()
    listed = set(now["domain"])
    declared = set(registry().impact_domains)

    assert declared <= listed, f"已宣告卻未列出的網域：{sorted(declared - listed)}"

    ruleless = declared - {r.domain for r in engine.rules}
    assert ruleless, "此測試假設仍有尚未建立規則的網域；若已全數補齊請改寫本測試"
    for domain in ruleless:
        row = now[now["domain"] == domain].iloc[0]
        assert row["criteria_total"] == 0
        assert row["level"] == "—", "無判據不得顯示為 L0"
        assert not row["fully_evaluated"], "沒有規則不等於全部通過"
        assert row["inference"] == "unavailable"
