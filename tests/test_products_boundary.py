"""產品層邊界契約（架構書 §4 治理規則、§16.2 R1）。

§4 規定展示層一律走契約介面。過去儀表板直接 import 規則引擎與匯出器，
API 也各自組參數——同一個產品兩條路徑，只改一邊就會說法不一。
這裡守兩件事：

  1. 邊界：apps/ 與 services/api 不得直接 import risk_engine／exporter，
     只能經 services.products。用 AST 掃描，延遲 import（函式內）也抓得到。
  2. 同源：API 回應與產品層函式的輸出逐欄一致。
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from services import products
from services.api.app import create_app
from swx_core import SwxStore

ROOT = Path(__file__).resolve().parents[1]

#: 只有產品層可以碰的內部模組
INTERNAL = ("services.risk_engine", "services.exporter")

#: 必須經產品層的呼叫端
CALLERS = [
    *sorted((ROOT / "apps").rglob("*.py")),
    *sorted((ROOT / "services" / "api").rglob("*.py")),
]


def _imports(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(node.lineno, a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append((node.lineno, node.module))
            # from services import risk_engine
            out += [(node.lineno, f"{node.module}.{a.name}") for a in node.names]
    return out


@pytest.mark.parametrize("path", CALLERS, ids=lambda p: str(p.relative_to(ROOT)))
def test_callers_do_not_bypass_product_layer(path):
    bad = [f"{path.name}:{ln} {mod}" for ln, mod in _imports(path)
           if mod.startswith(INTERNAL)]
    assert not bad, f"須改經 services.products：{bad}"


def test_boundary_scan_actually_sees_callers():
    """防呆：掃描清單若因搬目錄而變空，上一個測試會無聲通過。"""
    names = {p.name for p in CALLERS}
    assert {"app.py"} <= names and len(CALLERS) >= 3


# ── 同源 ────────────────────────────────────────────────────────────────
def _kp_store(tmp_path) -> SwxStore:
    store = SwxStore(tmp_path)
    t = pd.date_range("2024-05-08", "2024-05-15", freq="3h", tz="UTC")
    kp = [8.7 if pd.Timestamp("2024-05-10T18Z") <= x <= pd.Timestamp("2024-05-11T12Z")
          else 2.0 for x in t]
    store.write(pd.DataFrame({
        "valid_time": t, "param_code": "KP_3H", "value": kp, "unit": "",
        "source_id": "test", "source_tier": 1, "quality_flag": "good", "data_type": "OBS",
    }), ingest_time=datetime(2024, 5, 16, tzinfo=timezone.utc))
    return store


def test_api_event_cards_match_product_layer(tmp_path, monkeypatch):
    monkeypatch.setenv("SWX_DATA_DIR", str(tmp_path))
    store = _kp_store(tmp_path)
    client = create_app(store).test_client()
    body = client.get("/v1/events?from=2024-05-08&to=2024-05-15").get_json()

    direct = products.event_cards(
        store, start=datetime(2024, 5, 8, tzinfo=timezone.utc),
        end=datetime(2024, 5, 15, tzinfo=timezone.utc))
    assert direct, "測試資料應觸發至少一張事件卡"
    strip = lambda cs: [{k: v for k, v in c.items() if k != "issued_utc"} for c in cs]  # noqa: E731
    assert strip(body["events"]) == strip(direct)


def test_cards_from_episodes_equal_cards_from_window(tmp_path):
    """儀表板以既有事件段組卡（省一次回放），結果必須與重算相同。"""
    store = _kp_store(tmp_path)
    kw = dict(start=datetime(2024, 5, 8, tzinfo=timezone.utc),
              end=datetime(2024, 5, 15, tzinfo=timezone.utc))
    eps, _ = products.evaluate(store, **kw)
    strip = lambda cs: [{k: v for k, v in c.items() if k != "issued_utc"} for c in cs]  # noqa: E731
    assert strip(products.event_cards(store, episodes=eps)) == \
        strip(products.event_cards(store, **kw))


def test_api_stk_export_matches_product_layer(tmp_path):
    store = _kp_store(tmp_path)
    client = create_app(store).test_client()
    api_text = client.get("/v1/exports/stk/spaceweather.txt").get_data(as_text=True)
    lib_text = products.stk_cssi_text(store)
    # 唯一允許的差異是標頭的產生時間
    drop = lambda s: [ln for ln in s.splitlines() if not ln.startswith("UPDATED")]  # noqa: E731
    assert drop(api_text) == drop(lib_text)


def test_forecast_view_handles_continuous_target_without_prob_code(tmp_path):
    """F10.7／Ap 沒有機率參數。儀表板曾把 None 直接放進查詢清單。"""
    targets = products.forecast_targets()
    continuous = [k for k, s in targets.items() if s.prob_code is None]
    assert continuous, "應至少有一個連續型目標"
    for key in continuous:
        view = products.forecast_view(SwxStore(tmp_path), key)
        assert view.issued is None and view.own.empty


def test_forecast_view_anchor_is_floored_to_grid(tmp_path):
    store = SwxStore(tmp_path)
    store.write(pd.DataFrame({
        "valid_time": [pd.Timestamp("2026-11-18T09:05Z")], "param_code": "KP_3H",
        "value": [3.0], "unit": "", "source_id": "test", "source_tier": 1,
        "quality_flag": "good", "data_type": "OBS",
    }))
    view = products.forecast_view(store, "kp", now=pd.Timestamp("2026-11-18T10:00Z"))
    assert view.latest_observed == pd.Timestamp("2026-11-18T09:05Z")
    assert view.issued == pd.Timestamp("2026-11-18T09:00Z")
