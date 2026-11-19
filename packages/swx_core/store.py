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


def _utc(ts) -> pd.Timestamp | None:
    if ts is None:
        return None
    t = pd.Timestamp(ts)
    return t.tz_convert("UTC") if t.tzinfo else t.tz_localize("UTC")


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

    @staticmethod
    def _partition_span(name: str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
        """分區目錄名 → 涵蓋的 [起, 迄)。無法辨識的名稱回傳 None（一律保留，不裁剪）。"""
        try:
            if len(name) == 4 and name.isdigit():
                lo = pd.Timestamp(f"{name}-01-01", tz="UTC")
                return lo, lo + pd.DateOffset(years=1)
            if len(name) == 7 and name[4] == "-":
                lo = pd.Timestamp(f"{name}-01", tz="UTC")
                return lo, lo + pd.DateOffset(months=1)
        except ValueError:
            return None
        return None

    def partition_globs(
        self,
        params: list[str] | None = None,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[str]:
        """只列出與 [start, end] 重疊的分區（依 valid_time 裁剪）。

        分區鍵就是 valid_time 的年或年月（見 partition_format），所以不重疊的分區
        **不可能**含有落在查詢窗內的列——裁剪不改變結果，只少開檔。
        未給 start/end 時等同 globs()。無法辨識的目錄名一律保留。
        """
        if start is None and end is None:
            return self.globs(params)
        lo, hi = _utc(start), _utc(end)
        codes = params or [p.name for p in self.parquet_root.iterdir() if p.is_dir()]
        out: list[str] = []
        for code in codes:
            d = self.parquet_root / code
            if not d.is_dir():
                continue
            for part in sorted(p for p in d.iterdir() if p.is_dir()):
                span = self._partition_span(part.name)
                if span is not None:
                    p_lo, p_hi = span
                    if (lo is not None and p_hi <= lo) or (hi is not None and p_lo > hi):
                        continue
                if any(part.glob("*.parquet")):
                    out.append(str(part / "*.parquet").replace("\\", "/"))
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

        # 未註冊參數不得入庫（架構書 §6.3）。少了這道檢查，拼錯的參數代碼
        # 會安靜地長出一個新分區——寫入回報成功、查詢查不到、UI 少一條線，
        # 沒有任何一處會報錯。曾真的發生：預報端以 f"{code}_STORM_PROB" 拼出
        # KP_3H_STORM_PROB（正確為 KP_STORM_PROB），機率序列因此整批寫錯地方。
        unknown = sorted({str(c) for c in obs["param_code"].unique()
                          if str(c) not in registry()})
        if unknown:
            raise ValueError(
                f"未註冊的參數代碼：{unknown}；"
                "請先在 configs/params.yaml 註冊，或修正拼寫"
            )

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
                # 同來源、同 ingest_time 再寫一次（例如以相同 --ingest-time 重跑）時，
                # 檔名會相同。append-only 不可覆寫：覆寫加上 dedupe 只寫差異列，
                # 會讓前一批未變動的列靜默消失。加序號另存。
                n = 1
                while path.exists():
                    path = out_dir / f"{sid}-{stamp}-{n}.parquet"
                    n += 1
                table = pa.Table.from_pandas(grp, schema=ARROW_SCHEMA, preserve_index=False)
                pq.write_table(table, path, compression="zstd")
                files.append(path)

        return WriteResult(sid, rows_in, len(obs), skipped, files)

    def _drop_unchanged(self, obs: pd.DataFrame) -> pd.DataFrame:
        """變更偵測：與既有最新值相同者不重複寫入。

        比對 value、quality_flag 與 confidence 三者。三個都是交付欄位，
        任一改變都必須落庫——只比 value 的話，品質標記或可信度的修正會被
        當成「沒變」而靜默丟棄。
        """
        codes = sorted(set(obs["param_code"].dropna().astype(str)))
        globs = self.globs(codes)
        if not globs:
            return obs

        lo = obs["valid_time"].min().to_pydatetime()
        hi = obs["valid_time"].max().to_pydatetime()
        sql = f"""
            SELECT DISTINCT ON (param_code, source_id, valid_time, revision)
                   param_code, source_id, valid_time, revision, value, quality_flag,
                   confidence
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
            existing.rename(columns={"value": "_old_value", "quality_flag": "_old_flag",
                                     "confidence": "_old_conf"}),
            on=["param_code", "source_id", "valid_time", "revision"],
            how="left",
        )
        same_value = (
            (merged["value"] - merged["_old_value"]).abs() <= _VALUE_EPS
        ) | (merged["value"].isna() & merged["_old_value"].isna())
        same_flag = merged["quality_flag"] == merged["_old_flag"]
        # confidence 也要比。它是交付欄位（構想書四項 KPI 之一），值沒變但
        # 可信度算法修正時，若不比對就永遠寫不進去——修好了程式，庫裡仍是舊值，
        # 而且沒有任何錯誤訊息。此情況真的發生過（預報 confidence 曾恆為 1.6）。
        same_conf = (
            (merged["confidence"] - merged["_old_conf"]).abs() <= _VALUE_EPS
        ) | (merged["confidence"].isna() & merged["_old_conf"].isna())
        unchanged = (same_value & same_flag & same_conf).fillna(False).to_numpy()
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
        grid_id: str | None = None,
    ) -> pd.DataFrame:
        """雙時間軸查詢。

        as_of 為回放的關鍵：只取 ingest_time <= as_of 的列，
        亦即「在那個時刻，系統所能知道的內容」，避免前視偏差。
        同一 (param, valid_time) 有多來源／多版本時，取 tier 最小、ingest 最新者。

        **去重鍵含 grid_id**：分區資料（如 e-GNSS 三個網的 I95）在同一時刻
        本來就有多個合法值，不含 grid_id 會讓三個網互相蓋掉、只剩一個而不報錯。
        既有的純量參數 grid_id 皆為 NULL，DuckDB 的 DISTINCT ON 視 NULL 相等，
        故對它們是無異動。
        """
        codes = [params] if isinstance(params, str) else params
        globs = self.partition_globs(codes, start=start, end=end)
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
        if grid_id is not None:
            where.append("grid_id = ?")
            args.append(grid_id)
        if observed_only:
            where.append(f"data_type IN ({','.join(['?'] * len(OBSERVED_TYPES))})")
            args.extend(OBSERVED_TYPES)
        if not include_rejected:
            where.append("quality_flag <> ?")
            args.append(QUALITY_REJECTED)

        # **同一時刻觀測優先於預測**，再比 tier 與入庫時間。少了第一個排序鍵，
        # 同 tier 的預報（如 SWPC 3 日 Kp 預報）因入庫較晚而蓋掉觀測值：
        # 實測 2026-11-18 03:00 的 Kp 回傳的是預報 3.67 而非觀測——規則引擎、
        # 現況橫幅都會把預測當成發生過的事。要取預測列者請以 source_id 指定。
        observed_list = ",".join(f"'{t}'" for t in OBSERVED_TYPES)
        sql = f"""
            SELECT DISTINCT ON (param_code, valid_time, grid_id) *
            FROM read_parquet({globs!r}, union_by_name=true)
            WHERE {' AND '.join(where)}
            ORDER BY param_code, valid_time, grid_id,
                     CASE WHEN data_type IN ({observed_list}) THEN 0 ELSE 1 END,
                     source_tier, ingest_time DESC, revision DESC
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
        """最新一筆觀測。由新到舊逐分區找，找到即停。

        分區鍵是 valid_time，所以「含有任何合格觀測的最新分區」必然含有
        全域最新的那個 valid_time；同一 valid_time 的多來源、多版本也都在
        同一分區內，去重結果與全掃相同。以前每次全掃整個參數的歷史。
        """
        d = self.parquet_root / param
        parts = sorted((p for p in d.iterdir() if p.is_dir()), reverse=True) \
            if d.is_dir() else []
        # 名稱無法辨識、或年／月分區混用（cadence 設定曾變更）時，「最新分區」
        # 無法單憑名稱排序決定（2026-08 排在 2026 之前，但 2026 可能含 10 月）——保守全掃。
        if any(self._partition_span(p.name) is None for p in parts) or \
                len({len(p.name) for p in parts}) > 1:
            df = self.query(param, as_of=as_of, observed_only=True)
            return None if df.empty else df.iloc[-1]
        for part in parts:
            lo, hi = self._partition_span(part.name)
            df = self.query(param, start=lo, end=hi - pd.Timedelta(microseconds=1),
                            as_of=as_of, observed_only=True)
            if not df.empty:
                return df.iloc[-1]
        return None

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

    # ── 維運：碎檔合併 ──────────────────────────────────────────────────
    def compact(
        self,
        params: list[str] | None = None,
        *,
        min_files: int = 2,
        dry_run: bool = False,
    ) -> list[dict]:
        """把同一分區的多個小檔合併成一檔。**不刪任何一列**。

        為何需要：每次擷取都在分區內新增一檔（append-only 的代價），
        查詢時 read_parquet 須逐檔開啟。檔數隨排程線性成長，而內容多半只有幾十列。

        為何安全：
          · 合併只是搬家——每一列（含 ingest_time、revision）原樣保留，
            as_of 回放與「tier 最小、ingest 最新」的去重結果不變。
          · 先寫 .tmp、改名，**再**刪原檔。改名到刪除之間讀者會看到重複列，
            但重複列完全相同，被查詢的 DISTINCT ON 吸收。
          · 只刪本次讀過的檔；合併期間擷取端新寫入的檔不受影響。
        """
        codes = params or [p.name for p in self.parquet_root.iterdir() if p.is_dir()]
        report: list[dict] = []
        for code in sorted(codes):
            code_dir = self.parquet_root / code
            if not code_dir.is_dir():
                continue
            for part in sorted(p for p in code_dir.iterdir() if p.is_dir()):
                files = sorted(part.glob("*.parquet"))
                if len(files) < min_files:
                    continue
                tables = [self._conform(pq.read_table(f)) for f in files]
                merged = pa.concat_tables(tables).sort_by(
                    [("valid_time", "ascending"), ("ingest_time", "ascending")]
                )
                rows = sum(t.num_rows for t in tables)
                entry = {"param_code": code, "partition": part.name,
                         "files_in": len(files), "rows": rows,
                         "bytes_in": sum(f.stat().st_size for f in files)}
                if not dry_run:
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                    final = part / f"compacted-{stamp}.parquet"
                    tmp = final.with_suffix(".parquet.tmp")
                    pq.write_table(merged, tmp, compression="zstd")
                    if pq.read_metadata(tmp).num_rows != rows:   # 寫壞就不動原檔
                        tmp.unlink(missing_ok=True)
                        raise RuntimeError(f"{part} 合併後列數不符，已中止")
                    tmp.replace(final)
                    for f in files:
                        f.unlink()
                    entry["bytes_out"] = final.stat().st_size
                report.append(entry)
        return report

    @staticmethod
    def _conform(table: pa.Table) -> pa.Table:
        """舊檔可能缺後來新增的欄位：補 null 後依現行 schema 排序並轉型。"""
        cols = []
        for field_ in ARROW_SCHEMA:
            if field_.name in table.column_names:
                cols.append(table.column(field_.name).cast(field_.type))
            else:
                cols.append(pa.nulls(table.num_rows, type=field_.type))
        return pa.Table.from_arrays(cols, schema=ARROW_SCHEMA)

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
