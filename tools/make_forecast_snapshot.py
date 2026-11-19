"""產生隨版本發布的預報快照（data/demo/forecast_snapshot）。

雲端是空容器、只擷取近幾天資料，預報模型需要多年歷史才能訓練，所以雲端
**永遠無法自己產生預報**。本工具在開發環境把最新一批本系統預報連同它的
脈絡打包，讓預報頁在找不到即時預報時有東西可看，並清楚標示產生時刻。

每個目標打包三樣東西，全部保留原入庫時間：
  · 本系統預報   最新一批（source_id=swx_forecast，data_type=FCS）
  · 觀測脈絡     起報錨點前 3 天的觀測（只取觀測列）
  · 官方對照     起報當時已知的 NOAA SWPC Kp 預報（as_of＝產生時刻，不偷看後來的版本）

先在開發環境產生預報，再打包：
    python -m services.ingest.run --source gfz_hp30_nowcast
    python -m services.forecast.run --predict --write --target hp30
    python -m services.forecast.run --predict --write --target kp
    python tools/make_forecast_snapshot.py

每次執行會**整個重建**快照目錄（只留最新一份，舊快照由 git 歷史保存）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

import services  # noqa: E402,F401  （主控台 UTF-8）
from services import products  # noqa: E402
from services.forecast.skill import latest_forecast_batch  # noqa: E402
from swx_core import SwxStore  # noqa: E402

TARGETS = ("hp30", "kp")
CONTEXT = timedelta(days=3)
NOTE = ("開發環境產生、隨版本發布的預報快照，不是此刻的預報。"
        "雲端只擷取近幾天資料，無法訓練需要多年歷史的預報模型。")


def _commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def build(src: SwxStore, dst_root: Path) -> dict:
    if (dst_root / "swx_parquet").exists():
        shutil.rmtree(dst_root / "swx_parquet")
    dst_root.mkdir(parents=True, exist_ok=True)
    dst = SwxStore(dst_root)

    # 產生當下的 HEAD；打包的程式若尚未提交，實際版本會比它新
    meta: dict = {"note": NOTE, "generated_from_head": _commit(), "targets": {}}
    for target in TARGETS:
        spec = products.forecast_targets()[target]
        codes = [c for c in (spec.code, spec.prob_code) if c]
        own = latest_forecast_batch(src.query(codes, source_id="swx_forecast"))
        if own is None or own.empty:
            print(f"  {target}：開發環境沒有本系統預報，略過")
            continue
        generated = pd.Timestamp(own["ingest_time"].max())
        observed = src.query(spec.code, observed_only=True, as_of=generated.to_pydatetime(),
                             start=generated - CONTEXT, end=generated)
        anchor = pd.Timestamp(observed["valid_time"].max()).floor(spec.grid)
        reference = src.query(codes, source_id="swpc_geomag_forecast",
                              as_of=generated.to_pydatetime(),
                              start=anchor - CONTEXT, end=anchor + CONTEXT)
        for frame in (own, observed, reference):
            if frame.empty:
                continue
            for sid, part in frame.groupby("source_id"):
                dst.write(part.reset_index(drop=True), source_id=str(sid),
                          ingest_time=None, dedupe=False)
        meta["targets"][target] = {
            "generated_utc": generated.isoformat(),
            "anchor_utc": anchor.isoformat(),
            "rows": {"forecast": len(own), "observed": len(observed),
                     "reference": len(reference)},
        }
        print(f"  {target}：產生於 {generated:%Y-%m-%d %H:%MZ}，起報錨點 {anchor:%Y-%m-%d %H:%MZ}，"
              f"預報 {len(own)}／觀測 {len(observed)}／官方 {len(reference)} 列")

    (dst_root / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return meta


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=products.forecast_snapshot_dir())
    args = ap.parse_args(argv)
    meta = build(SwxStore(), args.out)
    if not meta["targets"]:
        print("沒有任何目標可打包")
        return 1
    print(f"已寫入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
