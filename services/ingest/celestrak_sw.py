"""services.ingest.celestrak_sw — CelesTrak CSSI 太空天氣檔擷取（來源 A3）。

此來源同時扮演兩個角色：
  1. F10.7 / Kp / ap / Ap / ISN 的主資料源
  2. **STK 匯入檔的資料本體** —— 匯出端只是把同一份資料再寫回 CSSI 格式

解析交由 swx_core.cssi（格式的唯一權威實作），本模組只負責取得與轉長表。
"""

from __future__ import annotations

import pandas as pd

from swx_core import cssi

from .base import Connector


class CelestrakSpaceWeatherConnector(Connector):
    formats = ("cssi_txt",)
    raw_ext = "txt"

    def parse(self, payload: bytes) -> pd.DataFrame:
        text = payload.decode("utf-8", errors="replace")
        wide = cssi.parse_text(text)
        if wide.empty:
            return wide
        obs = cssi.to_observations(
            wide, source_id=self.spec.source_id, source_tier=self.spec.tier
        )
        return obs

    def parse_wide(self, payload: bytes) -> pd.DataFrame:
        """保留寬表（含 BSRN/ND/Cp/C9 等匯出時需要的欄位）。"""
        return cssi.parse_text(payload.decode("utf-8", errors="replace"))


class CelestrakCsvConnector(Connector):
    """CelesTrak SW-All 的 CSV 版本。

    存在的理由：Sat_TraingDataExtension 既有的 `space_weather_ap.csv` 就是這個版本，
    直接接進來即可承接該案 2021-01-01→2041-10-01 的完整歷史，不必重抓。
    注意 CSV 版的欄位順序與文字版不同（CSV 為 OBS 在前、ADJ 在後）。
    """

    formats = ("cssi_csv",)
    raw_ext = "csv"

    _DAILY = {
        "F10.7_OBS": ("F107_OBS", "sfu"),
        "F10.7_ADJ": ("F107_ADJ", "sfu"),
        "F10.7_OBS_CENTER81": ("F107_OBS_C81", "sfu"),
        "F10.7_ADJ_CENTER81": ("F107_ADJ_C81", "sfu"),
        "AP_AVG": ("AP_AVG", "nT"),
        "ISN": ("ISN", "1"),
    }

    def parse(self, payload: bytes) -> pd.DataFrame:
        import io

        from swx_core import normalize

        df = pd.read_csv(io.BytesIO(payload))
        df["date"] = pd.to_datetime(df["DATE"], utc=True)
        data_type = df.get("F10.7_DATA_TYPE", pd.Series(["OBS"] * len(df)))

        recs: list[dict] = []
        for col, (code, unit) in self._DAILY.items():
            if col not in df.columns:
                continue
            sub = df[["date", col]].dropna()
            recs.append(
                pd.DataFrame(
                    {
                        "valid_time": sub["date"],
                        "param_code": code,
                        "value": pd.to_numeric(sub[col], errors="coerce"),
                        "unit": unit,
                        "data_type": data_type.loc[sub.index],
                    }
                )
            )

        for i in range(1, 9):
            offset = pd.Timedelta(hours=3 * (i - 1))
            for col, code, unit, scale in (
                (f"KP{i}", "KP_3H", "1", 0.1),   # CSV 亦為 Kp×10
                (f"AP{i}", "AP_3H", "nT", 1.0),
            ):
                if col not in df.columns:
                    continue
                sub = df[["date", col]].dropna()
                recs.append(
                    pd.DataFrame(
                        {
                            "valid_time": sub["date"] + offset,
                            "param_code": code,
                            "value": pd.to_numeric(sub[col], errors="coerce") * scale,
                            "unit": unit,
                            "data_type": data_type.loc[sub.index],
                        }
                    )
                )

        if not recs:
            from swx_core import empty_frame

            return empty_frame()

        out = pd.concat(recs, ignore_index=True)
        out["source_id"] = self.spec.source_id
        out["source_tier"] = self.spec.tier
        return normalize(out)
