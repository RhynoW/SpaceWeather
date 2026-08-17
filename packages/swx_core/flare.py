"""swx_core.flare — 太陽閃焰分級與 NOAA R 級對照。

太陽閃焰是磁場重聯釋放的高能量輻射爆發，其 0.1–0.8 nm 軟 X 射線峰值通量
由 NOAA GOES 衛星系列即時記錄，是國際通用的閃焰強度定義。
X 射線以光速抵達（約 8 分 20 秒），**無預警可言**——這是它與地磁暴的關鍵差異：
地磁暴有太陽風傳播時間（約 1–3 天）可供預警，閃焰只能即時偵測與事後影響評估。

閃焰分級（依 0.1–0.8 nm 峰值通量 W/m²）：
    A   1e-8    B   1e-7    C   1e-6    M   1e-5    X   1e-4
  類別後的數字為該級距內的倍數，例如 M5.2 = 5.2e-5、X1.0 = 1.0e-4。

NOAA R 級（無線電黑障）以峰值通量定義，直接對應 HF 通信中斷程度：
    R1 M1、R2 M5、R3 X1、R4 X10、R5 X20
"""

from __future__ import annotations

import math
import re

# 類別代號 → 該級距下限通量（W/m²）
CLASS_BASE: dict[str, float] = {
    "A": 1e-8,
    "B": 1e-7,
    "C": 1e-6,
    "M": 1e-5,
    "X": 1e-4,
}
_CLASS_ORDER = ("A", "B", "C", "M", "X")

# NOAA R 級門檻（峰值 0.1–0.8 nm 通量下限，W/m²）
R_SCALE: tuple[tuple[str, float, str], ...] = (
    ("R5", 2e-3, "X20"),
    ("R4", 1e-3, "X10"),
    ("R3", 1e-4, "X1"),
    ("R2", 5e-5, "M5"),
    ("R1", 1e-5, "M1"),
)

# R 級 → 本案任務風險等級（HF 通信網域之初版對照，須與需求單位校準）
R_TO_MISSION_LEVEL = {
    "R1": "L1",
    "R2": "L2",
    "R3": "L3",
    "R4": "L4",
    "R5": "L4",
}

_CLASS_RE = re.compile(r"^\s*([ABCMX])\s*([0-9]*\.?[0-9]*)\s*$", re.IGNORECASE)


def class_to_flux(flare_class: str) -> float | None:
    """'M5.2' → 5.2e-5。無法解析時回傳 None。"""
    if not flare_class:
        return None
    m = _CLASS_RE.match(str(flare_class))
    if not m:
        return None
    letter = m.group(1).upper()
    magnitude = float(m.group(2)) if m.group(2) else 1.0
    return CLASS_BASE[letter] * magnitude


def flux_to_class(flux: float | None, *, digits: int = 1) -> str | None:
    """5.2e-5 → 'M5.2'。低於 A 級或非有限值回傳 None。"""
    if flux is None or not math.isfinite(flux) or flux < CLASS_BASE["A"]:
        return None
    letter = _CLASS_ORDER[0]
    for cand in _CLASS_ORDER:
        if flux >= CLASS_BASE[cand]:
            letter = cand
    magnitude = flux / CLASS_BASE[letter]
    return f"{letter}{magnitude:.{digits}f}"


def r_scale(flux: float | None) -> str | None:
    """峰值通量 → NOAA R 級（未達 R1 回傳 None）。"""
    if flux is None or not math.isfinite(flux):
        return None
    for name, threshold, _label in R_SCALE:
        if flux >= threshold:
            return name
    return None


def mission_level(flux: float | None) -> str:
    """峰值通量 → 本案 L0–L4 任務風險等級（HF 通信網域）。"""
    r = r_scale(flux)
    return R_TO_MISSION_LEVEL.get(r, "L0") if r else "L0"


def hf_blackout_summary(flux: float | None) -> str:
    """依 NOAA R 級描述 HF 影響（事件卡敘述用）。"""
    r = r_scale(flux)
    return {
        "R1": "日照側 HF 短暫衰減，低頻端偶有失聯；影響約持續數十分鐘。",
        "R2": "日照側 HF 部分頻段中斷約數十分鐘，低頻導航訊號短暫降級。",
        "R3": "日照側 HF 大範圍中斷約一小時，低頻導航誤差增加。",
        "R4": "日照側 HF 中斷約一至二小時，衛星導航與低頻導航明顯降級。",
        "R5": "日照側 HF 完全中斷數小時，導航訊號嚴重降級。",
    }.get(r or "", "未達 R1 門檻，HF 無顯著影響。")


def is_stronger(a: str | None, b: str | None) -> bool:
    """比較兩個閃焰類別字串，a 是否強於 b。"""
    fa, fb = class_to_flux(a or ""), class_to_flux(b or "")
    if fa is None:
        return False
    if fb is None:
        return True
    return fa > fb
