"""orbit_drag — 熱氣層密度、大氣阻力與軌道風險產品（架構書 §7.1）。

自 Sat_TraingDataExtension 移入並整理（見 packages/SOURCE_MAP.md）。
物理與該案一致，差異在於驅動參數改由 swx_core 資料層供給，因而支援 as_of 回放。
"""

from .alongtrack import (
    BC_REFERENCE,
    DEFAULT_BC,
    Scenario,
    alongtrack_km,
    compare,
    constant_drivers,
    propagate,
)
from .atmospheric import (
    MU,
    RE,
    density,
    density_ratio,
    drag_residual,
    is_reentry_decay,
    load_space_weather,
)

__all__ = [
    "MU", "RE",
    "BC_REFERENCE", "DEFAULT_BC", "Scenario",
    "alongtrack_km", "compare", "constant_drivers", "propagate",
    "density", "density_ratio", "drag_residual", "is_reentry_decay",
    "load_space_weather",
]
