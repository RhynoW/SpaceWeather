"""API 介面契約測試（審查意見：文件寫死的端點數必須由測試守住）。

README 與架構書都寫「N 個端點」。這種手工維護的數字會在新增端點時默默過期，
而 API 是子計畫間的介接面——文件與實作不一致，介接方就會照著錯的文件開發。
這裡把端點集合寫成契約：改了 API 就會紅燈，逼你同步改文件。

另一條守的是**錯誤回應不得外洩內部例外**。API 規劃對外部署，例外字串常含
檔案路徑與 SQL 片段，不該直接回給呼叫端。
"""

from __future__ import annotations

import re
from pathlib import Path

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
    ("GET", "/v1/forecast"),
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


def test_documented_endpoint_count_matches_readme():
    """README 寫「N 個端點」，數字變了就要改文件（兩處同時改才會綠燈）。"""
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    documented = int(re.search(r"(\d+) 個端點", readme).group(1))
    assert len(DOCUMENTED_ENDPOINTS) == documented


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


def test_research_grade_advisory_present_for_own_forecast(app, tmp_path):
    """本系統預報必須在 JSON 中自帶作業性告誡。

    只寫在 README 不夠——呼叫端讀的是 JSON。這條守的是「文件說有、實際沒有」。
    """
    import pandas as pd

    from swx_core import SwxStore, normalize

    store = SwxStore(tmp_path)
    t = pd.Timestamp("2026-01-01", tz="UTC")
    store.write(
        normalize(pd.DataFrame([{
            "valid_time": t, "param_code": "KP_3H", "value": 5.0, "unit": "1",
            "source_id": "swx_forecast", "data_type": "FCS",
        }])),
        source_id="swx_forecast",
    )
    body = create_app(store).test_client().get("/v1/obs?param=KP_3H").get_json()
    adv = body.get("advisory")
    assert adv is not None, "本系統預報未帶 advisory"
    assert adv["code"] == "RESEARCH_GRADE_FORECAST"
    assert adv["not_for_operational_use_beyond_h"] == 12


def test_no_advisory_for_pure_observations(app, tmp_path):
    """純觀測不應被貼上預報告誡，否則告誡會被當成雜訊忽略。"""
    import pandas as pd

    from swx_core import SwxStore, normalize

    store = SwxStore(tmp_path)
    store.write(
        normalize(pd.DataFrame([{
            "valid_time": pd.Timestamp("2026-01-01", tz="UTC"), "param_code": "KP_3H",
            "value": 3.0, "unit": "1", "source_id": "gfz_nowcast", "data_type": "OBS",
        }])),
        source_id="gfz_nowcast",
    )
    body = create_app(store).test_client().get("/v1/obs?param=KP_3H").get_json()
    assert "advisory" not in body


def test_health_exposes_data_origin(app):
    """呼叫端要能分辨服務端的是示範快照還是實際擷取的資料。

    只給資料齡期不夠：快照內的資料在其自身時間軸上看起來是新的，
    齡期正常，使用者仍會誤以為是即時作業資料。
    """
    body = app.test_client().get("/health").get_json()
    origin = body.get("data")
    assert origin is not None, "/health 未揭露資料來源性質"
    assert set(origin) >= {"data_origin", "is_demo", "operational"}
    assert origin["operational"] is False, "本系統目前全域皆非作業級"


def test_data_age_excludes_forecast_rows(tmp_path):
    """齡期只能由觀測列計算，否則 degraded 永遠不觸發。

    實際發生過的缺陷：回應同時含觀測與預報列時，`max(valid_time)` 取到
    未來的預報時刻，`data_age_s` 變成負值，於是 `age > 5×cadence` 恆為假——
    **只要有任何預報列存在，過期的觀測通道就再也不會被標記為劣化**，
    而那正是 degraded 的唯一用途。畫面與 API 一切正常，只是警告失效。
    """
    from datetime import datetime, timedelta, timezone

    import pandas as pd

    from swx_core import SwxStore, normalize

    store = SwxStore(tmp_path)
    now = datetime.now(timezone.utc)
    store.write(
        normalize(pd.DataFrame([
            {"valid_time": pd.Timestamp(now - timedelta(hours=20)), "param_code": "KP_3H",
             "value": 3.0, "unit": "1", "source_id": "gfz_nowcast", "data_type": "OBS"},
            {"valid_time": pd.Timestamp(now + timedelta(hours=24)), "param_code": "KP_3H",
             "value": 5.0, "unit": "1", "source_id": "swx_forecast", "data_type": "FCS"},
        ])),
        source_id="mix",
    )
    body = create_app(store).test_client().get("/v1/obs?param=KP_3H").get_json()

    assert body["data_age_s"] > 0, f"齡期為負（{body['data_age_s']}）——取到了預報列"
    assert 71000 < body["data_age_s"] < 73000, "齡期未對應最新觀測"
    assert body["observed_count"] == 1
    assert body["degraded"] is True, "20 小時前的 3 小時參數未判為劣化"
    assert body["forecast_to_utc"] is not None, "預報涵蓋時刻未揭露"
    assert body["latest_observed_utc"] is not None


def test_advisory_threshold_matches_documented_wording():
    """API 的作業性門檻必須與文件用語一致。

    文件說「1–12 h 可作研究參考，>12 h 為非作業性研究預報」，
    API 就必須是 beyond_h = 12。兩者若對不上，介接方會照錯的那個實作。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "services" / "api" / "app.py").read_text(
        encoding="utf-8")
    assert '"not_for_operational_use_beyond_h": 12' in src

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert "12 h" in readme or "12h" in readme, "README 未載明 12 小時門檻"
