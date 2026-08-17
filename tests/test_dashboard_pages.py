"""儀表板頁面集合的契約測試。

README 曾寫死「9 頁」「10 頁」這種數字，新增頁面時必然過期。
把頁面清單變成契約：改了側欄就會紅燈，逼你同步改文件。
不驗證頁面內容，只驗證「文件說有的頁面確實存在、且沒有沒寫進文件的頁面」。
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "apps" / "dashboard" / "app.py"

# 與 README「儀表板」章節逐列對應。
DOCUMENTED_PAGES = [
    "值勤模式", "太空環境總覽", "參數時序", "事件卡", "太陽閃焰", "48 小時預報",
    "地磁基準場", "軌道與密度修正", "資料健康", "門檻校準", "名詞與判讀",
]


def _sidebar_pages() -> list[str]:
    """從 st.sidebar.radio 的原始碼取出頁面清單（不執行 streamlit）。"""
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr == "radio"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.List) and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str) for e in arg.elts
            ):
                return [e.value for e in arg.elts]
    raise AssertionError("找不到側欄的頁面清單")


def _dispatched_pages() -> set[str]:
    """取出所有 `page == "..."` 的分支，確認每一頁都有對應實作。"""
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) \
                and node.left.id == "page":
            for c in node.comparators:
                if isinstance(c, ast.Constant) and isinstance(c.value, str):
                    out.add(c.value)
    return out


def test_sidebar_matches_documentation():
    actual = _sidebar_pages()
    assert actual == DOCUMENTED_PAGES, (
        f"側欄頁面與文件不一致\n  側欄：{actual}\n  文件：{DOCUMENTED_PAGES}"
    )


def test_every_page_has_an_implementation_branch():
    missing = set(_sidebar_pages()) - _dispatched_pages()
    assert not missing, f"側欄列出但沒有實作分支的頁面：{sorted(missing)}"


def test_no_orphan_branches():
    orphan = _dispatched_pages() - set(_sidebar_pages())
    assert not orphan, f"有實作分支但側欄未列出的頁面：{sorted(orphan)}"


def test_duty_mode_is_the_landing_page():
    """值勤模式必須排第一——事件發生時預設落地頁就是它。"""
    assert _sidebar_pages()[0] == "值勤模式"
