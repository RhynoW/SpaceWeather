"""修補舊版回填寫入的預測列入庫時間（一次性維運工具）。

舊版 `--backfill` 不分型別，一律把 ingest_time 設成 `valid_time + 發布延遲`。
這對觀測列是合理近似，對預測列則是錯的：預測在有效時間**之前**就已發布，
照此推算會落在未來——2026-08-17 那次種子回填因此產生 1,253 筆入庫時間在
2026-12 到 2041 年的月預測列。它們在「取最新版本」時永遠勝出，蓋掉之後每一份
較新的預測，CSSI 匯出也被拉進錯的區段（月初日落進 MONTHLY_PREDICTED）。

程式已修正（`services.ingest.base.backfill_ingest_time`），此工具修補既有資料：
把符合下列**全部**條件的列，入庫時間改為該批的實際取得時刻：

  · source_id 為指定來源
  · data_type 不是 OBS／INT（觀測列的推算本來就合理，不動）
  · ingest_time 恰為 `valid_time + 發布延遲`——舊版回填的指紋；
    即時入庫的時間戳不會剛好落在這種偏移上
  · ingest_time 晚於取得時刻——資料不可能在被取得之前入庫

只改 ingest_time 一欄，不刪列、不改值。資料層原則上 append-only，這是對
**錯誤中繼資料**的一次性更正，故預設只演練；加 `--apply` 才寫入，並建議
`--backup` 保留原檔。示範快照（data/demo）受版控，原檔可由 git 取回。

    python tools/repair_backfill_ingest_time.py                       # 演練
    python tools/repair_backfill_ingest_time.py --apply --backup DIR  # 寫入
    python tools/repair_backfill_ingest_time.py --data-dir data/demo --apply
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

import services  # noqa: E402,F401  （主控台 UTF-8）
from swx_core import SwxStore, catalog  # noqa: E402
from swx_core.schema import OBSERVED_TYPES  # noqa: E402

# 2026-08-17 種子回填的實際寫入時刻（取自該批 Parquet 檔名）
DEFAULT_FETCHED_AT = "2026-08-17T13:34:50.193435Z"


def repair_file(path: Path, *, source_id: str, lag: pd.Timedelta,
                fetched: pd.Timestamp) -> tuple[pa.Table, int]:
    table = pq.read_table(path)
    df = table.select(["source_id", "data_type", "valid_time", "ingest_time"]).to_pandas()
    vt = pd.to_datetime(df["valid_time"], utc=True)
    it = pd.to_datetime(df["ingest_time"], utc=True)
    mask = (
        (df["source_id"] == source_id)
        & ~df["data_type"].isin(OBSERVED_TYPES)
        & (it == vt + lag)
        & (it > fetched)
    )
    n = int(mask.sum())
    if not n:
        return table, 0
    fixed = it.where(~mask, fetched)
    field = table.schema.field("ingest_time")
    col = pa.array(fixed, type=pa.timestamp("us", tz="UTC")).cast(field.type)
    idx = table.schema.get_field_index("ingest_time")
    return table.set_column(idx, field, col), n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", type=Path, help="資料根目錄（預設 SWX_DATA_DIR）")
    ap.add_argument("--source", default="celestrak_sw_all")
    ap.add_argument("--fetched-at", default=DEFAULT_FETCHED_AT, help="該批回填的實際取得時刻（UTC）")
    ap.add_argument("--apply", action="store_true", help="實際寫入（預設只演練）")
    ap.add_argument("--backup", type=Path, help="寫入前把原檔複製到此目錄（保留相對路徑）")
    args = ap.parse_args(argv)

    store = SwxStore(args.data_dir) if args.data_dir else SwxStore()
    lag = pd.Timedelta(seconds=catalog()[args.source].publication_lag_s)
    fetched = pd.Timestamp(args.fetched_at)
    base = store.root / "swx_parquet"

    files = rows = 0
    for path in sorted(base.rglob("*.parquet")):
        table, n = repair_file(path, source_id=args.source, lag=lag, fetched=fetched)
        if not n:
            continue
        files += 1
        rows += n
        if args.apply:
            if args.backup:
                dst = args.backup / path.relative_to(store.root)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dst)
            pq.write_table(table, path)

    verb = "已修補" if args.apply else "將修補（演練）"
    print(f"{base}：{verb} {rows} 列，{files} 檔；入庫時間改為 {fetched:%Y-%m-%d %H:%M:%S}Z")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
