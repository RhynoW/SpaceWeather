"""swx_core.store — 雙時間軸資料層（架構書 §6.1、§6.2）。

儲存分工（P9 讀寫分離）：
  · 擷取端只寫 **Parquet 分區**  data/swx_parquet/{param_code}/{YYYY|YYYY-MM}/*.parquet
    每個 connector 寫自己的檔，彼此不爭鎖，也不會卡住服務端查詢。
  · 服務端以 **DuckDB 唯讀** 掛 view 查詢，必要時 ATTACH 既有 space_db.duckdb 取軌道資料。

為何不直接寫 DuckDB：DuckDB 是單寫入者模型，擷取程序一旦持寫鎖，
API 與儀表板就全部卡住。Parquet 分區 + 唯讀查詢是最省事的解法。

寫入為 **append-only**：同一 (param, valid_time) 被重新擷取時不覆蓋舊列，
而是以新的 ingest_time 追加，如此 as_of 回放才能重現「當時已知」的狀態。
為避免每日重抓整份檔案造成無限膨脹，write() 預設做變更偵測（dedupe），
只寫入與既有最新值不同的列。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .config import data_dir, project_root
from .params import registry
from .schema import (
    ARROW_SCHEMA,
    OBSERVED_TYPES,
    QUALITY_GOOD,
    QUALITY_REJECTED,
    QUALITY_SUSPECT,
    empty_frame,
    normalize,
)

_VALUE_EPS = 1e-12


@dataclass
class WriteResult:
    source_id: str
    rows_in: int
    rows_written: int
    rows_skipped: int
    files: list[Path]

    def __str__(self) -> str:
        return (
            f"[{self.source_id}] 寫入 {self.rows_written}/{self.rows_in} 列"
            f"（略過未變更 {self.rows_skipped}），{len(self.files)} 檔"
        )


class SwxStore:
    """太空天氣觀測資料層。"""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else data_dir()
        self.parquet_root = self.root / "swx_parquet"
        self.raw_root = self.root / "raw"
        self.db_path = self.root / "swx_db.duckdb"
        self.parquet_root.mkdir(parents=True, exist_ok=True)
        self.raw_root.mkdir(parents=True, exist_ok=True)

    # ── 路徑 ────────────────────────────────────────────────────────────
    @staticmethod
    def partition_format(param_code: str) -> str:
        """依參數更新頻率決定分區粒度。

        日／3 小時級參數若按月分區會產生大量幾十列的碎檔（21 年 × 12 月 × N 參數），
        分鐘級參數若按年分區則單檔過大。故以 cadence 為準：
          cadence >= 1h  → 年分區（F10.7、Kp、Ap、Dst…）
          cadence <  1h  → 月分區（IMF、太陽風、X-ray、S4…）
        """
        spec = registry().get(param_code)
        cadence = spec.cadence_s if spec and spec.cadence_s else 3600
        return "%Y" if cadence >= 3600 else "%Y-%m"

    def _partition_dir(self, param_code: str, key: str) -> Path:
        return self.parquet_root / str(param_code) / key

    def globs(self, params: list[str] | None = None) -> list[str]:
        """回傳可餵給 read_parquet 的 glob 清單（只含實際存在的分區）。"""
        codes = params or [p.name for p in self.parquet_root.iterdir() if p.is_dir()]
        out: list[str] = []
        for code in codes:
            d = self.parquet_root / code
            if d.is_dir() and any(d.rglob("*.parquet")):
                out.append(str(d / "*" / "*.parquet").replace("\\", "/"))
        return out

    def raw_path(self, source_id: str, ts: datetime, ext: str) -> Path:
        """原始落地路徑（先原封存檔再解析，解析錯誤可重跑不需重抓）。"""
        p = (
            self.raw_root
            / source_id
            / f"{ts:%Y}"
            / f"{ts:%m}"
            / f"{ts:%d}"
            / f"{ts:%Y%m%dT%H%M%SZ}.{ext.lstrip('.')}"
        )
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    # ── 寫入 ────────────────────────────────────────────────────────────
    def write(
        self,
        df: pd.DataFrame,
        *,
        source_id: str | None = None,
        ingest_time: datetime | None = None,
        dedupe: bool = True,
    ) -> WriteResult:
        obs = normalize(df, ingest_time=ingest_time)
        if ingest_time is not None:
            # 呼叫端明確指定本批次的入庫時間時必須覆寫。connector 產生的 frame
            # 早已被 normalize 填過 now()，若不覆寫，整個 as_of 回放能力就失效
            # ——而且是靜默失效，查詢只會回空表。
            obs["ingest_time"] = pd.Timestamp(ingest_time).tz_convert("UTC")                 if pd.Timestamp(ingest_time).tzinfo else pd.Timestamp(ingest_time, tz="UTC")
        rows_in = len(obs)
        sid = source_id or (str(obs["source_id"].iloc[0]) if rows_in else "unknown")
        if rows_in == 0:
            return WriteResult(sid, 0, 0, 0, [])

        stamp = (ingest_time or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S%fZ")

        if dedupe:
            obs = self._drop_unchanged(obs)
        skipped = rows_in - len(obs)
        if obs.empty:
            return WriteResult(sid, rows_in, 0, skipped, [])

        files: list[Path] = []
        for code, by_param in obs.groupby("param_code", sort=False):
            fmt = self.partition_format(str(code))
            keys = by_param["valid_time"].dt.strftime(fmt)
            for key, grp in by_param.groupby(keys, sort=False):
                out_dir = self._partition_dir(str(code), str(key))
                out_dir.mkdir(parents=True, exist_ok=True)
                path = out_dir / f"{sid}-{stamp}.parquet"
                table = pa.Table.from_pandas(grp, schema=ARROW_SCHEMA, preserve_index=False)
                pq.write_table(table, path, compression="zstd")
                files.append(path)

        return WriteResult(sid, rows_in, len(obs), skipped, files)

    def _drop_unchanged(self, obs: pd.DataFrame) -> pd.DataFrame:
        """變更偵測：與既有最新值相同者不重複寫入。"""
        codes = sorted(set(obs["param_code"].dropna().astype(str)))
        globs = self.globs(codes)
        if not globs:
            return obs

        lo = obs["valid_time"].min().to_pydatetime()
        hi = obs["valid_time"].max().to_pydatetime()
        sql = f"""
            SELECT DISTINCT ON (param_code, source_id, valid_time, revision)
                   param_code, source_id, valid_time, revision, value, quality_flag
            FROM read_parquet({globs!r}, union_by_name=true)
            WHERE valid_time BETWEEN ? AND ?
            ORDER BY param_code, source_id, valid_time, revision, ingest_time DESC
        """
        with self.connect() as con:
            existing = con.execute(sql, [lo, hi]).fetchdf()
        if existing.empty:
            return obs

        existing["valid_time"] = pd.to_datetime(existing["valid_time"], utc=True)
        merged = obs.merge(
            existing.rename(columns={"value": "_old_value", "quality_flag": "_old_flag"}),
            on=["param_code", "source_id", "valid_time", "revision"],
            how="left",
        )
        same_value = (
            (merged["value"] - merged["_old_value"]).abs() <= _VALUE_EPS
        ) | (merged["value"].isna() & merged["_old_value"].isna())
        same_flag = merged["quality_flag"] == merged["_old_flag"]
        unchanged = (same_value & same_flag).fillna(False).to_numpy()
        return obs.loc[~unchanged].reset_index(drop=True)

    # ── 讀取 ────────────────────────────────────────────────────────────
    def connect(self, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
        """取得 DuckDB 連線。預設用記憶體連線（只查 Parquet，不需持久檔）。"""
        con = duckdb.connect(":memory:")
        con.execute("SET TimeZone='UTC'")
        return con

    def attach_orbit_db(
        self, con: duckdb.DuckDBPyConnection, path: str | Path | None = None, alias: str = "orbit"
    ) -> bool:
        """掛載既有 Sat_TraingDataExtension 的 space_db.duckdb（唯讀）。

        供 model_thermo／事件卡的 ORBIT_PREDICTION 分項取用 TLE 全庫。
        路徑可用環境變數 SWX_ORBIT_DB 指定；找不到時回傳 False 而非拋錯（P5 降級）。
        """
        p = Path(path or os.getenv("SWX_ORBIT_DB", "")) if (path or os.getenv("SWX_ORBIT_DB")) else None
        if p is None or not p.exists():
            return False
        con.execute(f"ATTACH '{p.as_posix()}' AS {alias} (READ_ONLY)")
        return True

    def query(
        self,
        params: str | list[str] | None = None,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        as_of: datetime | None = None,
        observed_only: bool = False,
        include_rejected: bool = False,
        source_id: str | None = None,
    ) -> pd.DataFrame:
        """雙時間軸查詢。

        as_of 為回放的關鍵：只取 ingest_time <= as_of 的列，
        亦即「在那個時刻，系統所能知道的內容」，避免前視偏差。
        同一 (param, valid_time) 有多來源／多版本時，取 tier 最小、ingest 最新者。
        """
        codes = [params] if isinstance(params, str) else params
        globs = self.globs(codes)
        if not globs:
            return empty_frame()

        where = ["1=1"]
        args: list[object] = []
        if codes:
            where.append(f"param_code IN ({','.join(['?'] * len(codes))})")
            args.extend(codes)
        if start is not None:
            where.append("valid_time >= ?")
            args.append(start)
        if end is not None:
            where.append("valid_time <= ?")
            args.append(end)
        if as_of is not None:
            where.append("ingest_time <= ?")
            args.append(as_of)
        if source_id is not None:
            where.append("source_id = ?")
            args.append(source_id)
        if observed_only:
            where.append(f"data_type IN ({','.join(['?'] * len(OBSERVED_TYPES))})")
            args.extend(OBSERVED_TYPES)
        if not include_rejected:
            where.append("quality_flag <> ?")
            args.append(QUALITY_REJECTED)

        sql = f"""
            SELECT DISTINCT ON (param_code, valid_time) *
            FROM read_parquet({globs!r}, union_by_name=true)
            WHERE {' AND '.join(where)}
            ORDER BY param_code, valid_time, source_tier, ingest_time DESC, revision DESC
        """
        with self.connect() as con:
            df = con.execute(sql, args).fetchdf()
        if df.empty:
            return empty_frame()
        return normalize(df).sort_values(["param_code", "valid_time"]).reset_index(drop=True)

    def series(self, param: str, **kw) -> pd.Series:
        """單一參數的時間索引序列（供模型與規則引擎使用）。"""
        df = self.query(param, **kw)
        if df.empty:
            return pd.Series(dtype="float64", index=pd.DatetimeIndex([], tz="UTC"))
        return df.set_index("valid_time")["value"].astype("float64")

    def latest(self, param: str, *, as_of: datetime | None = None) -> pd.Series | None:
        df = self.query(param, as_of=as_of, observed_only=True)
        return None if df.empty else df.iloc[-1]

    def available_params(self) -> list[str]:
        if not self.parquet_root.is_dir():
            return []
        return sorted(
            p.name for p in self.parquet_root.iterdir()
            if p.is_dir() and any(p.rglob("*.parquet"))
        )

    # ── 資料健康（架構書 §11「資料健康」頁）────────────────────────────
    def health(self, *, now: datetime | None = None) -> pd.DataFrame:
        """各來源×參數之最新資料時間、齡期與是否逾越 latency_budget。"""
        from .config import catalog

        now = now or datetime.now(timezone.utc)
        globs = self.globs()
        if not globs:
            return pd.DataFrame(
                columns=["source_id", "param_code", "latest_valid_time", "age_s",
                         "latency_budget_s", "degraded", "n_rows", "good_rate"]
            )
        sql = f"""
            SELECT source_id, param_code,
                   max(valid_time) AS latest_valid_time,
                   count(*)        AS n_rows,
                   avg(CASE WHEN quality_flag = '{QUALITY_GOOD}' THEN 1.0 ELSE 0.0 END) AS good_rate
            FROM read_parquet({globs!r}, union_by_name=true)
            WHERE data_type IN ({','.join([repr(t) for t in OBSERVED_TYPES])})
            GROUP BY 1, 2
            ORDER BY 1, 2
        """
        with self.connect() as con:
            df = con.execute(sql).fetchdf()
        if df.empty:
            return df

        df["latest_valid_time"] = pd.to_datetime(df["latest_valid_time"], utc=True)
        df["age_s"] = (pd.Timestamp(now) - df["latest_valid_time"]).dt.total_seconds()

        cat = catalog()
        budgets = {s.source_id: s.latency_budget_s for s in cat}
        df["latency_budget_s"] = df["source_id"].map(budgets)
        df["degraded"] = (df["age_s"] > df["latency_budget_s"]).fillna(False)
        return df

    # ── 稽核 ────────────────────────────────────────────────────────────
    def storage_summary(self) -> pd.DataFrame:
        rows = []
        for code_dir in sorted(self.parquet_root.glob("*")):
            if not code_dir.is_dir():
                continue
            files = list(code_dir.rglob("*.parquet"))
            if not files:
                continue
            rows.append(
                {
                    "param_code": code_dir.name,
                    "partitions": len({f.parent for f in files}),
                    "files": len(files),
                    "bytes": sum(f.stat().st_size for f in files),
                }
            )
        return pd.DataFrame(rows)
