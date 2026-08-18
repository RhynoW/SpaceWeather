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
    "太陽閃焰", "48 小時預報", "地磁基準場", "軌道與密度修正",
    "資料健康", "門檻校準", "名詞與判讀", "使用指南",
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


def test_sidebar_offers_every_documented_page():
    """側欄實際提供的選項要與文件一致（與 test_dashboard_pages 的靜態檢查互補）。"""
    app = AppTest.from_file(APP_PATH, default_timeout=120)
    app.run()
    assert list(app.sidebar.radio[0].options) == PAGES
