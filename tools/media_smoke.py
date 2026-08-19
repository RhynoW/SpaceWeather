"""tools.media_smoke — 影像與動畫的連線煙霧測試。

對 `configs/imagery.yaml` 裡的每一項（或只針對 STEM 頁引用的那些）實際發出
請求，確認**當下真的取得到**。單元測試不連網，所以外部端點改版、搬家或撤除時
不會紅燈——這支就是補那個缺口。

**刻意直接呼叫 `apps/dashboard/media_url.image_url`**，不自己組網址。
這支腳本原本有一份自己的解析邏輯，新增 `json_api` 類型時漏改，
於是煙霧測試報 FAIL 而 app 其實是好的——**測試與被測對象不一致，
比沒有測試更糟**。現在兩者共用同一個實作，漏改會同時出現在兩邊。

用法:
    python tools/media_smoke.py            # STEM 頁引用的媒體
    python tools/media_smoke.py --all      # 設定檔內全部
"""

from __future__ import annotations

import argparse
import ast
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages"))
sys.path.insert(0, str(ROOT / "apps" / "dashboard"))

MIN_BYTES = 1000
READ_CAP = 1 << 20          # 影片只讀前 1 MB，確認可串流即可


def stem_media_ids() -> list[str]:
    """STEM 頁引用的媒體 id，依原始碼出現順序。"""
    src = (ROOT / "apps" / "dashboard" / "stem.py").read_text(encoding="utf-8")
    out: list[str] = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in ("images_by_id", "animations_by_id"):
            out += [a.value for a in node.args
                    if isinstance(a, ast.Constant) and isinstance(a.value, str)]
    return out


def resolve(item: dict) -> str:
    """影像用共用解析器；動畫的網址或索引直接取用。"""
    from media_url import image_url

    if item.get("kind") in ("video", "frames"):
        return item.get("url") or item["index_url"]
    return image_url(item)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--all", action="store_true", help="檢查設定檔內全部媒體")
    ap.add_argument("--timeout", type=int, default=45)
    args = ap.parse_args()

    from swx_core import animations, imagery

    index = {i["id"]: i for i in imagery()} | {a["id"]: a for a in animations()}
    ids = sorted(index) if args.all else stem_media_ids()
    print(f"檢查 {len(ids)} 項\n")

    bad = 0
    for mid in ids:
        item = index.get(mid)
        if item is None:
            print(f"  MISSING  {mid:<22} 設定檔無此 id")
            bad += 1
            continue
        try:
            url = resolve(item)
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=args.timeout) as resp:
                size = len(resp.read(READ_CAP))
                ctype = resp.headers.get("Content-Type", "")
                status = resp.status
            ok = status == 200 and size > MIN_BYTES
            print(f"  {'OK ' if ok else 'BAD'}      {mid:<22} {status} "
                  f"{size:>9}B  {ctype[:24]}")
            bad += 0 if ok else 1
        except Exception as exc:      # noqa: BLE001 — 逐項回報，不中斷整批
            print(f"  FAIL     {mid:<22} {type(exc).__name__}: {exc}")
            bad += 1

    print(f"\n失敗 {bad} 項")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
