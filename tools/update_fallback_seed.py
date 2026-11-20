"""更新 repo 內的 CelesTrak 本機備援檔（data/seed/SW-All.txt）。

雲端站台連不到 CelesTrak（TCP 連線在網路層被丟棄，見 docs/architecture.md §16.2 G19），
每輪都改讀這份備援檔。檔案是某個時刻的快照、不會自己變新：放著不管，雲端的
F10.7 81 日平均、STK 驅動檔、CSSI 匯出就停在檔案產製那天（曾停在 08-17 三個月）。
研討會或對外展示前，與預報快照一起在能連線的主機上執行：

    python tools/update_fallback_seed.py
    python tools/make_forecast_snapshot.py   # 見 README 部署章節

寫入前檢查：可解析為 CSSI、觀測列數合理、UPDATED 不早於現有檔案——
抓到錯誤頁面或較舊的版本時寧可不換，也不要把備援檔換壞。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

import services  # noqa: E402,F401  （主控台 UTF-8）
from tools.cssi_compare import _updated_date  # noqa: E402
from swx_core import catalog, cssi  # noqa: E402

SOURCE_ID = "celestrak_sw_all"
MIN_OBSERVED_ROWS = 1000      # SW-Last5Years 約 2 千列觀測；少於此數多半是錯誤頁或截斷


def validate(text: str) -> tuple[int, object]:
    """回傳 (觀測列數, UPDATED 日期)；不合格時拋 ValueError。"""
    updated = _updated_date(text)
    if updated is None:
        raise ValueError("找不到 UPDATED 標頭——不是 CelesTrak CSSI 檔")
    df = cssi.parse_text(text)
    n_obs = int((df["section"] == "OBSERVED").sum()) if not df.empty else 0
    if n_obs < MIN_OBSERVED_ROWS:
        raise ValueError(f"觀測列只有 {n_obs} 列（預期 ≥{MIN_OBSERVED_ROWS}）——檔案可能被截斷")
    return n_obs, updated


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--from-file", type=Path, help="改用已下載的檔案（不連網）")
    args = ap.parse_args(argv)

    spec = catalog()[SOURCE_ID]
    target = ROOT / spec.raw["local_fallback"]

    if args.from_file:
        text = args.from_file.read_text(encoding="utf-8", errors="replace")
    else:
        resp = requests.get(spec.endpoint, timeout=60)
        resp.raise_for_status()
        text = resp.text

    n_obs, updated = validate(text)
    old = _updated_date(target.read_text(encoding="utf-8", errors="replace")) if target.exists() else None
    if old is not None and updated < old:
        print(f"下載的版本（{updated:%Y-%m-%d}）比現有備援檔（{old:%Y-%m-%d}）舊，不更換")
        return 1

    tmp = target.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(target)
    was = f"（原 {old:%Y-%m-%d}）" if old is not None else ""
    print(f"已更新 {target.relative_to(ROOT)}：UPDATED {updated:%Y-%m-%d}{was}，觀測 {n_obs} 列")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
