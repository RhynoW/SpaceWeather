"""services.ingest.gfz_nowcast — GFZ Potsdam Kp/ap nowcast（來源 A5，備援兼近即時）。

解析邏輯移植自 Sat_TraingDataExtension/data/space_weather/fetch_space_weather.py，
差別在於：該案只取「每日最大 Kp」做事件標注，本案保留**逐 3 小時**原始值，
因為分級規則需要駐留時間（dwell）判斷，日彙整會把時間資訊丟掉。

GFZ 檔格式（每日一列，空白分隔，共 28 欄）：
  YYYY MM DD days days_m Bsr dB Kp1..Kp8 ap1..ap8 Ap SN F10.7obs F10.7adj D
  索引 7–14 為 Kp1–8（真實 Kp，非 ×10），15–22 為 ap1–8，23 為 Ap，24 為 SN，
  倒數第 3 欄為 F10.7 觀測值。
"""

from __future__ import annotations

import pandas as pd

from swx_core import empty_frame, normalize

from .base import Connector

_MISSING = -1.0


class GfzNowcastConnector(Connector):
    formats = ("gfz_fixed",)
    raw_ext = "txt"

    def parse(self, payload: bytes) -> pd.DataFrame:
        text = payload.decode("utf-8", errors="replace")
        recs: list[dict] = []

        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 28:
                continue
            try:
                year, mon, day = int(parts[0]), int(parts[1]), int(parts[2])
                kp_vals = [float(parts[i]) for i in range(7, 15)]
                ap_vals = [float(parts[i]) for i in range(15, 23)]
                ap_daily = float(parts[23])
                sn = float(parts[24])
                f107 = float(parts[-3])
            except (ValueError, IndexError):
                continue

            date = pd.Timestamp(year=year, month=mon, day=day, tz="UTC")

            for i, (kp, ap) in enumerate(zip(kp_vals, ap_vals)):
                offset = pd.Timedelta(hours=3 * i)
                if kp >= 0:
                    recs.append({"valid_time": date + offset, "param_code": "KP_3H",
                                 "value": kp, "unit": "1"})
                if ap >= 0:
                    recs.append({"valid_time": date + offset, "param_code": "AP_3H",
                                 "value": ap, "unit": "nT"})

            if ap_daily >= 0:
                recs.append({"valid_time": date, "param_code": "AP_AVG",
                             "value": ap_daily, "unit": "nT"})
            if f107 > 0:
                recs.append({"valid_time": date, "param_code": "F107_OBS",
                             "value": f107, "unit": "sfu"})
            if sn >= 0:
                recs.append({"valid_time": date, "param_code": "ISN",
                             "value": sn, "unit": "1"})

        if not recs:
            return empty_frame()

        df = pd.DataFrame(recs)
        df["source_id"] = self.spec.source_id
        df["source_tier"] = self.spec.tier
        df["data_type"] = "OBS"
        return normalize(df)
