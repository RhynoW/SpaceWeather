"""services.exporter.aurora_geojson — OVATION 極光橢圓赤道側邊界（GeoJSON）。

資料層只存一個純量 AURORA_BOUNDARY_LAT（全球最低的那一點）。但邊界在不同經度
差很多：同一時刻，北美側可能推進到 50°N、亞洲側還在 65°N。高緯 HF 路徑與
極區航路要看的是**自己那一段經度**的邊界，所以這裡回到 OVATION 的原始格網
（360 經度 × 181 緯度，每格一個極光機率），逐經度取邊界，輸出可直接疊進
GIS 的線圖層。

逐經度的邊界：
  北半球  機率 ≥ 門檻的**最低**北緯
  南半球  機率 ≥ 門檻的**最高**南緯（同為赤道側）
某經度沒有任何格點達門檻 → 該處斷線（MultiLineString 的分段），**不插值補線**：
補出來的線會讓人以為那裡有邊界。

OVATION 是 30–90 分鐘預報（模式輸出），不是觀測；properties 照實標示。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_THRESHOLD = 10.0     # 與 AURORA_BOUNDARY_LAT 的判定門檻一致（geomag_sources）
SOURCE_ID = "swpc_ovation"


class NoAuroraData(RuntimeError):
    """沒有 OVATION 原始落地檔（API 據此回 503）。"""


def latest_raw(raw_root: Path) -> Path:
    root = Path(raw_root) / SOURCE_ID
    files = sorted(root.rglob("*.json")) if root.exists() else []
    if not files:
        raise NoAuroraData(
            "沒有 OVATION 原始落地檔；請先執行 "
            f"`python -m services.ingest.run --source {SOURCE_ID}`")
    return files[-1]


def _segments(lons: np.ndarray, lats: np.ndarray) -> list[list[list[float]]]:
    """依經度排序，遇到缺值或經度不連續就斷開；少於兩點的段丟棄（線需兩點）。"""
    segs, cur, prev = [], [], None
    for lon, lat in zip(lons, lats):
        if np.isnan(lat) or (prev is not None and lon - prev > 1.5):
            if len(cur) >= 2:
                segs.append(cur)
            cur = []
        if not np.isnan(lat):
            cur.append([round(float(lon), 1), round(float(lat), 1)])
        prev = lon
    if len(cur) >= 2:
        segs.append(cur)
    return segs


def boundary_from_payload(payload: dict, *, threshold: float = DEFAULT_THRESHOLD) -> dict:
    grid = np.asarray(payload.get("coordinates") or [], dtype=float)
    if grid.ndim != 2 or grid.shape[1] < 3 or not len(grid):
        raise NoAuroraData("OVATION 檔沒有格網資料")
    lon = np.where(grid[:, 0] > 180, grid[:, 0] - 360, grid[:, 0])   # GeoJSON 經度 −180..180
    lat, prob = grid[:, 1], grid[:, 2]
    lit = prob >= threshold

    features = []
    for hemi, sel, pick in (("north", lat > 0, "min"), ("south", lat < 0, "max")):
        mask = lit & sel
        df = pd.DataFrame({"lon": lon[mask], "lat": lat[mask]})
        all_lons = np.unique(lon)
        edge = (df.groupby("lon")["lat"].agg(pick).reindex(all_lons)
                if not df.empty else pd.Series(np.nan, index=all_lons))
        segs = _segments(edge.index.to_numpy(dtype=float), edge.to_numpy(dtype=float))
        valid = edge.dropna()
        features.append({
            "type": "Feature",
            "geometry": {"type": "MultiLineString", "coordinates": segs} if segs else None,
            "properties": {
                "hemisphere": hemi,
                "threshold_percent": threshold,
                "longitudes_with_boundary": int(valid.size),
                # 北半球取最低緯度、南半球取最高緯度——都是「最靠赤道」的那一點
                "most_equatorward_lat": (None if valid.empty else
                                         float(valid.min() if hemi == "north" else valid.max())),
                "most_equatorward_lon": (None if valid.empty else
                                         float(valid.idxmin() if hemi == "north" else valid.idxmax())),
            },
        })

    return {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "product": "aurora_equatorward_boundary",
            "schema_version": "1.0",
            "source": "NOAA SWPC OVATION Prime (ovation_aurora_latest.json)",
            "observation_time": payload.get("Observation Time"),
            "forecast_time": payload.get("Forecast Time"),
            "data_type": "model forecast (30–90 min lead), not an observation",
            "method": ("per-longitude equatorward-most grid cell with aurora probability "
                       f"≥ {threshold:g}%; gaps left open, not interpolated"),
            "grid_resolution_deg": 1.0,
            "attribution": "NOAA/NWS Space Weather Prediction Center — U.S. Government work, public domain",
        },
    }


def build(raw_root: Path, *, threshold: float = DEFAULT_THRESHOLD) -> dict:
    path = latest_raw(raw_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        raise NoAuroraData(f"OVATION 原始檔無法解析：{path.name}") from exc
    out = boundary_from_payload(payload, threshold=threshold)
    out["properties"]["raw_file"] = path.name
    return out
