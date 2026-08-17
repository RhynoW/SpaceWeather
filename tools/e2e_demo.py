"""tools/e2e_demo.py — 端到端最小鏈路演練（架構書 §17 第 3 項）。

一次跑完六層，證明資料契約在各層之間確實可用：

    來源 → 資料層(雙時間軸) → 規則引擎 → 事件卡 → STK 匯入檔 → 密度修正因子

預設以 2024-05 Gannon G5 事件為題材（構想書明列之案例）。
加 --as-of 可進入回放模式，只用該時刻前已入庫的資料，用來驗證「無前視偏差」。

用法：
    python tools/e2e_demo.py
    python tools/e2e_demo.py --start 2022-02-01 --end 2022-02-10   # Starlink 再入事件
    python tools/e2e_demo.py --as-of 2024-05-10T12:00Z             # 回放
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages"))

import pandas as pd  # noqa: E402

import services  # noqa: E402,F401  (路徑與主控台編碼設定)
from services.exporter import drag_correction, stk_spaceweather  # noqa: E402
from services.risk_engine.engine import RiskEngine  # noqa: E402
from services.risk_engine.eventcard import EventStore, build_event_cards  # noqa: E402
from swx_core import SwxStore, quality_summary  # noqa: E402

RULE = "─" * 78


def section(n: int, title: str) -> None:
    print(f"\n{RULE}\n【{n}】{title}\n{RULE}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SWX-SDA 端到端最小鏈路演練")
    ap.add_argument("--start", default="2024-05-08")
    ap.add_argument("--end", default="2024-05-15")
    ap.add_argument("--as-of", default=None, help="回放模式：只用該時刻前已入庫的資料")
    ap.add_argument("--outdir", default=None)
    args = ap.parse_args(argv)

    start = pd.Timestamp(args.start, tz="UTC").to_pydatetime()
    end = pd.Timestamp(args.end, tz="UTC").to_pydatetime()
    as_of: datetime | None = (
        pd.Timestamp(args.as_of, tz="UTC").to_pydatetime() if args.as_of else None
    )

    store = SwxStore()
    outdir = Path(args.outdir) if args.outdir else store.root / "exports" / "e2e"
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"SWX-SDA 端到端演練　期間 {args.start} → {args.end}"
          + (f"　（回放至 {args.as_of}）" if as_of else ""))

    # ── 1. 資料層 ──────────────────────────────────────────────────────
    section(1, "資料層：雙時間軸查詢")
    params = ["KP_3H", "AP_AVG", "F107_OBS"]
    obs = store.query(params, start=start, end=end, as_of=as_of)
    if obs.empty:
        print("資料層無資料。請先執行：python -m services.ingest.run --source all")
        return 1
    print(f"取得 {len(obs)} 列，涵蓋 {sorted(obs['param_code'].unique())}")
    print(f"來源：{sorted(obs['source_id'].unique())}")
    qs = quality_summary(obs)
    print("\n品質旗標分布：")
    print(qs.to_string(index=False))

    kp = store.series("KP_3H", start=start, end=end, as_of=as_of)
    if not kp.empty:
        print(f"\nKp 峰值 {kp.max():.2f}（{kp.idxmax():%Y-%m-%d %H:%MZ}）")

    # ── 2. 規則引擎 ────────────────────────────────────────────────────
    section(2, "風險轉譯層：L0–L4 分級")
    engine = RiskEngine(store)
    episodes, status = engine.evaluate(start=start, end=end, as_of=as_of)
    if not episodes:
        print("此期間無規則命中（L0）。")
    else:
        df = pd.DataFrame([e.to_dict() for e in episodes])
        print(df[["rule_id", "domain", "level", "start", "end", "duration_h",
                  "peak_param", "peak_value"]].to_string(index=False))
    unavailable = status[status["status"] == "unavailable"]
    if not unavailable.empty:
        print(f"\n因缺資料而無法判定的規則 {len(unavailable)} 條"
              f"（{', '.join(sorted(set(unavailable['domain'])))}）——"
              "此為「沒資料」而非「沒事」，需 C2/C3 來源到位後才生效。")

    # ── 3. 事件卡 ──────────────────────────────────────────────────────
    section(3, "事件卡：SDA 介接物")
    cards = build_event_cards(episodes, store=store)
    event_store = EventStore()
    for card in cards:
        event_store.upsert(card)
    if not cards:
        print("無事件卡。")
    else:
        for card in cards:
            d = card.to_dict()
            print(f"{d['event_id']}　{d['type']}　等級 {d['mission_level']}"
                  f"（國際 {d['international_scale']}）　可信度 {d['confidence']}")
            print(f"  時間軸：{d['timeline']['onset_utc']} → {d['timeline']['expected_end_utc']}"
                  f"（{d['timeline']['duration_h']} 小時）")
            for imp in d["impacts"]:
                mark = "（間接推估）" if imp.get("inference") == "proxy" else ""
                print(f"  影響 {imp['domain']:<18} {imp['level']}{mark}"
                      f"　已排除：{'、'.join(imp['exclusions_checked']) or '—'}")
            print(f"  通報對象：{'、'.join(d['notify']) or '—'}")
            print(f"  SDA 掛鉤：{d['sda_hooks']}")
        path = outdir / f"{cards[0].event_id}.json"
        path.write_text(cards[0].to_json(), encoding="utf-8")
        print(f"\n事件卡 JSON → {path}")

    # ── 4. STK 匯入檔 ──────────────────────────────────────────────────
    section(4, "STK/GMAT CSSI 驅動檔")
    wide = stk_spaceweather.build_frame(store, as_of=as_of, mode=stk_spaceweather.MODE_SOURCE)
    stk_path = outdir / "SpaceWeather-All-v1.2.txt"
    from swx_core import cssi

    cssi.write_file(wide, stk_path)
    info = stk_spaceweather.summary(wide)
    print(f"已產生 {stk_path}")
    print(f"  {info['date_min']} → {info['date_max']}，{info['rows']} 天，區段 {info['sections']}")
    print("  用法：置入 STK 的 CSSI 太空天氣檔路徑，HPOP 選 NRLMSISE-00／JB2008 即生效")

    # ── 5. 密度修正因子 ────────────────────────────────────────────────
    section(5, "大氣密度修正因子（議題四產品）")
    dc = drag_correction.build(store, start=start, end=end, as_of=as_of)
    if dc.empty:
        print("無法產生修正因子。")
    else:
        peak = dc.loc[dc["storm_ratio"].idxmax()]
        print(f"最大修正倍率 {peak['storm_ratio']:.2f}× ± {peak['uncertainty']:.2f}"
              f"（{peak['alt_band_km']} km，{peak['valid_time']:%Y-%m-%d %H:%MZ}，"
              f"Ap={peak['ap']:.0f}）")
        by_band = dc.groupby("alt_band_km")["storm_ratio"].max().round(2)
        print("\n各高度帶峰值倍率：")
        print(by_band.to_string())
        dc_path = outdir / "drag_correction.csv"
        drag_correction.export(dc, dc_path)
        print(f"\n修正因子表 → {dc_path}")
        print("  基準：同一 F10.7、地磁寧靜（Ap=4）；尚未由觀測反演校準")

    # ── 6. 總結 ────────────────────────────────────────────────────────
    section(6, "鏈路總結")
    print(f"資料層 {len(obs)} 列 → 規則命中 {len(episodes)} 段 → 事件卡 {len(cards)} 張"
          f" → STK 檔 {info['rows']} 天 → 修正因子 {len(dc)} 列")
    print(f"輸出目錄：{outdir}")
    if as_of:
        print(f"\n本次為回放模式（as_of={args.as_of}）：所有結果僅使用該時刻前已入庫的資料，"
              "無前視偏差。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
