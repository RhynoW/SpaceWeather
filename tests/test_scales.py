"""NOAA G／R／S 階梯：程式與 README 對照表逐階比對。

README 宣稱「實作階梯由測試對照上表驗證」。在本檔之前只驗了 R 級，
G、S 根本沒有程式實作。這裡**直接解析 README 的表格**——改了表或改了程式，
另一邊會紅燈，而不是兩份各自維護、悄悄分岔的數字。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from swx_core.scales import G_THRESHOLDS, S_THRESHOLDS, g_scale, r_scale, s_scale, scale_rank

README = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
_SUP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻", "0123456789-")


def _readme_ladder(letter: str) -> dict[str, float]:
    out = {}
    if letter == "G":
        for lvl, kp in re.findall(r"\*\*G(\d)\*\* ＝ Kp (\d+)", README):
            out[f"G{lvl}"] = float(kp)
    elif letter == "S":
        for lvl, base, exp in re.findall(r"\*\*S(\d)\*\* ＝ (10)([⁰¹²³⁴⁵⁶⁷⁸⁹]*)", README):
            out[f"S{lvl}"] = 10.0 ** int(exp.translate(_SUP) or "1")
    else:
        for lvl, mant, exp in re.findall(r"\*\*R(\d)\*\* ＝ (\d)×10([⁻⁰¹²³⁴⁵⁶⁷⁸⁹]+)", README):
            out[f"R{lvl}"] = float(mant) * 10.0 ** int(exp.translate(_SUP))
    return out


@pytest.mark.parametrize("letter", ["G", "R", "S"])
def test_readme_table_has_five_levels(letter):
    assert sorted(_readme_ladder(letter)) == [f"{letter}{i}" for i in range(1, 6)]


def test_g_thresholds_match_readme():
    assert {lab: lo for lo, lab in G_THRESHOLDS} == _readme_ladder("G")


def test_s_thresholds_match_readme():
    assert {lab: lo for lo, lab in S_THRESHOLDS} == _readme_ladder("S")


@pytest.mark.parametrize("letter,fn", [("G", g_scale), ("R", r_scale), ("S", s_scale)])
def test_each_threshold_is_the_exact_boundary(letter, fn):
    """恰在門檻＝該級；略低於門檻＝前一級（或 None）。"""
    ladder = sorted(_readme_ladder(letter).items(), key=lambda kv: kv[1])
    for i, (label, lo) in enumerate(ladder):
        assert fn(lo) == label, f"{letter}: {lo} 應為 {label}"
        below = lo - 1e-3 if letter == "G" else lo * 0.999
        assert fn(below) == (ladder[i - 1][0] if i else None)


def test_kp_thirds_semantics():
    """5- (4.67) 不是 G1；9- (8.67) 是 G4、9o 才是 G5——與 SWPC 3 日預報的標示一致。"""
    assert g_scale(4.67) is None
    assert g_scale(4.9999999) == "G1"          # 浮點誤差容差
    assert g_scale(8.67) == "G4"
    assert g_scale(9.0) == "G5"


def test_missing_values_are_none_not_quiet():
    for fn in (g_scale, s_scale, r_scale):
        assert fn(None) is None
        assert fn(float("nan")) is None


def test_scale_rank():
    assert [scale_rank(x) for x in (None, "G1", "S3", "R5")] == [0, 1, 3, 5]
