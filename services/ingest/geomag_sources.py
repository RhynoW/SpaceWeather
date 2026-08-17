"""services.ingest.geomag_sources — 高解析地磁指數與極光橢圓。

  gfz_hp30    GFZ Hp30／ap30：30 分鐘解析度的地磁指數（1985 年起）。
              Kp 是 3 小時值，暴起始時刻會被糊掉 1–2 小時；「提前量」是構想書
              明列的 KPI，用 3 小時解析度去量測 1 小時等級的提前量並不合理。
              Hp30 把時間解析度提高 6 倍，直接改善這個量測。

  swpc_ovation  極光橢圓沉降能通量機率格網（30–90 分鐘預報）。
              臺灣位處低緯，極光本身無直接影響；有用的是**極光橢圓赤道側邊界緯度**
              ——它是地磁暴強度的直觀指標，邊界越往低緯推進代表擾動越深入，
              也是高緯路徑 HF 與極區 GNSS 閃爍的判定依據。
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from swx_core import empty_frame, normalize

from .base import Connector

#: 判定極光橢圓邊界所用的機率門檻（%）
AURORA_BOUNDARY_PROB = 10.0


class GfzHp30Connector(Connector):
    """GFZ Hp30／ap30 逐 30 分鐘地磁指數。

    檔案自 1985 年起、逾 70 萬列。以 window_days 限制只解析近期，
    避免例行擷取每次都處理整份歷史。
    """

    formats = ("gfz_hp30_txt",)
    raw_ext = "txt"

    def parse(self, payload: bytes) -> pd.DataFrame:
        recs: list[dict] = []
        for line in payload.decode("utf-8", errors="replace").splitlines():
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 9:
                continue
            try:
                year, mon, day = int(parts[0]), int(parts[1]), int(parts[2])
                hour = float(parts[3])
                hp30, ap30 = float(parts[7]), float(parts[8])
            except (ValueError, IndexError):
                continue
            if hp30 < 0:            # 缺值碼
                continue
            ts = pd.Timestamp(year=year, month=mon, day=day, tz="UTC") + pd.Timedelta(hours=hour)
            recs.extend(
                [
                    {"valid_time": ts, "param_code": "HP30", "value": hp30, "unit": "1"},
                    {"valid_time": ts, "param_code": "AP30", "value": ap30, "unit": "nT"},
                ]
            )

        if not recs:
            return empty_frame()
        df = pd.DataFrame(recs)

        window_days = self.spec.raw.get("window_days")
        if window_days:
            cutoff = df["valid_time"].max() - pd.Timedelta(days=float(window_days))
            df = df[df["valid_time"] >= cutoff]

        df["source_id"] = self.spec.source_id
        df["source_tier"] = self.spec.tier
        df["data_type"] = "OBS"
        return normalize(df)


class SwpcOvationConnector(Connector):
    """SWPC OVATION 極光橢圓機率格網 → 邊界緯度與最大機率。"""

    formats = ("swpc_ovation_json",)
    raw_ext = "json"

    def parse(self, payload: bytes) -> pd.DataFrame:
        data = json.loads(payload.decode("utf-8", errors="replace"))
        coords = data.get("coordinates")
        if not coords:
            return empty_frame()

        forecast_time = pd.to_datetime(data.get("Forecast Time"), utc=True, errors="coerce")
        if pd.isna(forecast_time):
            forecast_time = pd.to_datetime(data.get("Observation Time"), utc=True, errors="coerce")
        if pd.isna(forecast_time):
            return empty_frame()

        grid = np.asarray(coords, dtype=float)     # 欄位為 [lon, lat, prob]
        lat, prob = grid[:, 1], grid[:, 2]

        lit = prob >= AURORA_BOUNDARY_PROB
        recs = [
            {"valid_time": forecast_time, "param_code": "AURORA_MAX_PROB",
             "value": float(np.nanmax(prob)), "unit": "%"},
        ]
        if lit.any():
            # 北半球橢圓的赤道側邊界：機率達門檻的最低北緯
            north = lat[lit & (lat > 0)]
            if north.size:
                recs.append(
                    {"valid_time": forecast_time, "param_code": "AURORA_BOUNDARY_LAT",
                     "value": float(np.nanmin(north)), "unit": "deg"}
                )

        df = pd.DataFrame(recs)
        df["source_id"] = self.spec.source_id
        df["source_tier"] = self.spec.tier
        df["data_type"] = "PRD"      # OVATION 是 30–90 分鐘預報，非觀測
        return normalize(df)
