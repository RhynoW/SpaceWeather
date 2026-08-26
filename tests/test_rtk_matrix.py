"""RTK 網域：門檻必須標註出處，矩陣不得有空格。

兩件事各自守一個容易失守的地方：

  **門檻出處**　I95 的 8／20／30 是內政部國土測繪中心的公告值，不是本案標定的。
                這是它最大的價值——唯一有作業單位背書的判據。有人把門檻「調順眼」
                而沒改出處，交付文件就會用官方名義背書一組沒人核可過的數字。

  **矩陣完整**　影響矩陣最常見的失敗不是寫錯，是**漏格**。少一格在畫面上看起來
                只是空白，讀者會當成「這個組合沒有風險」，而事實是沒有人判斷過。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from services.risk_engine.engine import load_rules

ROOT = Path(__file__).resolve().parents[1]
RULES = yaml.safe_load((ROOT / "configs" / "rules" / "gnss_rtk.yaml").read_text(encoding="utf-8"))
MATRIX = yaml.safe_load((ROOT / "configs" / "matrix" / "rtk.yaml").read_text(encoding="utf-8"))

#: 國土測繪中心公告的作業判讀門檻。改這組數字必須連同 threshold_sources 一起改。
OFFICIAL_I95 = [8, 20, 30]


def test_thresholds_cite_their_source():
    sources = RULES.get("threshold_sources") or {}
    assert "I95" in sources, "I95 門檻缺少出處宣告"
    i95 = sources["I95"]
    for field in ("authority", "url", "statement", "values"):
        assert i95.get(field), f"I95 出處缺少 {field}"
    assert "國土測繪中心" in i95["authority"], "I95 門檻的權威單位標錯"
    assert i95["values"] == OFFICIAL_I95


def test_rule_thresholds_match_the_cited_values():
    """規則裡的數字必須就是出處宣告的數字，不得各寫各的。"""
    used = sorted({c.value for r in load_rules()
                   if r.domain == "GNSS_RTK"
                   for c in r.conditions if c.param == "I95"})
    assert used == [float(v) for v in OFFICIAL_I95], (
        f"規則使用的 I95 門檻 {used} 與公告值 {OFFICIAL_I95} 不符")


def test_scintillation_thresholds_are_marked_as_uncalibrated():
    """S4 門檻是本案自訂，不得混進「官方公告」的敘述裡。"""
    s4 = (RULES.get("threshold_sources") or {}).get("S4", {})
    assert s4, "S4 門檻缺少出處宣告"
    assert "自訂" in s4.get("authority", ""), "S4 門檻不得宣稱為官方值"
    assert RULES.get("calibrated") is False


def test_every_i95_rule_declares_its_network():
    """分網判讀：三個網的值差很多，沒宣告 region 等於讓列序決定判讀結果。"""
    for rule in load_rules():
        if rule.domain != "GNSS_RTK":
            continue
        if any(c.param == "I95" for c in rule.conditions):
            assert rule.region, f"{rule.rule_id} 未宣告 region"


def test_matrix_has_no_empty_cells():
    modes = MATRIX["modes"]
    events = MATRIX["event_types"]
    cells = MATRIX["cells"]
    assert set(cells) == set(modes), "cells 的模式與 modes 不一致"
    for mode in modes:
        missing = sorted(set(events) - set(cells[mode]))
        assert not missing, f"{mode} 缺少事件型態：{missing}"
        for event, cell in cells[mode].items():
            for field in ("level", "short_baseline", "long_baseline", "action"):
                assert str(cell.get(field, "")).strip(), f"{mode}×{event} 的 {field} 是空的"
            assert cell["level"] in ("L0", "L1", "L2", "L3", "L4")


def test_matrix_declares_both_modes_and_they_differ():
    """單基站與網路 RTK 的主導量不同；合併成一套判準會同時誤導兩邊。"""
    modes = MATRIX["modes"]
    assert {"single_base", "network_vbs"} <= set(modes)
    assert modes["single_base"]["primary_index"] != modes["network_vbs"]["primary_index"]
    assert "I95" in modes["network_vbs"]["primary_index"]


def test_single_base_baseline_bands_are_contiguous():
    """基線分層不得有斷帶或重疊——中間漏掉的距離會沒有任何建議可查。"""
    bands = MATRIX["modes"]["single_base"]["baseline_bands"]
    edges = [b["range_km"] for b in bands]
    assert edges[0][0] == 0
    for (_, hi), (lo, _) in zip(edges, edges[1:]):
        assert hi == lo, f"分層不連續：{hi} → {lo}"
    assert edges[-1][1] is None, "最後一層必須開放到無限遠"


@pytest.mark.parametrize("group", ["space_environment", "service_and_equipment"])
def test_triage_lists_both_classes_of_cause(group):
    """兩類肇因都要有內容。少了設備那一類，現場會把所有問題都推給太空天氣。"""
    items = MATRIX["triage"][group]
    assert len(items) >= 4
    for item in items:
        assert item.get("check") and item.get("source")


def test_service_exclusions_reach_every_rule():
    """服務與設備因素必須掛在每一條規則上，事件卡才會帶著它一起發出去。

    只寫在矩陣裡不夠——現場看到的是事件卡，不是設定檔。
    """
    n_service = len(MATRIX["triage"]["service_and_equipment"])
    rtk_rules = [r for r in load_rules() if r.domain == "GNSS_RTK"]
    assert rtk_rules
    for rule in rtk_rules:
        assert len(rule.exclusions) >= n_service, f"{rule.rule_id} 的排除清單不完整"
        assert any("RTCM" in e for e in rule.exclusions)
        assert any("NTRIP" in e for e in rule.exclusions)
