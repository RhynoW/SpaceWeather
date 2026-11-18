"""合併 Parquet 分區碎檔（維運工具）。

每次擷取都在分區內新增一檔；長期排程下檔數線性成長而每檔只有幾十列，
查詢成本隨之上升。本工具把同一分區的多檔合併為一檔，**不刪任何一列**，
as_of 回放結果不變（見 SwxStore.compact 說明）。

    python tools/compact_store.py --dry-run          # 只列出會合併什麼
    python tools/compact_store.py                    # 合併 SWX_DATA_DIR 下的資料
    python tools/compact_store.py --param KP_3H      # 只合併指定參數

建議放在每日擷取之後的離峰排程。示範快照（data/demo）受版控，不要對它執行。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))
sys.path.insert(0, str(ROOT))

import services  # noqa: E402,F401  （主控台 UTF-8）
from swx_core import SwxStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--param", action="append", help="只處理此參數（可重複）")
    ap.add_argument("--min-files", type=int, default=2, help="分區至少幾檔才合併")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--data-dir", type=Path, help="資料根目錄（預設 SWX_DATA_DIR）")
    args = ap.parse_args(argv)

    store = SwxStore(args.data_dir) if args.data_dir else SwxStore()
    before = store.storage_summary()
    report = store.compact(args.param, min_files=args.min_files, dry_run=args.dry_run)

    files_in = sum(r["files_in"] for r in report)
    rows = sum(r["rows"] for r in report)
    verb = "將合併" if args.dry_run else "已合併"
    print(f"{verb} {len(report)} 個分區：{files_in} 檔 → {len(report)} 檔（{rows} 列，不刪列）")
    if not before.empty:
        print(f"合併前總檔數 {int(before['files'].sum())}")
    if not args.dry_run:
        after = store.storage_summary()
        if not after.empty:
            print(f"合併後總檔數 {int(after['files'].sum())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
