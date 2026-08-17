"""services.ingest.omni — NASA OMNI2 逐時歷史資料（預報引擎的訓練資料來源）。

為什麼需要這個來源：SWPC 的即時通道（RTSW 磁場／太陽風）只保留近幾天，
Kyoto Dst 即時頁只有當月。若只靠這些，48 小時預報的訓練集連一次完整太陽週期
都湊不齊，太陽風耦合特徵更是幾乎全為空值。

OMNI2 把 IMF、太陽風電漿、Kp、Dst、ap、F10.7、AE 對齊到同一條逐時時間軸，
自 1963 年起，公開下載且免認證。這是把「事後偵測」升級為「可訓練預報」的關鍵。

欄位位置已對 2024 年檔案驗證：
  Kp×10 = 第 39 欄（Gannon 事件達 90，即 Kp 9）
  Dst   = 第 41 欄（Gannon 事件最低 −406 nT，與公布值相符）
  ap    = 第 50 欄、F10.7 = 第 51 欄、V = 第 25 欄、N = 第 24 欄、Bz(GSM) = 第 17 欄

注意：OMNI 是**事後重整**的資料集，發布延遲數週至數月。回填時 publication_lag_s
必須反映這一點，否則 as_of 回放會把還沒發布的資料當成即時可用（前視偏差）。
"""

from __future__ import annotations

import pandas as pd

from swx_core import empty_frame, normalize

from .base import Connector

# (欄索引 0-based, param_code, unit, 缺值碼, 換算)
OMNI_FIELDS: list[tuple[int, str, str, float, float]] = [
    (16, "IMF_BZ", "nT", 999.9, 1.0),
    (23, "SW_N", "cm^-3", 999.9, 1.0),
    (24, "SW_V", "km/s", 9999.0, 1.0),
    (38, "KP_3H", "1", 99.0, 0.1),      # 檔內為 Kp×10
    (40, "DST", "nT", 99999.0, 1.0),
    (49, "AP_3H", "nT", 999.0, 1.0),
    (50, "F107_OBS", "sfu", 999.9, 1.0),
]


class OmniConnector(Connector):
    """OMNI2 逐時檔（每年一檔）。

    endpoint 中的 `{year}` 會被替換；`--year` 由 sources.yaml 的 years 欄或
    呼叫端指定，預設抓當年與前一年。
    """

    formats = ("omni2_hourly",)
    raw_ext = "dat"

    def __init__(self, *args, year: int | None = None, **kw) -> None:
        super().__init__(*args, **kw)
        self.year = year

    def fetch_bytes(self) -> tuple[bytes, str]:
        if self.year is not None and self.spec.endpoint:
            original = self.spec.endpoint
            object.__setattr__(self.spec, "endpoint", original.format(year=self.year))
            try:
                return super().fetch_bytes()
            finally:
                object.__setattr__(self.spec, "endpoint", original)
        return super().fetch_bytes()

    def parse(self, payload: bytes) -> pd.DataFrame:
        rows = [line.split() for line in payload.decode("ascii", errors="replace").splitlines()]
        rows = [r for r in rows if len(r) >= 51]
        if not rows:
            return empty_frame()

        raw = pd.DataFrame(rows).apply(pd.to_numeric, errors="coerce")
        valid_time = pd.to_datetime(
            raw[0].astype("Int64").astype(str) + raw[1].astype("Int64").astype(str).str.zfill(3),
            format="%Y%j",
            utc=True,
            errors="coerce",
        ) + pd.to_timedelta(raw[2].fillna(0).astype(int), unit="h")

        frames = []
        for idx, code, unit, fill, scale in OMNI_FIELDS:
            if idx >= raw.shape[1]:
                continue
            values = raw[idx].where(raw[idx] < fill)      # 缺值碼一律轉 NaN
            piece = pd.DataFrame(
                {"valid_time": valid_time, "param_code": code,
                 "value": values * scale, "unit": unit}
            ).dropna(subset=["valid_time", "value"])
            # OMNI 把低頻指數複製到每個小時，直接入庫會灌水並在下游造成
            # 「同一天有 24 個值、最後一筆勝出」的覆蓋問題，只保留原生格點：
            #   Kp/ap 為 3 小時指數 → 取 0,3,6…時
            #   F10.7 為日指數      → 只取 00:00
            if code in ("KP_3H", "AP_3H"):
                piece = piece[piece["valid_time"].dt.hour % 3 == 0]
            elif code == "F107_OBS":
                piece = piece[piece["valid_time"].dt.hour == 0]
            frames.append(piece)

        if not frames:
            return empty_frame()
        df = pd.concat(frames, ignore_index=True)
        df["source_id"] = self.spec.source_id
        df["source_tier"] = self.spec.tier
        df["data_type"] = "OBS"
        return normalize(df)
