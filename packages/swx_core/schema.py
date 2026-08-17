"""swx_core.schema — swx_observation 標準資料契約（架構書 §6.2）。

本模組是**唯一**定義觀測列格式的地方。所有 connector 產出、所有查詢回傳，
一律使用 OBS_COLUMNS 的欄位與型別。

雙時間軸（bitemporal）是本 schema 的核心：
  valid_time  物理有效時間 —— 這筆值描述的是哪一刻的太空環境
  ingest_time 入庫時間     —— 系統是在哪一刻才知道這件事

兩者分開，回放（as_of 查詢）才能只看「當時已知」的資料，
預報驗證才不會拿事後訂正值當即時值用（架構書 P3／P6）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd
import pyarrow as pa

# ── 品質旗標（沿用 Sat_TraingDataExtension/data_quality_audit.py 之三級制）──
QUALITY_GOOD = "good"
QUALITY_SUSPECT = "suspect"
QUALITY_REJECTED = "rejected"
QUALITY_FLAGS = (QUALITY_GOOD, QUALITY_SUSPECT, QUALITY_REJECTED)

# ── 資料型別（沿用 CelesTrak 語彙，觀測與預測在資料層就分得清楚）──────────
DATA_TYPE_OBS = "OBS"   # 觀測值
DATA_TYPE_INT = "INT"   # 內插值
DATA_TYPE_PRD = "PRD"   # 日預測
DATA_TYPE_PRM = "PRM"   # 月預測
DATA_TYPE_FCS = "FCS"   # 本系統產生的預報（與來源預測區分）
OBSERVED_TYPES = (DATA_TYPE_OBS, DATA_TYPE_INT)

OBS_COLUMNS: dict[str, str] = {
    "valid_time": "datetime64[ns, UTC]",
    "ingest_time": "datetime64[ns, UTC]",
    "param_code": "string",
    "value": "float64",
    "unit": "string",
    "source_id": "string",
    "source_tier": "int16",
    "quality_flag": "string",
    "quality_reason": "string",
    "confidence": "float32",
    "lat": "float64",
    "lon": "float64",
    "grid_id": "string",
    "revision": "int32",
    "data_type": "string",
}

ARROW_SCHEMA = pa.schema(
    [
        ("valid_time", pa.timestamp("us", tz="UTC")),
        ("ingest_time", pa.timestamp("us", tz="UTC")),
        ("param_code", pa.string()),
        ("value", pa.float64()),
        ("unit", pa.string()),
        ("source_id", pa.string()),
        ("source_tier", pa.int16()),
        ("quality_flag", pa.string()),
        ("quality_reason", pa.string()),
        ("confidence", pa.float32()),
        ("lat", pa.float64()),
        ("lon", pa.float64()),
        ("grid_id", pa.string()),
        ("revision", pa.int32()),
        ("data_type", pa.string()),
    ]
)

# DuckDB 端的等價 DDL（供需要實體表而非 Parquet view 的情境使用）
OBSERVATION_DDL = """
CREATE TABLE IF NOT EXISTS swx_observation (
  valid_time     TIMESTAMPTZ NOT NULL,
  ingest_time    TIMESTAMPTZ NOT NULL,
  param_code     VARCHAR     NOT NULL,
  value          DOUBLE,
  unit           VARCHAR     NOT NULL,
  source_id      VARCHAR     NOT NULL,
  source_tier    SMALLINT    NOT NULL,
  quality_flag   VARCHAR     NOT NULL,
  quality_reason VARCHAR,
  confidence     REAL,
  lat            DOUBLE,
  lon            DOUBLE,
  grid_id        VARCHAR,
  revision       INTEGER     NOT NULL DEFAULT 0,
  data_type      VARCHAR     NOT NULL
);
"""


@dataclass
class Observation:
    """單筆觀測。connector 通常直接產 DataFrame，此類別用於測試與單筆組裝。"""

    valid_time: datetime
    param_code: str
    value: float | None
    unit: str
    source_id: str
    source_tier: int = 1
    quality_flag: str = QUALITY_GOOD
    quality_reason: str | None = None
    confidence: float | None = None
    lat: float | None = None
    lon: float | None = None
    grid_id: str | None = None
    revision: int = 0
    data_type: str = DATA_TYPE_OBS
    ingest_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def empty_frame() -> pd.DataFrame:
    """回傳符合契約的空 DataFrame（欄位與 dtype 皆正確）。"""
    return normalize(pd.DataFrame({c: [] for c in OBS_COLUMNS}))


def normalize(df: pd.DataFrame, *, ingest_time: datetime | None = None) -> pd.DataFrame:
    """把任意 DataFrame 對齊到 OBS_COLUMNS 契約。

    缺欄補預設值、多餘欄位丟棄、型別統一、時間一律轉 UTC。
    connector 只需產出 valid_time / param_code / value / unit 等核心欄位即可。
    """
    out = df.copy()

    if "ingest_time" not in out.columns or out.get("ingest_time") is None:
        out["ingest_time"] = ingest_time or datetime.now(timezone.utc)
    if ingest_time is not None:
        out["ingest_time"] = out["ingest_time"].fillna(ingest_time)

    defaults: dict[str, object] = {
        "source_tier": 1,
        "quality_flag": QUALITY_GOOD,
        "quality_reason": None,
        "confidence": None,
        "lat": None,
        "lon": None,
        "grid_id": None,
        "revision": 0,
        "data_type": DATA_TYPE_OBS,
        "unit": "1",
        "value": None,
    }
    for col, default in defaults.items():
        if col not in out.columns:
            out[col] = default

    missing = {"valid_time", "param_code", "source_id"} - set(out.columns)
    if missing:
        raise ValueError(f"observation frame 缺少必要欄位: {sorted(missing)}")

    out = out[list(OBS_COLUMNS)]

    for col in ("valid_time", "ingest_time"):
        s = pd.to_datetime(out[col], utc=True, errors="coerce")
        out[col] = s
    for col in ("param_code", "unit", "source_id", "quality_flag", "quality_reason",
                "grid_id", "data_type"):
        out[col] = out[col].astype("string")
    out["value"] = pd.to_numeric(out["value"], errors="coerce").astype("float64")
    out["confidence"] = pd.to_numeric(out["confidence"], errors="coerce").astype("float32")
    for col in ("lat", "lon"):
        out[col] = pd.to_numeric(out[col], errors="coerce").astype("float64")
    out["source_tier"] = pd.to_numeric(out["source_tier"], errors="coerce").fillna(1).astype("int16")
    out["revision"] = pd.to_numeric(out["revision"], errors="coerce").fillna(0).astype("int32")

    bad = out.loc[~out["quality_flag"].isin(QUALITY_FLAGS), "quality_flag"].unique()
    if len(bad):
        raise ValueError(f"未知的 quality_flag: {list(bad)}（合法值 {QUALITY_FLAGS}）")

    return out.dropna(subset=["valid_time"]).reset_index(drop=True)
