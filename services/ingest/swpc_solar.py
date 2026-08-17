"""services.ingest.swpc_solar — GOES 太陽閃焰事件與太陽活動區（架構書 C1）。

兩個來源，都直接支援「太陽閃焰」這條鏈：

  swpc_flare_json         GOES X 射線閃焰事件表（begin/max/end 時間與分級）。
                          與 X 射線時序（swpc_xray）的差別：時序是連續通量，
                          這裡是**已判定的離散事件**，可直接對應事件卡的時間軸欄位。

  swpc_solar_regions_json 太陽活動區表，含黑子分類、磁分類與 **M/X 級閃焰發生機率**。
                          機率欄位是議題三（短時預報）少數現成可用的預報因子，
                          屬於「來源即預報」而非本系統自行計算，故標 data_type=PRD。

閃焰的時效特性必須在架構上被正確對待：X 射線以光速抵達（約 8 分 20 秒），
沒有預警空間，只能即時偵測與影響評估；能提前的是「活動區閃焰機率」這類統計預報。
"""

from __future__ import annotations

import json

import pandas as pd

from swx_core import empty_frame, normalize
from swx_core.flare import class_to_flux

from .base import Connector


class SwpcFlareConnector(Connector):
    """GOES X 射線閃焰事件表。"""

    formats = ("swpc_flare_json",)
    raw_ext = "json"

    def parse(self, payload: bytes) -> pd.DataFrame:
        data = json.loads(payload.decode("utf-8", errors="replace"))
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list) or not data:
            return empty_frame()

        raw = pd.DataFrame(data)
        recs: list[dict] = []
        for _, row in raw.iterrows():
            max_time = pd.to_datetime(row.get("max_time"), utc=True, errors="coerce")
            if pd.isna(max_time):
                continue

            # 峰值通量：優先用數值欄，缺漏時由分級字串還原
            peak = row.get("max_xrlong")
            if peak is None or pd.isna(peak):
                peak = class_to_flux(str(row.get("max_class") or ""))
            if peak is None or pd.isna(peak):
                continue

            recs.append(
                {
                    "valid_time": max_time,
                    "param_code": "FLARE_PEAK",
                    "value": float(peak),
                    "unit": "W/m^2",
                }
            )

            # 事件持續時間（分鐘）——事件卡「預估持續時間」欄位的實測值
            begin = pd.to_datetime(row.get("begin_time"), utc=True, errors="coerce")
            end = pd.to_datetime(row.get("end_time"), utc=True, errors="coerce")
            if not pd.isna(begin) and not pd.isna(end):
                recs.append(
                    {
                        "valid_time": max_time,
                        "param_code": "FLARE_DURATION",
                        "value": (end - begin).total_seconds() / 60.0,
                        "unit": "min",
                    }
                )

        if not recs:
            return empty_frame()
        df = pd.DataFrame(recs)
        df["source_id"] = self.spec.source_id
        df["source_tier"] = self.spec.tier
        df["data_type"] = "OBS"
        return normalize(df)

    def flare_events(self, payload: bytes) -> pd.DataFrame:
        """回傳事件層級的表（供事件卡直接取用 begin/max/end 與分級）。"""
        data = json.loads(payload.decode("utf-8", errors="replace"))
        if isinstance(data, dict):
            data = [data]
        raw = pd.DataFrame(data)
        if raw.empty:
            return raw
        for col in ("begin_time", "max_time", "end_time"):
            if col in raw.columns:
                raw[col] = pd.to_datetime(raw[col], utc=True, errors="coerce")
        return raw


class SwpcSolarRegionsConnector(Connector):
    """太陽活動區表（含 M/X 級閃焰機率）。"""

    formats = ("swpc_solar_regions_json",)
    raw_ext = "json"

    def parse(self, payload: bytes) -> pd.DataFrame:
        data = json.loads(payload.decode("utf-8", errors="replace"))
        if not isinstance(data, list) or not data:
            return empty_frame()

        raw = pd.DataFrame(data)
        if "observed_date" not in raw.columns:
            return empty_frame()
        raw["date"] = pd.to_datetime(raw["observed_date"], utc=True, errors="coerce")
        raw = raw.dropna(subset=["date"])
        if raw.empty:
            return empty_frame()

        for col in ("area", "number_spots", "c_flare_probability",
                    "m_flare_probability", "x_flare_probability"):
            if col in raw.columns:
                raw[col] = pd.to_numeric(raw[col], errors="coerce")

        recs: list[dict] = []
        for date, grp in raw.groupby("date"):
            recs.append({"valid_time": date, "param_code": "AR_COUNT",
                         "value": float(len(grp)), "unit": "1", "data_type": "OBS"})
            if "area" in grp:
                total_area = grp["area"].sum(min_count=1)
                if pd.notna(total_area):
                    recs.append({"valid_time": date, "param_code": "AR_AREA_TOTAL",
                                 "value": float(total_area), "unit": "uHem",
                                 "data_type": "OBS"})
            # 全日盤面機率：以各活動區獨立為近似，取「至少一區發生」之聯合機率
            for col, code in (("m_flare_probability", "M_FLARE_PROB"),
                              ("x_flare_probability", "X_FLARE_PROB")):
                if col not in grp:
                    continue
                p = grp[col].dropna() / 100.0
                if p.empty:
                    continue
                joint = 1.0 - float((1.0 - p).prod())
                recs.append({"valid_time": date, "param_code": code,
                             "value": joint, "unit": "1", "data_type": "PRD"})

        if not recs:
            return empty_frame()
        df = pd.DataFrame(recs)
        df["source_id"] = self.spec.source_id
        df["source_tier"] = self.spec.tier
        return normalize(df)
