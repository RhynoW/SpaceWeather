"""README 的數量宣稱必須與實際設定一致。

實際發生過：接入 CWA 後 README 仍寫「19 個來源（16 個可運作）」，
而真實值已是 20／17。這種漂移**不會有任何徵兆**——程式照跑、測試照過，
只有讀文件的人被誤導。而 README 正是外部審查者唯一會讀的東西。

這裡把 README 裡的數字抽出來，與 configs/ 的實際內容比對。
新增來源、影像、動畫、規則或頁面後若忘了改文件，就會紅燈。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))
sys.path.insert(0, str(ROOT))

README = (ROOT / "README.md").read_text(encoding="utf-8")


def _only(pattern: str) -> int:
    """抽出唯一一個符合的數字；有多個不同值代表 README 自相矛盾。"""
    found = {int(m) for m in re.findall(pattern, README)}
    assert found, f"README 中找不到符合 {pattern!r} 的數字"
    assert len(found) == 1, f"README 中 {pattern!r} 有多個不一致的值：{sorted(found)}"
    return found.pop()


def _sidebar_pages() -> list[str]:
    """側欄頁面清單。取自模組層級的 PAGES 常數，而非 radio 的呼叫參數——
    頁面清單與網址代稱需成對維護，故已抽成常數。"""
    tree = ast.parse((ROOT / "apps" / "dashboard" / "app.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "PAGES" for t in node.targets
        ):
            return [e.value for e in node.value.elts]
    raise AssertionError("找不到側欄頁面清單（PAGES）")


def test_source_counts_match_config():
    from swx_core import catalog

    srcs = list(catalog())
    ready = [s for s in srcs if s.status == "ready"]
    assert _only(r"(\d+) 個來源（") == len(srcs)
    assert _only(r"（(\d+) 個可運作") == len(ready)


def test_auto_refresh_count_matches_config():
    from services.ingest.refresh import live_sources

    assert _only(r"(\d+) 個納入背景自動更新") == len(live_sources())


def test_param_and_rule_counts_match_config():
    from swx_core import registry

    from services.risk_engine.engine import load_rules

    assert _only(r"(\d+) 個註冊參數") == len(registry().codes)
    assert _only(r"(\d+) 條規則") == len(load_rules())


def test_media_counts_match_config():
    from swx_core import animations, imagery

    anims = animations()
    assert _only(r"(\d+) 張公開影像") == len(imagery())
    assert _only(r"\*\*(\d+) 段動畫\*\*") == len(anims)
    videos = sum(1 for a in anims if a["kind"] == "video")
    assert _only(r"現成 MP4 共 (\d+) 支") == videos


def test_api_endpoint_count_matches_implementation():
    src = (ROOT / "services" / "api" / "app.py").read_text(encoding="utf-8")
    assert _only(r"(\d+) 個端點") == len(re.findall(r'@app\.get\("', src))


def test_dashboard_page_table_lists_every_page():
    """README 的頁面表必須涵蓋側欄的每一頁（頁數不寫死，逐項比對）。"""
    missing = [p for p in _sidebar_pages() if p not in README]
    assert not missing, f"README 未提及的頁面：{missing}"


def test_lock_file_package_count_matches():
    lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    pkgs = [ln for ln in lock.splitlines() if ln.strip() and not ln.startswith("#")]
    assert _only(r"（(\d+) 個套件") == len(pkgs)


@pytest.mark.parametrize("doc", sorted((ROOT / "docs").glob("*.md")))
def test_every_doc_is_linked_from_readme(doc: Path):
    """docs/ 下的每份文件都要能從 README 找到，否則等於不存在。"""
    assert f"docs/{doc.name}" in README, f"{doc.name} 未被 README 引用"
