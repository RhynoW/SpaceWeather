"""swx_core.scales — NOAA Space Weather Scales（G／R／S）單一實作。

依 https://www.swpc.noaa.gov/noaa-scales-explanation 定義，本系統不自訂、不修改。
三套尺度**各自獨立**，同一階不代表等價（README「分級術語」）。

  G 地磁暴     依 Kp：G1=5、G2=6、G3=7、G4=8（含 9-）、G5=9
  R 無線電衰減  依 GOES 0.1–0.8 nm X 射線峰值：見 swx_core.flare.r_scale
  S 輻射風暴    依 ≥10 MeV 積分質子：S1=10、S2=10²、S3=10³、S4=10⁴、S5=10⁵ pfu

在此之前只有 R 級有程式實作；G、S 只以規則 YAML 的 `scale_hint` 字串存在，
R/S/G 狀態列與 72 小時展望需要能由數值換算等級，故補齊於此。
"""

from __future__ import annotations

import math

from .flare import r_scale

__all__ = ["G_THRESHOLDS", "S_THRESHOLDS", "g_scale", "s_scale", "r_scale", "scale_rank"]

#: (Kp 下限, 等級)，由高到低比對
G_THRESHOLDS: tuple[tuple[float, str], ...] = (
    (9.0, "G5"), (8.0, "G4"), (7.0, "G3"), (6.0, "G2"), (5.0, "G1"),
)

#: (≥10 MeV 質子 pfu 下限, 等級)
S_THRESHOLDS: tuple[tuple[float, str], ...] = (
    (1e5, "S5"), (1e4, "S4"), (1e3, "S3"), (1e2, "S2"), (1e1, "S1"),
)

# Kp 以三分位表示（5- = 4.67、5o = 5.00）。浮點運算可能得到 4.99999，
# 給一點容差，避免 5.00 被判為未達 G1。NOAA 只把 5o 以上算 G1，5- 不算——
# SWPC 3 日預報同樣只在 5.00 旁標 (G1)，4.67 不標。
_KP_EPS = 1e-6


def _bad(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def g_scale(kp: float | None) -> str | None:
    """Kp → G 級；未達 G1 或無值回傳 None。"""
    if _bad(kp):
        return None
    for lo, label in G_THRESHOLDS:
        if float(kp) + _KP_EPS >= lo:
            return label
    return None


def s_scale(pfu: float | None) -> str | None:
    """≥10 MeV 積分質子通量（pfu）→ S 級；未達 S1 或無值回傳 None。"""
    if _bad(pfu):
        return None
    for lo, label in S_THRESHOLDS:
        if float(pfu) >= lo:
            return label
    return None


def scale_rank(label: str | None) -> int:
    """'G3' → 3；None → 0。供取最大值與排序。"""
    if not label:
        return 0
    try:
        return int(label[1:])
    except ValueError:
        return 0
