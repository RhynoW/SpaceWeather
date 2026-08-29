"""services.ingest.forecast_sources — 預報基準與環電流／電離層通道。

這四個來源都是為了讓 48 小時預報「有得比、有得算」而加入的：

  swpc_geomag_forecast  NOAA 官方 3 日 Kp 預報（逐 3 小時，共 72 小時）。
                        **這是本案預報引擎的對照基準**——若我們的模型贏不過
                        NOAA 的官方預報，那就沒有自建的理由。誠實的做法是把它
                        當基準線同場比較，而不是只跟持續性（persistence）比。

  swpc_27day_outlook    27 日展望（逐日 F10.7 / Ap / 最大 Kp）。太陽自轉週期為
                        27 天，冕洞高速流會週期性復現，這是 24–48 小時以上horizon
                        少數有物理依據的預報訊號。

  swpc_45day_forecast   45 日 Ap 與 F10.7 預報。**軌道預測實際依賴的驅動量**，
                        提前量以週計；本案原本在這個尺度上沒有任何驗證。
                        它是要打敗的作業基線，也是 COMSPOC 等業者合成
                        CSSI 驅動檔時用的同一份來源。

  kyoto_dst             Kyoto WDC 逐時 Dst。地磁暴主相的標準指標，比 3 小時的 Kp
                        有更好的時間解析度，也是熱氣層密度響應的關鍵驅動。

  swpc_drap             D-Region Absorption Prediction 全球最高受影響頻率格網。
                        這是**現成可用的電離層產品**（架構書 C2 原列為需外部協調），
                        可直接支援 HF 吸收判定，並在台灣周邊取樣為區域指標。
"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd

from swx_core import empty_frame, normalize

from .base import Connector

# 台灣周邊取樣範圍（D-RAP 區域指標）
TW_LAT, TW_LON = 23.5, 121.0


class SwpcGeomagForecastConnector(Connector):
    """NOAA 3 日地磁預報（逐 3 小時 Kp）——預報引擎的對照基準。"""

    formats = ("swpc_geomag_forecast_txt",)
    raw_ext = "txt"

    _DATE_RE = re.compile(r"NOAA Kp index (?:breakdown|forecast)\s+(.+)", re.IGNORECASE)
    _ROW_RE = re.compile(r"^(\d{2})-(\d{2})UT\s+(.+)$")

    def parse(self, payload: bytes) -> pd.DataFrame:
        text = payload.decode("utf-8", errors="replace")
        lines = text.splitlines()

        # 找出欄位對應的三個日期（標頭形如 "Aug 17    Aug 18    Aug 19"）
        dates: list[pd.Timestamp] = []
        issued = None
        for line in lines:
            if line.startswith(":Issued:"):
                issued = pd.to_datetime(line.split(":Issued:")[1].strip(),
                                        format="%Y %b %d %H%M UTC", utc=True, errors="coerce")
            m = self._DATE_RE.search(line)
            if m:
                span = m.group(1)
                year = (issued or pd.Timestamp.now(tz="UTC")).year
                for token in re.findall(r"([A-Z][a-z]{2})\s+(\d{1,2})", span):
                    pass  # 期間字串只給起訖，實際日期取自下一行的欄標頭
            if re.match(r"^\s+[A-Z][a-z]{2}\s+\d{1,2}\s+[A-Z][a-z]{2}\s+\d{1,2}", line):
                year = (issued or pd.Timestamp.now(tz="UTC")).year
                for mon, day in re.findall(r"([A-Z][a-z]{2})\s+(\d{1,2})", line):
                    ts = pd.to_datetime(f"{year} {mon} {day}", format="%Y %b %d",
                                        utc=True, errors="coerce")
                    if pd.notna(ts):
                        dates.append(ts)
                if dates:
                    break

        if not dates:
            return empty_frame()

        recs: list[dict] = []
        for line in lines:
            m = self._ROW_RE.match(line.strip())
            if not m:
                continue
            hour = int(m.group(1))
            values = [v for v in m.group(3).split() if re.match(r"^[\d.]+", v)]
            for i, raw in enumerate(values[: len(dates)]):
                try:
                    kp = float(re.sub(r"[^\d.]", "", raw))
                except ValueError:
                    continue
                recs.append(
                    {
                        "valid_time": dates[i] + pd.Timedelta(hours=hour),
                        "param_code": "KP_3H",
                        "value": kp,
                        "unit": "1",
                        "data_type": "PRD",       # 來源預報，非觀測
                    }
                )

        if not recs:
            return empty_frame()
        df = pd.DataFrame(recs)
        df["source_id"] = self.spec.source_id
        df["source_tier"] = self.spec.tier
        return normalize(df)


class Swpc27DayOutlookConnector(Connector):
    """27 日展望（逐日 F10.7 / Ap / 最大 Kp）。"""

    formats = ("swpc_27day_txt",)
    raw_ext = "txt"

    _ROW_RE = re.compile(
        r"^(\d{4})\s+([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d+)\s+(\d+)\s+(\d+)\s*$"
    )

    def parse(self, payload: bytes) -> pd.DataFrame:
        recs: list[dict] = []
        for line in payload.decode("utf-8", errors="replace").splitlines():
            m = self._ROW_RE.match(line.strip())
            if not m:
                continue
            year, mon, day, f107, ap, kp = m.groups()
            ts = pd.to_datetime(f"{year} {mon} {day}", format="%Y %b %d",
                                utc=True, errors="coerce")
            if pd.isna(ts):
                continue
            recs.extend(
                [
                    {"valid_time": ts, "param_code": "F107_OBS", "value": float(f107),
                     "unit": "sfu", "data_type": "PRD"},
                    {"valid_time": ts, "param_code": "AP_AVG", "value": float(ap),
                     "unit": "nT", "data_type": "PRD"},
                    {"valid_time": ts, "param_code": "KP_MAX_DAILY", "value": float(kp),
                     "unit": "1", "data_type": "PRD"},
                ]
            )
        if not recs:
            return empty_frame()
        df = pd.DataFrame(recs)
        df["source_id"] = self.spec.source_id
        df["source_tier"] = self.spec.tier
        return normalize(df)


class Swpc45DayForecastConnector(Connector):
    """45 日 Ap 與 F10.7 預報——**軌道預測實際依賴的驅動量**。

    為什麼要它：本案的預報擂台原本只驗 Kp（3–48 小時）與 Hp30（1–6 小時），
    但軌道預測用的驅動量是**逐日的 F10.7 與 Ap**，提前量以週、月計。
    那個尺度上我們一條驗證都沒有，議題一的 KPI 等於靠別人的預測值撐著。
    這支是「要打敗的作業基線」：SWPC 自己的 45 天預報。

    **沒有回填管道**（與 e-GNSS I95 同型的限制）：SWPC 只發布當前一份，
    歷史版本不可取得。因此 SWPC 預報的實測成績只能自開始輪詢之日起累積，
    靠資料層的 bitemporal（`ingest_time` + `as_of` 回放）還原「當時報了什麼」。
    我們自己的模型則可用歷史觀測回測——兩者**不在同一個起跑線上**，
    比較時必須講清楚，不可把「我們有回測、它沒有」說成「我們比較準」。

    檔案裡的 `FORECASTER: AUTOMATED` 值得一記：這份 45 天預報是自動產生的，
    不是預報員逐日判斷的產物。
    """

    formats = ("swpc_45day_txt",)
    raw_ext = "txt"

    #: 每列數個 `DDMonYY VVV` 對，例如 `28Aug26 034`
    _PAIR_RE = re.compile(r"(\d{2}[A-Z][a-z]{2}\d{2})\s+(\d{1,4})")
    _SECTIONS = (
        ("45-DAY AP FORECAST", "AP_AVG", "nT"),
        ("45-DAY F10.7 CM FLUX FORECAST", "F107_OBS", "sfu"),
    )

    def parse(self, payload: bytes) -> pd.DataFrame:
        text = payload.decode("utf-8", errors="replace")
        lines = text.splitlines()

        starts = {}
        for i, line in enumerate(lines):
            for header, _code, _unit in self._SECTIONS:
                if line.strip().startswith(header):
                    starts[header] = i

        recs: list[dict] = []
        for header, code, unit in self._SECTIONS:
            i = starts.get(header)
            if i is None:
                # 少一個區塊就少半份預報。回空表會讓另一半也不見，
                # 所以只警告、繼續解析另一個區塊。
                self.warn(f"{header} 區塊不存在（版面可能已改版）")
                continue
            for line in lines[i + 1:]:
                if not self._PAIR_RE.search(line):
                    break                      # 區塊結束
                for day, value in self._PAIR_RE.findall(line):
                    ts = pd.to_datetime(day, format="%d%b%y", utc=True, errors="coerce")
                    if pd.isna(ts):
                        continue
                    recs.append({"valid_time": ts, "param_code": code,
                                 "value": float(value), "unit": unit,
                                 # PRD：來源自帶的預測，與本系統自產的 FCS 分開。
                                 # 混在一起就分不出「誰預報的」，擂台也就沒得比。
                                 "data_type": "PRD"})

        if not recs:
            return empty_frame()
        df = pd.DataFrame(recs)
        df["source_id"] = self.spec.source_id
        df["source_tier"] = self.spec.tier
        return normalize(df)


class KyotoDstConnector(Connector):
    """Kyoto WDC 逐時 Dst（即時值）。

    頁面為 euc-jp 編碼的 HTML，資料在 <pre> 區塊，每列一天、24 個小時值。
    """

    formats = ("kyoto_dst_html",)
    raw_ext = "html"

    _MONTH_RE = re.compile(r"([A-Z]+)\s+(\d{4})")

    def parse(self, payload: bytes) -> pd.DataFrame:
        text = payload.decode("euc-jp", errors="replace")
        block = text[text.find("<pre") : text.find("</pre>")]
        if not block:
            return empty_frame()

        m = self._MONTH_RE.search(block)
        if not m:
            return empty_frame()
        month = pd.to_datetime(f"{m.group(1)} {m.group(2)}", format="%B %Y",
                               utc=True, errors="coerce")
        if pd.isna(month):
            return empty_frame()

        recs: list[dict] = []
        for line in block.splitlines():
            parts = line.split()
            if len(parts) < 25 or not parts[0].isdigit():
                continue
            day = int(parts[0])
            if not 1 <= day <= 31:
                continue
            try:
                values = [float(v) for v in parts[1:25]]
            except ValueError:
                continue
            base = month + pd.Timedelta(days=day - 1)
            for hour, val in enumerate(values):
                if val == 9999:            # 缺值碼
                    continue
                recs.append(
                    {
                        "valid_time": base + pd.Timedelta(hours=hour),
                        "param_code": "DST",
                        "value": val,
                        "unit": "nT",
                        "data_type": "OBS",
                    }
                )
        if not recs:
            return empty_frame()
        df = pd.DataFrame(recs)
        df["source_id"] = self.spec.source_id
        df["source_tier"] = self.spec.tier
        return normalize(df)


class SwpcDrapConnector(Connector):
    """D-Region Absorption Prediction 全球格網 → 全球與台灣區域指標。

    格網值為「受吸收影響的最高頻率（MHz）」：值越高代表吸收越強、
    越多 HF 頻段不可用。平靜時全球約 1–2 MHz，大型閃焰時日照側可達 20–30 MHz。

    存兩個參數：
      DRAP_MAX_MHZ  全球最大值（事件強度）
      DRAP_TW_MHZ   台灣周邊格點值（區域影響——這才是任務要看的）
    """

    formats = ("swpc_drap_txt",)
    raw_ext = "txt"

    def parse(self, payload: bytes) -> pd.DataFrame:
        text = payload.decode("utf-8", errors="replace")
        lines = text.splitlines()

        valid_time = None
        lons: list[float] | None = None
        rows: list[tuple[float, list[float]]] = []

        for line in lines:
            if "Product Valid At" in line:
                valid_time = pd.to_datetime(line.split(":", 1)[1].strip(),
                                            utc=True, errors="coerce")
            if line.strip().startswith("-17") and lons is None:
                lons = [float(v) for v in line.split()]
                continue
            if "|" in line and lons is not None:
                left, right = line.split("|", 1)
                try:
                    lat = float(left.strip())
                except ValueError:
                    continue
                vals = []
                for v in right.split():
                    try:
                        vals.append(float(v))
                    except ValueError:
                        vals.append(np.nan)
                if len(vals) == len(lons):
                    rows.append((lat, vals))

        if valid_time is None or not rows or lons is None:
            return empty_frame()

        grid = np.array([v for _lat, v in rows], dtype=float)
        lats = np.array([lat for lat, _v in rows], dtype=float)

        # 台灣周邊：取最近格點
        i = int(np.nanargmin(np.abs(lats - TW_LAT)))
        j = int(np.nanargmin(np.abs(np.array(lons) - TW_LON)))

        recs = [
            {"valid_time": valid_time, "param_code": "DRAP_MAX_MHZ",
             "value": float(np.nanmax(grid)), "unit": "MHz", "data_type": "OBS"},
            {"valid_time": valid_time, "param_code": "DRAP_TW_MHZ",
             "value": float(grid[i, j]), "unit": "MHz", "data_type": "OBS",
             "lat": float(lats[i]), "lon": float(lons[j])},
        ]
        df = pd.DataFrame(recs)
        df["source_id"] = self.spec.source_id
        df["source_tier"] = self.spec.tier
        return normalize(df)
