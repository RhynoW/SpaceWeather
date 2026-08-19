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
    "值勤模式", "太空環境總覽", "太陽與行星際影像", "參數時序", "事件卡",
    "太陽閃焰", "48 小時預報", "地磁基準場", "軌道與密度修正",
    "資料健康", "門檻校準", "名詞與判讀", "使用指南", "STEM 教學",
]


def _assign(name: str):
    """取模組層級某個賦值的 AST 節點（不執行 streamlit）。"""
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return node.value
    raise AssertionError(f"找不到 {name}")


def _sidebar_pages() -> list[str]:
    """側欄頁面清單。"""
    node = _assign("PAGES")
    assert isinstance(node, ast.List), "PAGES 應為字面清單，否則此契約無從檢查"
    return [e.value for e in node.elts]


def _slugs() -> dict[str, str]:
    """頁面 → 網址代稱。"""
    node = _assign("PAGE_SLUGS")
    assert isinstance(node, ast.Dict)
    return {k.value: v.value for k, v in zip(node.keys, node.values)}


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


# ── STEM 教學頁的多語完整性 ────────────────────────────────────────────
def test_stem_content_is_complete_in_every_language():
    """任一語言缺一句，該語言的使用者就會看到空白或英文夾雜。

    翻譯最典型的腐化方式是「新增了中文段落，忘了補其他三種」——
    這不會報錯、不會當掉，只會讓某個語言的讀者看到殘缺內容。
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "dashboard"))
    from stem import G1_EXPLAIN, G1_OPTIONS, G2_NO, G2_YES, LANGS, T

    codes = set(LANGS.values())
    assert codes == {"zh", "ja", "en", "ms"}

    for key, variants in T.items():
        missing = codes - set(variants)
        assert not missing, f"文案 {key!r} 缺少語言：{sorted(missing)}"
        empty = [c for c in codes if not str(variants[c]).strip()]
        assert not empty, f"文案 {key!r} 在 {empty} 為空字串"

    for name, table in (("G1_OPTIONS", G1_OPTIONS), ("G1_EXPLAIN", G1_EXPLAIN),
                        ("G2_YES", G2_YES), ("G2_NO", G2_NO)):
        assert set(table) == codes, f"{name} 缺少語言：{sorted(codes - set(table))}"


def test_stem_storm_rule_is_self_consistent():
    """遊戲 2 的判定規則與解說必須一致，否則會教錯。

    這個遊戲要傳達的核心是「南向 Bz 才會出事」。若判定邏輯與解說文字
    對不上，學生會學到相反的規則——教學錯誤比程式錯誤更難發現。
    """
    import random
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "dashboard"))
    from stem import _g2_case

    for seed in range(200):
        bz, speed, storm = _g2_case(random.Random(seed))
        if storm:
            assert bz < 0, f"判定為地磁暴但 Bz 朝北（{bz}）——與教學規則矛盾"
            assert speed > 450, f"判定為地磁暴但風速僅 {speed}"


def test_stem_kp_to_g_scale_matches_noaa():
    """遊戲 3 的 Kp → G 級對照必須符合 NOAA 定義（Kp5=G1 … Kp9=G5）。"""
    import random
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "dashboard"))
    from stem import _g3_case

    for seed in range(100):
        kp, level = _g3_case(random.Random(seed))
        assert 1 <= level <= 5
        assert level == min(5, int(kp) - 4), f"Kp {kp} 對到 G{level}，與 NOAA 定義不符"


def test_stem_media_captions_are_multilingual():
    """STEM 頁的影像／動畫說明必須隨語言切換。

    實際發生過的錯誤：STEM 頁沿用共用影像元件，而元件顯示的是
    configs/imagery.yaml 的 note——那是寫給值勤人員的中文操作說明，
    切成日／英／馬來語時仍是中文。使用者看到的是半翻譯的頁面。

    這條同時守住「新增教學圖片時忘了補其他三種語言」。
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "dashboard"))
    from stem import (
        LANGS, LOAD_FAIL, MEDIA, MODEL_TAG, SOURCE_LABEL, VIDEO_SIZE,
    )

    codes = set(LANGS.values())
    assert MEDIA, "STEM 未定義任何多語媒體說明"

    for media_id, entry in MEDIA.items():
        for field in ("title", "note"):
            assert field in entry, f"{media_id} 缺 {field}"
            missing = codes - set(entry[field])
            assert not missing, f"{media_id}.{field} 缺少語言：{sorted(missing)}"
            empty = [c for c in codes if not str(entry[field][c]).strip()]
            assert not empty, f"{media_id}.{field} 在 {empty} 為空字串"

    for name, table in (("SOURCE_LABEL", SOURCE_LABEL), ("LOAD_FAIL", LOAD_FAIL),
                        ("VIDEO_SIZE", VIDEO_SIZE), ("MODEL_TAG", MODEL_TAG)):
        assert set(table) == codes, f"{name} 缺少語言：{sorted(codes - set(table))}"


def test_stem_media_ids_exist_in_imagery_config():
    """STEM 引用的媒體 id 必須真的存在於 configs/imagery.yaml。

    id 打錯不會報錯，只會讓那張圖與說明一起消失——頁面看起來「少了一段」。
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "dashboard"))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages"))
    from stem import MEDIA
    from swx_core import animations, imagery

    known = {i["id"] for i in imagery()} | {a["id"] for a in animations()}
    unknown = sorted(set(MEDIA) - known)
    assert not unknown, f"STEM 引用了不存在的媒體 id：{unknown}"


def test_every_page_has_a_url_slug():
    """每一頁都要能被直接連結。

    深層連結的價值在教學場合最明顯——網址要能寫在投影片上。
    """
    slugs = _slugs()
    assert set(slugs) == set(DOCUMENTED_PAGES), (
        f"頁面與網址代稱不一致；缺代稱：{set(DOCUMENTED_PAGES) - set(slugs)}；"
        f"多餘代稱：{set(slugs) - set(DOCUMENTED_PAGES)}"
    )
    assert len(set(slugs.values())) == len(slugs), "代稱重複"
    for page, slug in slugs.items():
        assert slug and slug.isascii() and slug.islower() and " " not in slug, (
            f"{page} 的代稱 {slug!r} 不適合放在網址上"
        )


def _stem_media_calls() -> list[str]:
    """STEM 頁實際引用的媒體 id（依原始碼出現順序）。"""
    tree = ast.parse((Path(__file__).resolve().parents[1] / "apps" / "dashboard"
                      / "stem.py").read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)                 and node.func.id in ("images_by_id", "animations_by_id"):
            out += [a.value for a in node.args
                    if isinstance(a, ast.Constant) and isinstance(a.value, str)]
    return out


def test_stem_shows_no_duplicate_media():
    """同一張媒體不得在教學頁出現兩次。

    重複顯示會讓學生以為那是兩件不同的事物，而這一頁的主要成本就是注意力。
    """
    used = _stem_media_calls()
    dupes = sorted({x for x in used if used.count(x) > 1})
    assert not dupes, f"STEM 頁重複引用：{dupes}"


def test_stem_does_not_show_a_still_and_its_own_animation():
    """不得同時放某張靜態圖與「同一張的動畫版」。

    這是實際發生過的問題：第二節先放 sdo_304_video 與 soho_c2_video，
    緊接著又放 sdo_euv_304 與 soho_lasco_c2 的靜態圖——同樣兩個主題
    連續出現兩次。配對關係宣告在 configs/imagery.yaml 的 still_of，
    因為那是資料的性質，新增動畫的人最清楚它拍的是哪一張。
    """
    from swx_core import animations

    pairs = {a["id"]: a.get("still_of") for a in animations()}
    used = set(_stem_media_calls())
    clashes = sorted(
        f"{anim} 與 {still}"
        for anim, still in pairs.items()
        if still and anim in used and still in used
    )
    assert not clashes, f"靜態圖與其動畫版同時出現：{clashes}"
