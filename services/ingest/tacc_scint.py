"""services.ingest.tacc_scint — 福衛七號掩星閃爍指數（TACC `scn1c2`）。

**這是唯一能讓 `GNSS-L3-SCINT` 規則脫離 `unavailable` 的連續資料源。**
在此之前 `GNSS_PNT` 網域的實測判據（S4／ROTI）完全沒有來源。

資料來自 TGRS 接收機**星上**即時運算的 S4 振幅閃爍指數，10 秒節奏，
對每顆追蹤到的 GNSS 衛星計算。一個 netCDF 檔＝一次掩星事件 × 一顆 GNSS 衛星。

## 為什麼取 daily_tar 而非逐檔

單日約 9,000–10,600 個檔案。逐檔抓等於一天打對方站台一萬次；
`daily_tar/{YYYY.DDD}/scn1c2_trops.{YYYY.DDD}.tar.gz` 是**單一 72 MB 下載**。
代價是不能只取臺灣周邊——整包拿下來後在本地篩。

**因此本來源排除於背景自動更新之外**（見 `refresh.EXCLUDE_FROM_REFRESH`），
與 `gfz_hp30`、`omni2_hourly` 同樣改由手動或排程主機處理。
把 72 MB 放進頁面載入路徑會讓儀表板無法使用。

## 掩星幾何不是地面測站

S4 取自沿**臨邊射線**在切點附近的量測，不是某個測站正上方的垂直觀測。
CWA SWOO 的 S4 來自逾 100 個地面站，**那是另一種量**：取樣體積、
時間解析度與代表性都不同。兩者以不同 `source_id` 入庫，
**不可在同一條規則中混用門檻**——掩星的 S4≥0.6 與地面站的 S4≥0.6
不是同一件事。

## RFI 汙染會產生假的 S4

Release Memo 5 明載：地面射頻干擾會扭曲 TGRS 的訊雜比而**產生虛假的 S4**。
memo 說團隊提供 RFI 指數**與**一個更可靠的 RFI 品質旗標，
但**實測本產品的 netCDF 只有 `RFI` 指數變數，沒有那個旗標**。

memo 同時警告「RFI 指數並非總是與資料損壞明確相關」——
所以對指數自訂門檻正是它提醒不要做的事。本模組的處理方式：

  · 逐事件記錄 RFI 指數最大值
  · 超過 `RFI_SUSPECT` 者標 `suspect` 而非丟棄（與突波處理一致：
    標記而不刪除，原值保留，讓下游自行決定）
  · **`RFI_SUSPECT` 是未經校準的保守值**，不是 memo 給的門檻

**在取得那個 QC 旗標之前，本來源不應驅動分級規則**——
與 CWA 的 TWDI 同樣處理（`configs/rules/` 暫不引用此 source 的 S4）。

## 時間慣例

檔內 `start_time` 為 GPS 紀元（1980-01-06）起算的秒數。實測**未經閏秒修正
即與檔內 year/month/day/hour/minute/second 屬性完全吻合**（差 0 秒），
故本模組直接以該紀元換算。UTC 與 GPS 之間仍有 ≤18 秒的歧義，
對 15 分鐘分箱無影響，但做事件起始時刻的精確比對時須留意。

另注意 `time` 變數的 units 字串寫「seconds since <起始時刻>」，
但實際值是**絕對 GPS 秒**（約 1.47e9），不是相對偏移。照字面讀會差 46 年。
"""

from __future__ import annotations

import io
import tarfile
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from swx_core import QUALITY_GOOD, QUALITY_SUSPECT, empty_frame, normalize

from .base import Connector

# 臺灣周邊取樣框（地磁緯度約 19°N，位於赤道異常影響範圍）
TW_LAT_MIN, TW_LAT_MAX = 18.0, 28.0
TW_LON_MIN, TW_LON_MAX = 116.0, 126.0

# 區域代表值的時間分箱。與 GNSS-L3-SCINT 的 dwell 0.25h 對齊。
BIN = "15min"

# RFI 指數的可疑門檻。**未經校準**——memo 提供的權威 QC 旗標不在本產品中，
# 而 memo 亦警告指數與損壞的相關性不穩定。設此值只為讓明顯受擾的事件
# 不以 good 入庫，不宜當成物理門檻。
RFI_SUSPECT = 0.01

GPS_EPOCH = datetime(1980, 1, 6, tzinfo=timezone.utc)
FILL = -900.0          # 檔內以 -999 表缺值


def _attr(nc, name: str) -> float:
    v = nc._attributes.get(name)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def _clean(nc, name: str) -> np.ndarray | None:
    """取變數並把缺值碼轉為 NaN。不存在則回 None。"""
    if name not in nc.variables:
        return None
    v = np.asarray(nc.variables[name][:], dtype=float).ravel()
    return np.where(v <= FILL, np.nan, v)


def _wrap_lon(lon):
    return ((lon + 180.0) % 360.0) - 180.0


def extract_records(raw: bytes) -> list[dict]:
    """解析單一 scn1c2 netCDF，回傳逐筆閃爍記錄。

    **兩個任務的檔案結構不同，必須分開處理。** 這一點沒有寫在下載頁上，
    是實測比對才發現的——用其中一種的假設去解析另一種，會**靜默回傳零筆**
    而不報錯（整包近萬個檔案全數略過，看起來就像那天沒有事件）。

      TDPC（`fs7rt_tdpc`）  全域屬性已含 `s4max_L1` 與其發生位置
                            `lat_s4max_L1`／`lon_s4max_L1`／`alt_s4max_L1`，
                            另有逐點的 **`RFI` 指數變數**。
      TROPS（`fs7rt_trops`）無上述屬性，改為逐點陣列 `Lat`／`Lon`／`Alt`／
                            `s4_L1`，且**完全沒有 RFI 變數**——無從篩檢
                            memo 所述的射頻干擾假訊號。

    因此本案預設採 TDPC。TROPS 仍支援，但其記錄的 `rfi` 為 NaN，
    下游無法據以標記可疑，須在來源說明中載明。
    """
    from scipy.io import netcdf_file

    try:
        nc = netcdf_file(io.BytesIO(raw), "r", mmap=False)
    except Exception:
        return []
    try:
        start = _attr(nc, "start_time")
        if not np.isfinite(start):
            return []
        t0 = GPS_EPOCH + timedelta(seconds=start)

        # ── TDPC：屬性已給峰值與其位置 ──
        s4max = _attr(nc, "s4max_L1")
        lat_pk = _attr(nc, "lat_s4max_L1")
        if np.isfinite(s4max) and np.isfinite(lat_pk):
            rfi = np.nan
            series = _clean(nc, "RFI")
            if series is not None and np.isfinite(series).any():
                rfi = float(np.nanmax(series))
            return [{
                "valid_time": pd.Timestamp(t0),
                "s4": s4max,
                "lat": lat_pk,
                "lon": _wrap_lon(_attr(nc, "lon_s4max_L1")),
                "alt_km": _attr(nc, "alt_s4max_L1"),
                "rfi": rfi,
                "layout": "tdpc",
            }]

        # ── TROPS：逐點陣列，無 RFI ──
        lat = _clean(nc, "Lat")
        lon = _clean(nc, "Lon")
        s4 = _clean(nc, "s4_L1")
        if lat is None or lon is None or s4 is None:
            return []
        n = min(len(lat), len(lon), len(s4))
        if n == 0:
            return []

        times = _clean(nc, "time")
        out = []
        for k in range(n):
            if not (np.isfinite(lat[k]) and np.isfinite(lon[k]) and np.isfinite(s4[k])):
                continue
            # time 變數為絕對 GPS 秒（其 units 字串寫「seconds since …」但值不是相對量）
            if times is not None and k < len(times) and np.isfinite(times[k]):
                ts = GPS_EPOCH + timedelta(seconds=float(times[k]))
            else:
                ts = t0
            alt = _clean(nc, "Alt")
            out.append({
                "valid_time": pd.Timestamp(ts),
                "s4": float(s4[k]),
                "lat": float(lat[k]),
                "lon": float(_wrap_lon(lon[k])),
                "alt_km": float(alt[k]) if alt is not None and k < len(alt) else np.nan,
                "rfi": np.nan,           # TROPS 無此欄位
                "layout": "trops",
            })
        return out
    finally:
        nc.close()


def in_taiwan_box(lat: float, lon: float) -> bool:
    return (TW_LAT_MIN <= lat <= TW_LAT_MAX) and (TW_LON_MIN <= lon <= TW_LON_MAX)


def events_to_frame(events: list[dict]) -> pd.DataFrame:
    """把逐事件摘要彙整成區域代表值。

    分箱取 **max** 而非平均：這個參數要回答的是「臺灣上空現在有沒有閃爍」，
    平均會被同時段其他平靜的掩星稀釋掉單一強事件——而強事件正是要偵測的對象。
    """
    if not events:
        return empty_frame()

    df = pd.DataFrame(events).sort_values("valid_time")
    df["bin"] = df["valid_time"].dt.floor(BIN)

    grouped = df.groupby("bin", as_index=False).agg(
        value=("s4", "max"),
        rfi_max=("rfi", "max"),
        n_events=("s4", "size"),
    )
    out = pd.DataFrame({
        "valid_time": grouped["bin"],
        "param_code": "S4",
        "value": grouped["value"].astype(float),
        "unit": "1",
        "data_type": "OBS",
    })
    suspect = grouped["rfi_max"].fillna(0.0) > RFI_SUSPECT
    out["quality_flag"] = np.where(suspect, QUALITY_SUSPECT, QUALITY_GOOD)
    out["quality_reason"] = np.where(
        suspect,
        "rfi_index_above_" + str(RFI_SUSPECT) + "（未校準門檻；權威 QC 旗標不在本產品中）",
        "",
    )
    return out


class TaccScintConnector(Connector):
    """TACC `scn1c2` 每日打包檔。

    `--date` 由呼叫端指定（`YYYY.DDD`）；未指定時取昨日——當日的打包檔
    通常尚未產生，直接抓會 404。
    """

    formats = ("tacc_scn1c2_tar",)
    raw_ext = "tar.gz"

    def __init__(self, *args, date: str | None = None, **kw) -> None:
        super().__init__(*args, **kw)
        self.date = date                 # None = 自動往前找可用的日期
        self.resolved_date: str | None = None

    @staticmethod
    def default_date(now: datetime | None = None) -> str:
        d = (now or datetime.now(timezone.utc)) - timedelta(days=1)
        return f"{d.year}.{d.timetuple().tm_yday:03d}"

    def candidate_dates(self) -> list[str]:
        """要嘗試的日期，由新到舊。

        每日打包檔的產出時間不固定——寫死「昨天」會在對方延遲時週期性失敗，
        而那種失敗看起來就像來源掛掉。往前試幾天可吸收正常的產製延遲。
        """
        if self.date is not None:
            return [self.date]
        days = int(self.spec.raw.get("lookback_days", 4))
        now = datetime.now(timezone.utc)
        return [
            f"{d.year}.{d.timetuple().tm_yday:03d}"
            for d in (now - timedelta(days=k) for k in range(1, days + 1))
        ]

    def fetch_bytes(self) -> tuple[bytes, str]:
        if not self.spec.endpoint:
            return super().fetch_bytes()

        original = self.spec.endpoint
        errors: list[str] = []
        for date in self.candidate_dates():
            object.__setattr__(self.spec, "endpoint", original.format(date=date))
            try:
                payload, mode = super().fetch_bytes()
                self.resolved_date = date
                return payload, mode
            except Exception as exc:      # noqa: BLE001 — 逐日嘗試，全失敗才拋
                errors.append(f"{date}: {type(exc).__name__}")
            finally:
                object.__setattr__(self.spec, "endpoint", original)
        raise RuntimeError(
            f"{self.spec.source_id} 於最近 {len(self.candidate_dates())} 天皆無可用打包檔："
            + "; ".join(errors)
        )

    def parse(self, payload: bytes) -> pd.DataFrame:
        events: list[dict] = []
        try:
            tar = tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz")
        except tarfile.TarError:
            return empty_frame()

        with tar:
            for member in tar:
                if not member.isfile() or "scn1c2" not in member.name:
                    continue
                handle = tar.extractfile(member)
                if handle is None:
                    continue
                # 先篩地理範圍再收集：整包近萬個檔案，全留在記憶體無必要
                for rec in extract_records(handle.read()):
                    if in_taiwan_box(rec["lat"], rec["lon"]):
                        events.append(rec)

        df = events_to_frame(events)
        if df.empty:
            return df
        df["source_id"] = self.spec.source_id
        df["source_tier"] = self.spec.tier
        return normalize(df)
