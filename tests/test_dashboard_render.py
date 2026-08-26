"""儀表板實際渲染測試。

為什麼需要這個：先前只做 `ast.parse` 語法檢查與 HTTP 200 探測，
兩者都**證明不了頁面能渲染**——Streamlit 是 SPA，伺服器起得來不代表
頁面腳本跑得完。實際踩過的坑包括：某頁引用了不存在的輔助函式、
資料為空時的欄位存取、以及 API 過期（`use_container_width` 官方移除期限已過）。

這裡用 Streamlit 官方的 AppTest 逐頁實跑，任一頁拋例外就紅燈。
較慢（約 30–60 秒），但它守的是「使用者打開會不會看到錯誤畫面」，
這是其他任何測試都涵蓋不到的。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit.testing.v1", reason="需要 Streamlit 的 AppTest")

from streamlit.testing.v1 import AppTest  # noqa: E402

APP_PATH = str(Path(__file__).resolve().parents[1] / "apps" / "dashboard" / "app.py")

PAGES = [
    "值勤模式", "太空環境總覽", "太陽與行星際影像", "參數時序", "事件卡",
    "太陽閃焰", "短時預報", "RTK 現場查核", "地磁基準場", "軌道與密度修正",
    "資料健康", "門檻校準", "名詞與判讀", "使用指南", "STEM 教學",
]


@pytest.mark.parametrize("page", PAGES)
def test_page_renders_without_exception(page: str):
    app = AppTest.from_file(APP_PATH, default_timeout=120)
    app.run()
    assert not app.exception, f"首頁載入即失敗：{[str(e.message) for e in app.exception]}"

    app.sidebar.radio[0].set_value(page).run()
    assert not app.exception, (
        f"「{page}」渲染失敗：" + "; ".join(str(e.message) for e in app.exception)
    )


def test_rtk_page_says_why_i95_is_missing(tmp_path, monkeypatch):
    """沒有 I95 時，畫面必須說出**是哪一種沒有**。

    雲端站台沒有終端機可下指令；畫面只寫「目前沒有資料」的話，
    值勤的人分不出是還沒抓、抓失敗、還是抓到了讀不出數值——
    這三種要找的人完全不同。
    """
    monkeypatch.setenv("SWX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SWX_DISABLE_SOURCES", "nlsc_egnss_i95")

    # Streamlit 的快取是**行程層級**的：同一輪 pytest 裡先前那次 AppTest
    # 已經用真實 data/ 建好 store 並存進 cache_resource，不清掉的話
    # 這裡改的 SWX_DATA_DIR 根本不會生效，測試會在單獨跑時過、整批跑時掛。
    import streamlit as _st

    _st.cache_data.clear()
    _st.cache_resource.clear()

    app = AppTest.from_file(APP_PATH, default_timeout=120)
    app.run()
    app.sidebar.radio[0].set_value("RTK 現場查核").run()
    assert not app.exception, (
        "無 I95 時 RTK 頁渲染失敗："
        + "; ".join(str(e.message) for e in app.exception)
    )

    text = " ".join(str(b.value) for b in app.info)
    assert "nlsc_egnss_i95" in text, "空狀態沒有指出是哪個來源"
    assert any(code in text for code in
               ("running", "skipped", "failed", "empty", "ok", "not_run")),         "空狀態沒有說出更新結果的狀態碼"


def test_sidebar_offers_every_documented_page():
    """側欄實際提供的選項要與文件一致（與 test_dashboard_pages 的靜態檢查互補）。"""
    app = AppTest.from_file(APP_PATH, default_timeout=120)
    app.run()
    assert list(app.sidebar.radio[0].options) == PAGES


def _stem_still_ids() -> list[str]:
    """STEM 頁引用、且屬於靜態影像（非動畫）的媒體 id。"""
    import ast
    import sys

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "packages"))
    from swx_core import animations, imagery

    still = {i["id"] for i in imagery()}
    anim = {a["id"] for a in animations()}
    src = (root / "apps" / "dashboard" / "stem.py").read_text(encoding="utf-8")
    out = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)                 and node.func.id == "images_by_id":
            out += [a.value for a in node.args
                    if isinstance(a, ast.Constant) and a.value in still - anim]
    return out


def test_stem_renders_every_referenced_still():
    """STEM 頁引用幾張靜態圖，就必須渲染出幾個影像元素。

    **這條是為了抓一個實際漏掉的失效。** 新增了一種動態網址類型
    （kind: latest_json，網址帶時刻、無固定 latest.jpg），只改了 app.py
    的解析函式；stem.py 的媒體卡另有一份複製品，仍直接讀 item["url"]。
    該欄位在這類影像上不存在，KeyError 被卡片的 except 吞掉，
    顯示成「載入失敗」——看起來像對方站台掛了，其實是本地邏輯漏改。

    當時的 smoke test 只驗證網址解析得出來、抓得到圖，**沒有驗證頁面實際
    渲染出幾張**，所以漏掉了。元素計數會直接抓到這件事。
    """
    expected = _stem_still_ids()
    assert expected, "STEM 頁未引用任何靜態影像，此測試失去意義"

    app = AppTest.from_file(APP_PATH, default_timeout=180)
    app.query_params["page"] = "stem"
    app.run()
    assert not app.exception, "; ".join(str(e.message) for e in app.exception)

    imgs = app.get("imgs")
    assert len(imgs) == len(expected), (
        f"引用 {len(expected)} 張靜態圖（{expected}），"
        f"實際只渲染出 {len(imgs)} 個影像元素——有影像取不到網址"
    )
    # **模式標記本來就是 st.warning**（「這是算的不是拍的」必須醒目），
    # 所以不能一律禁止警告，只能禁止「載入失敗」那一類。
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "dashboard"))
    from stem import LOAD_FAIL, LOAD_FAIL_WHY

    bad_prefixes = tuple(
        v.split("（")[0].split("(")[0].strip()
        for v in list(LOAD_FAIL.values()) + list(LOAD_FAIL_WHY.values())
    )
    failures = [w.value for w in app.warning
                if any(w.value.startswith(b) for b in bad_prefixes if b)]
    assert not failures, f"STEM 頁出現載入失敗：{failures}"
