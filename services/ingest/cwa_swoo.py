"""services.ingest.cwa_swoo — 中央氣象署太空天氣作業辦公室（SWOO）在地觀測。

**這是本案唯一的在地實測來源。** 其餘來源都是國外機構的全球產品；
臺灣上空的電離層與地磁擾動，只有 CWA 有作業級的實測。

取用的兩個量：

  TWTEC  臺灣上空總電子含量（TECU）
         來源為 CWA 地震測報中心維運的地面 GNSS 接收站網（逾 100 站），
         投影至 300 km 高度。這讓 GNSS_PNT 網域從「全部 unavailable」
         變成至少有一個可判定的實測參數。

  TWDI   臺灣地磁擾動指數（nT）
         三分量磁力計的水平總強度減寧靜基線，各站取中位數。
         這是議題二「區域擾動」的正式實測值，可取代
         `geomag.regional_disturbance_proxy()` 的推估（is_proxy=True）。

**授權**：本端點為 SWOO 網站前端自用，非公開發布之 API。本案經成功大學
合作管道向 CWA 取得授權後使用。**未取得授權者不得比照辦理**——
`configs/sources.yaml` 的 notes 欄載明授權依據，移作他用前須重新確認。

**刻意不取的欄位**：SWInfo.json 另含 kidx／DST／sws／swd／IMF／XrayFx／
PtonFx／ssn／sfx 等全球量。這些本案已有權威來源（GFZ、京都、SWPC、CelesTrak），
重複入庫只會製造同一時刻多來源的取捨負擔而無新資訊，故一律不取。

**時間戳**：`issuedate` 格式為民國年 + 本地時間，例如 `115-08-17 16:00 LT`。
必須轉為 UTC（民國年 + 1911，LT = UTC+8），否則會有 8 小時偏移，
且在跨日時把資料放到錯誤的日期分區。

**已知待確認事項**（見 docs/cwa_swoo_analysis.md）：
抓取當下 TWDI = −186 nT，但同一份檔案的 DST = −28、Kp = 3.67（地磁平靜）。
低緯地區的 Sq 日變化可達數十 nT，但 −186 與平靜的全球指數仍不相稱。
在與 CWA 確認 TWDI 的基線定義與時間解析度之前，**本連接器預設不讓 TWDI
直接驅動分級規則**（`configs/rules/` 未引用 DB_TW），避免產生誤警。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pandas as pd

from swx_core import empty_frame, normalize

from .base import Connector

# SWInfo.json 欄位 → (param_code, unit)。只列在地獨有的量，理由見模組說明。
LOCAL_FIELDS: dict[str, tuple[str, str]] = {
    "TWTEC": ("TEC", "TECU"),
    "TWDI": ("DB_TW", "nT"),
}

# 民國年 + 本地時間，例：'115-08-17 16:00 LT'
_ISSUE_RE = re.compile(r"(\d{2,3})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})")
_TW_UTC_OFFSET = timedelta(hours=8)


def parse_issue_time(text: str | None) -> pd.Timestamp | None:
    """把 `115-08-17 16:00 LT` 轉成 UTC 時間戳。

    民國年 + 1911 = 西元年；LT 為 UTC+8。兩者任一沒處理，
    資料就會落在錯誤的時間甚至錯誤的日期分區。
    """
    if not text:
        return None
    m = _ISSUE_RE.search(str(text))
    if not m:
        return None
    roc_y, mo, d, hh, mm = (int(g) for g in m.groups())
    try:
        local = datetime(roc_y + 1911, mo, d, hh, mm)
    except ValueError:
        return None
    return pd.Timestamp(local - _TW_UTC_OFFSET, tz="UTC")


class CwaSwooConnector(Connector):
    """CWA SWOO 前端 JSON（`/json/SWInfo.json`）。"""

    formats = ("cwa_swoo_json",)
    raw_ext = "json"

    def parse(self, payload: bytes) -> pd.DataFrame:
        try:
            data = json.loads(payload.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return empty_frame()
        if not isinstance(data, dict):
            return empty_frame()

        valid_time = parse_issue_time(data.get("issuedate"))
        if valid_time is None:
            # 沒有可信的時間戳就不入庫。用抓取時間頂替會讓回放失真：
            # 那等於宣稱「這個值就是此刻的觀測」，但它可能是數小時前的發布。
            return empty_frame()

        rows = []
        for key, (code, unit) in LOCAL_FIELDS.items():
            value = data.get(key)
            if value is None or not isinstance(value, (int, float)):
                continue
            if float(value) <= -9990.0:      # SWOO 以 -9999 表缺值
                continue
            rows.append({
                "valid_time": valid_time,
                "param_code": code,
                "value": float(value),
                "unit": unit,
            })
        if not rows:
            return empty_frame()

        df = pd.DataFrame(rows)
        df["source_id"] = self.spec.source_id
        df["source_tier"] = self.spec.tier
        df["data_type"] = "OBS"
        return normalize(df)
