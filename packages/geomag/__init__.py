"""geomag — 地磁參考場與區域擾動評估（構想書議題二）。

基準場（IGRF-14）離線可算、現在就能交付；區域擾動需在地磁力計實測（架構書 C3），
在此之前只有標記 is_proxy=True 的推估值。兩者不可混為一談。
"""

from .reference_field import (
    STATIONS,
    TW_LAT,
    TW_LON,
    FieldVector,
    geomagnetic_latitude,
    igrf_field,
    regional_disturbance,
    regional_disturbance_proxy,
    station_fields,
    summary,
)

__all__ = [
    "STATIONS", "TW_LAT", "TW_LON", "FieldVector",
    "igrf_field", "station_fields", "geomagnetic_latitude",
    "regional_disturbance", "regional_disturbance_proxy", "summary",
]
