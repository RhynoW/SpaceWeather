"""geomag.reference_field — 全球地磁參考場與地磁座標（構想書議題二）。

議題二要求「建立全球地磁場基準模型與臺灣周邊地磁擾動評估方法」。這兩件事必須分開看：

  **基準場**（本模組）    IGRF-14 是國際標準，離線可算，**不需要任何在地觀測資料**。
                          這部分現在就能完成，不必等外部協調。
  **區域擾動**            ΔB = B_觀測 − B_IGRF，需要在地磁力計即時串流（架構書 C3）。
                          本模組提供計算框架與代理指標，實測資料到位即可直接接上。

誠實界定：在取得臺灣磁力計實測前，`regional_disturbance_proxy()` 只是以全球指數
配合地磁緯度做的**推估**，不是實測。所有輸出都帶 `is_proxy=True`，
事件卡與報告必須據此標示，不可讓使用者誤以為是在地實測。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

#: 臺灣周邊主要地磁觀測點（Lunping 為 INTERMAGNET 站，中央氣象署運作）
STATIONS: dict[str, tuple[float, float, float]] = {
    # 代碼: (緯度 °N, 經度 °E, 高度 km)
    "LNP": (25.00, 121.17, 0.0),      # 崙坪，INTERMAGNET 代碼 LNP
    "KUL": (22.60, 120.60, 0.0),      # 高雄周邊參考點
    "HUA": (23.98, 121.61, 0.0),      # 花蓮周邊參考點
}

#: 臺灣代表點（用於區域指標）
TW_LAT, TW_LON = 23.5, 121.0


@dataclass(frozen=True)
class FieldVector:
    """單點的地磁參考場。

    X 北向、Y 東向、Z 下向（地磁學慣例，與一般 ENU 的 Up 方向相反）。
    """

    lat: float
    lon: float
    epoch: datetime
    x_north: float
    y_east: float
    z_down: float

    @property
    def h(self) -> float:
        """水平分量強度（nT）。地磁暴期間 ΔH 是最常用的擾動量。"""
        return float(np.hypot(self.x_north, self.y_east))

    @property
    def f(self) -> float:
        """總場強度（nT）。"""
        return float(np.sqrt(self.x_north**2 + self.y_east**2 + self.z_down**2))

    @property
    def declination_deg(self) -> float:
        """磁偏角 D（東偏為正）。"""
        return float(np.degrees(np.arctan2(self.y_east, self.x_north)))

    @property
    def inclination_deg(self) -> float:
        """磁傾角 I（下傾為正）。"""
        return float(np.degrees(np.arctan2(self.z_down, self.h)))

    def to_dict(self) -> dict:
        return {
            "lat": self.lat, "lon": self.lon,
            "epoch": self.epoch.isoformat(),
            "X_north_nT": round(self.x_north, 1),
            "Y_east_nT": round(self.y_east, 1),
            "Z_down_nT": round(self.z_down, 1),
            "H_nT": round(self.h, 1),
            "F_nT": round(self.f, 1),
            "D_deg": round(self.declination_deg, 3),
            "I_deg": round(self.inclination_deg, 3),
        }


def igrf_field(lat: float, lon: float, epoch: datetime, alt_km: float = 0.0) -> FieldVector:
    """以 IGRF-14 計算單點參考場。

    ppigrf 回傳 (Be 東, Bn 北, Bu 上)；本模組轉為地磁慣例的 (X 北, Y 東, Z 下)。
    """
    import ppigrf

    be, bn, bu = ppigrf.igrf(lon, lat, alt_km, epoch)
    scalar = (float(np.atleast_1d(v).ravel()[0]) for v in (be, bn, bu))
    be_v, bn_v, bu_v = scalar
    return FieldVector(
        lat=lat, lon=lon, epoch=epoch,
        x_north=bn_v, y_east=be_v, z_down=-bu_v,
    )


def station_fields(epoch: datetime) -> pd.DataFrame:
    """臺灣周邊各測點的參考場（議題二「全球地磁場基準模型」之在地實例）。"""
    rows = []
    for code, (lat, lon, alt) in STATIONS.items():
        fv = igrf_field(lat, lon, epoch, alt)
        rows.append({"station": code, **fv.to_dict()})
    return pd.DataFrame(rows)


def geomagnetic_latitude(lat: float, lon: float, epoch: datetime) -> float:
    """由 IGRF 磁傾角反推的地磁緯度（dipole 近似）。

    tan(I) = 2·tan(λ_m) → λ_m = arctan(tan(I)/2)

    為什麼需要它：電離層現象（赤道異常、閃爍帶、極蓋吸收）依**地磁**緯度分布，
    不是地理緯度。臺灣地理緯度約 23.5°N，地磁緯度僅約 14°N，正落在赤道異常
    駝峰區——這是臺灣 GNSS 閃爍風險偏高的物理原因，也是區域模型必須處理的重點。
    """
    fv = igrf_field(lat, lon, epoch)
    return float(np.degrees(np.arctan(np.tan(np.radians(fv.inclination_deg)) / 2.0)))


def regional_disturbance(
    observed: pd.DataFrame,
    *,
    station: str = "LNP",
    epoch: datetime | None = None,
) -> pd.DataFrame:
    """由實測磁場計算區域擾動 ΔH／ΔF（相對 IGRF 基準場）。

    observed 需含 valid_time 與 x_north／y_east／z_down（或 h／f）欄位。
    這是議題二的**正式作法**，但需要在地磁力計資料（架構書 C3）。
    """
    if station not in STATIONS:
        raise KeyError(f"未知測站 {station}；已知：{sorted(STATIONS)}")
    lat, lon, alt = STATIONS[station]
    ref = igrf_field(lat, lon, epoch or datetime.now(), alt)

    out = observed.copy()
    if "h" not in out.columns and {"x_north", "y_east"} <= set(out.columns):
        out["h"] = np.hypot(out["x_north"], out["y_east"])
    if "f" not in out.columns and {"x_north", "y_east", "z_down"} <= set(out.columns):
        out["f"] = np.sqrt(out["x_north"] ** 2 + out["y_east"] ** 2 + out["z_down"] ** 2)

    if "h" in out.columns:
        out["dH_nT"] = out["h"] - ref.h
    if "f" in out.columns:
        out["dF_nT"] = out["f"] - ref.f
    out["station"] = station
    out["is_proxy"] = False
    return out


def regional_disturbance_proxy(
    dst: pd.Series | None = None,
    kp: pd.Series | None = None,
    *,
    lat: float = TW_LAT,
    lon: float = TW_LON,
    epoch: datetime | None = None,
) -> pd.DataFrame:
    """**代理指標**：無在地實測時，由全球指數推估臺灣周邊 ΔH。

    物理依據：環電流造成的地表水平分量壓抑近似隨地磁緯度餘弦分布，
        ΔH(λ_m) ≈ Dst · cos(λ_m)
    臺灣地磁緯度約 14°N，cos ≈ 0.97，故低緯地區的 ΔH 與 Dst 相當接近。

    **這是推估，不是實測**。輸出一律標 is_proxy=True，事件卡與報告必須據此標示。
    誤差來源包括：地磁暴期間電離層電流系統（EEJ、DP2）的區域貢獻、
    測站在地感應效應，這些都只有實測能捕捉。
    """
    epoch = epoch or datetime.now()
    mlat = geomagnetic_latitude(lat, lon, epoch)
    factor = float(np.cos(np.radians(mlat)))

    if dst is not None and not dst.empty:
        est = dst.astype(float) * factor
        source = "dst"
    elif kp is not None and not kp.empty:
        # Kp → 約略 Dst 的經驗換算（僅供無 Dst 時的粗估）
        est = -20.0 * (kp.astype(float).clip(lower=0) ** 1.6) * factor
        source = "kp_fallback"
    else:
        return pd.DataFrame(columns=["valid_time", "dH_est_nT", "is_proxy", "basis"])

    return pd.DataFrame(
        {
            "valid_time": est.index,
            "dH_est_nT": est.to_numpy(),
            "geomagnetic_lat": round(mlat, 2),
            "scale_factor": round(factor, 4),
            "is_proxy": True,
            "basis": source,
        }
    ).reset_index(drop=True)


def summary(epoch: datetime | None = None) -> dict:
    """議題二基準場交付摘要（供報告與 API）。"""
    epoch = epoch or datetime.now()
    tw = igrf_field(TW_LAT, TW_LON, epoch)
    return {
        "model": "IGRF-14 (ppigrf)",
        "epoch": epoch.isoformat(),
        "taiwan_reference_point": {"lat": TW_LAT, "lon": TW_LON, **tw.to_dict()},
        "geomagnetic_latitude_deg": round(geomagnetic_latitude(TW_LAT, TW_LON, epoch), 2),
        "stations": station_fields(epoch).to_dict(orient="records"),
        "note": (
            "基準場已完成，離線可算，不需外部資料。區域擾動 ΔH 需在地磁力計實測"
            "（架構書 C3）；在此之前只能使用 regional_disturbance_proxy()，"
            "其輸出標記 is_proxy=True。"
        ),
    }
