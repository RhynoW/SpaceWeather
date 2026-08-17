"""API 介面契約測試（審查意見：文件寫死的端點數必須由測試守住）。

README 與架構書都寫「13 個端點」。這種手工維護的數字會在新增端點時默默過期，
而 API 是子計畫間的介接面——文件與實作不一致，介接方就會照著錯的文件開發。
這裡把端點集合寫成契約：改了 API 就會紅燈，逼你同步改文件。

另一條守的是**錯誤回應不得外洩內部例外**。API 規劃對外部署，例外字串常含
檔案路徑與 SQL 片段，不該直接回給呼叫端。
"""

from __future__ import annotations

import pytest

from services.api.app import create_app
from swx_core import SwxStore

# 與 README「API」章節逐列對應。新增端點時兩處要一起改。
DOCUMENTED_ENDPOINTS = {
    ("GET", "/health"),
    ("GET", "/v1/params"),
    ("GET", "/v1/sources"),
    ("GET", "/v1/health/data"),
    ("GET", "/v1/obs"),
    ("GET", "/v1/nowcast"),
    ("GET", "/v1/events"),
    ("GET", "/v1/events/<event_id>"),
    ("GET", "/v1/events/<event_id>/history"),
    ("GET", "/v1/rules"),
    ("GET", "/v1/flares"),
    ("GET", "/v1/exports/stk/spaceweather.txt"),
    ("GET", "/v1/exports/drag-correction"),
}


@pytest.fixture
def app(tmp_path):
    return create_app(SwxStore(tmp_path))


def _routes(app) -> set[tuple[str, str]]:
    out = set()
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            out.add((method, str(rule)))
    return out


def test_endpoint_set_matches_documentation(app):
    actual = _routes(app)
    undocumented = actual - DOCUMENTED_ENDPOINTS
    missing = DOCUMENTED_ENDPOINTS - actual
    assert not undocumented, f"實作有但文件未列的端點：{sorted(undocumented)}"
    assert not missing, f"文件列了但實作沒有的端點：{sorted(missing)}"


def test_documented_endpoint_count_is_thirteen():
    """README 寫「13 個端點」，數字變了就要改文件。"""
    assert len(DOCUMENTED_ENDPOINTS) == 13


def test_internal_exception_is_not_leaked_to_client(app):
    """500 回應不得帶內部例外訊息（非 debug 模式）。"""
    app.config["PROPAGATE_EXCEPTIONS"] = False

    @app.get("/v1/_boom")
    def _boom():
        raise RuntimeError("秘密路徑 F:/internal/secret.duckdb")

    resp = app.test_client().get("/v1/_boom")
    assert resp.status_code == 500
    body = resp.get_json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "detail" not in body["error"], "非 debug 模式不應回傳內部細節"
    assert "secret" not in resp.get_data(as_text=True)


def test_missing_required_param_is_400_not_500(app):
    """缺參數是呼叫端的錯，要回 400 並說明，不能讓它變成 500。"""
    resp = app.test_client().get("/v1/obs")
    assert resp.status_code == 400


def test_unregistered_param_is_404(app):
    resp = app.test_client().get("/v1/obs?param=NO_SUCH_PARAM")
    assert resp.status_code == 404
