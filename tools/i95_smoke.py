"""tools/i95_smoke.py — e-GNSS I95 連線與版面煙霧測試。

單元測試不連網（合成圖驗證換算邏輯），所以**外部端點改版時只有這支會紅燈**。
與 tools/media_smoke.py 同樣的分工。

檢查三件事：

  1. 頁面連得上（憑證需 tls_relaxed_strict，標準驗證會失敗）；
  2. 三個網的圖檔連結都在；
  3. 每張圖都能擷取出逐時值——版面若改版，這裡會空手而回。

用法：
    python tools/i95_smoke.py            # 只檢查，不寫入資料層
    python tools/i95_smoke.py --write    # 檢查並寫入（來源仍為 planned，需自行確認條款）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "packages"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from services.ingest.nlsc_egnss import extract_i95          # noqa: E402
from services.ingest.run import build                        # noqa: E402
from swx_core import SwxStore, catalog                       # noqa: E402

BANDS = ((30, "L3 建議避開該時段"), (20, "L2 僅環境良好可嘗試"),
         (8, "L1 超過警戒值"), (0, "L0 低於警戒值"))


def band(value: float) -> str:
    return next(label for threshold, label in BANDS if value >= threshold)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="e-GNSS I95 煙霧測試")
    ap.add_argument("--write", action="store_true", help="同時寫入資料層")
    args = ap.parse_args(argv)

    spec = catalog()["nlsc_egnss_i95"]
    conn = build(spec, SwxStore())
    payload, mode = conn.fetch_bytes()
    charts = conn._chart_urls(payload)
    print(f"頁面 [{mode}] {len(payload):,} bytes，找到 {len(charts)} 張圖表")
    if not charts:
        print("✗ 頁面中找不到 I95 圖表連結——版面可能已改版")
        return 1

    failed = 0
    for name, url, day in charts:
        try:
            values = extract_i95(conn.fetch_related(url))
        except Exception as exc:                              # noqa: BLE001
            print(f"✗ {name}: {type(exc).__name__}: {exc}")
            failed += 1
            continue
        if not values:
            print(f"✗ {name}: 圖表無可辨識的長條")
            failed += 1
            continue
        peak_hour = max(values, key=values.get)
        print(f"✓ {name:10s} {day:%Y-%m-%d} {len(values):2d} 個時段，"
              f"峰值 {values[peak_hour]:.1f}（{peak_hour:02d}Z，{band(values[peak_hour])}）")

    if args.write:
        outcome = conn.run()
        print(f"\n{outcome}")
        print("提醒：本來源 status=planned，使用條款尚未與國土測繪中心確認。")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
