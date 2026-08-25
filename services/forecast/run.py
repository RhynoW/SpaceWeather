"""services.forecast.run — 預報引擎進入點。

用法：
    python -m services.forecast.run --verify                 全 horizon 驗證擂台
    python -m services.forecast.run --verify --horizon 48    單一 horizon
    python -m services.forecast.run --verify --target hp30   1／3／6 h（Hp30 格點）
    python -m services.forecast.run --predict                產生預報並寫入資料層
    python -m services.forecast.run --coverage               特徵覆蓋率體檢

預報結果以 `data_type=FCS` 寫回 swx_observation，與來源自帶的預測（PRD/PRM）
區分開來，這樣事後才能分辨「誰預報的」。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from swx_core import DATA_TYPE_FCS, SwxStore, normalize

from .features import (HORIZONS, KP_TARGET, OPERATIONAL_HORIZON_LIMIT_H, TARGETS,
                       TargetSpec, build_dataset, build_features, feature_coverage,
                       load_panel)
from .models import GbmForecaster, default_models
from .skill import SKILL_FIELDS, write_skill
from .verify import evaluate, transfer_warning, verdict

RULE = "─" * 78

#: 預報列的 `confidence`（構想書明列的 KPI 之一），值域 [0, 1]。
#:
#: 刻意**不隨單筆機率浮動**：機率分類器在測試折上過擬合（訓練 POD 約 0.83、
#: 測試 0.38–0.02），把「這一筆有多篤定」當成可信度，等於把過擬合當成信心。
#: 改以驗證擂台實測的技巧分層——這是事後可複核的依據，不是模型的自我感覺。
#: 數字出處：docs/forecast_verification.md。
_CONFIDENCE_WITHIN_LIMIT = 0.6   # ≤12 h：GBM 穩定勝 persistence 約 7.5%
_CONFIDENCE_BEYOND_LIMIT = 0.3   # >12 h：24 h 起 BSS 轉負，48 h 由氣候平均勝出


def forecast_confidence(horizon_h: int) -> float:
    """該 horizon 之預報列的可信度。"""
    return (_CONFIDENCE_WITHIN_LIMIT if horizon_h <= OPERATIONAL_HORIZON_LIMIT_H
            else _CONFIDENCE_BEYOND_LIMIT)


def _panel(store: SwxStore, as_of: datetime | None = None,
           spec: TargetSpec = KP_TARGET) -> pd.DataFrame:
    return load_panel(store, as_of=as_of, spec=spec)


def cmd_coverage(store: SwxStore, spec: TargetSpec = KP_TARGET) -> int:
    panel = _panel(store, spec=spec)
    f = build_features(panel, spec)
    cov = feature_coverage(f)
    print(f"特徵數 {len(cov)}，樣本期間 {panel.index.min():%Y-%m-%d} → {panel.index.max():%Y-%m-%d}"
          f"（{len(panel)} 個 3 小時格點）\n")
    print("覆蓋率最低的 15 個特徵：")
    print(cov.head(15).to_string(index=False))
    low = cov[cov["coverage"] < 0.5]
    if not low.empty:
        print(f"\n⚠ {len(low)} 個特徵覆蓋率低於 50%。若其中含太陽風耦合項，"
              "報告中不可宣稱「以太陽風驅動預報」。")
    return 0


def cmd_verify(store: SwxStore, horizons: tuple[int, ...], n_splits: int,
               objective: str = "csi", spec: TargetSpec = KP_TARGET,
               write_summary: bool = False) -> int:
    panel = _panel(store, spec=spec)
    features = build_features(panel, spec)
    print(f"驗證擂台　目標 {spec.label}　格點 {spec.grid}")
    print(f"樣本期間 {panel.index.min():%Y-%m-%d} → {panel.index.max():%Y-%m-%d}")
    print(f"滾動起報 {n_splits} 折，訓練集永遠在測試集之前並留 7 天 gap\n")

    summary = []
    skill: dict[str, dict] = {}
    for h in horizons:
        X, y, _ = build_dataset(panel, h, features=features, spec=spec)
        print(f"{RULE}\n【horizon {h} 小時】樣本 {len(X)}，"
              f"其中 {spec.short}≥{spec.storm_threshold} 佔 "
              f"{100 * (y >= spec.storm_threshold).mean():.1f}%\n{RULE}")
        table = evaluate(default_models(spec.storm_threshold), X, y,
                         n_splits=n_splits, objective=objective,
                         storm_threshold=spec.storm_threshold, horizon_h=h,
                         merge_gap_h=max(3.0, 2 * spec.grid_h))
        if table.empty:
            print("資料不足，略過。\n")
            continue
        cols = [c for c in ["model", "tier", "n", "MAE", "MAE_lo", "MAE_hi", "RMSE",
                            "thr", "hits", "false_alarms", "misses", "correct_neg",
                            "POD", "POD_train", "FAR", "CSI", "HSS", "BSS",
                            "episodes", "ep_recall", "lead_h_med", "lead_n",
                            "skill_vs_persistence"]
                if c in table.columns]
        print(table[cols].to_string(index=False))
        skipped = table[table["status"] != "ok"] if "status" in table.columns else table.iloc[:0]
        for _, r in skipped.iterrows():
            print(f"　⚠ {r['model']}：{r.get('status')}——{r.get('note')}")
        warn = transfer_warning(table, objective)
        if warn:
            print("")
            print(warn)
        print(f"\n結論：{verdict(table)}\n")
        ok_rows = table[table["status"] == "ok"]
        if not ok_rows.empty:
            skill[str(h)] = {
                "samples": int(len(X)),
                "event_rate": round(float((y >= spec.storm_threshold).mean()), 4),
                "confidence": forecast_confidence(h),
                "models": [
                    {k: (None if pd.isna(r[k]) else
                         (r[k].item() if hasattr(r[k], "item") else r[k]))
                     for k in SKILL_FIELDS if k in ok_rows.columns}
                    for _, r in ok_rows.iterrows()
                ],
            }

        best = ok_rows.nsmallest(1, "MAE")
        if not best.empty:
            row = {"horizon_h": h, "best_model": best.iloc[0]["model"],
                   "MAE": best.iloc[0]["MAE"], "POD": best.iloc[0]["POD"],
                   "FAR": best.iloc[0]["FAR"]}
            for col in ("ep_recall", "lead_h_med", "lead_n"):
                if col in best.columns:
                    row[col] = best.iloc[0][col]
            summary.append(row)

    if summary:
        print(f"{RULE}\n各 horizon 最佳模型\n{RULE}")
        print(pd.DataFrame(summary).to_string(index=False))
        print("\n提前量以事件段計：告警的目標時刻落在事件段內才算命中，"
              "提前量 = horizon −（首次命中的目標時刻 − 事件起始），上限即 horizon。")
        print("lead_n 為可算出提前量的事件段數；只看提前量不看 ep_recall 會誤讀。")
        print("\n判讀：技巧隨 horizon 下降的主因是 L1 太陽風僅約 30–60 分鐘先導期，")
        print("超過此範圍已無即時觀測可用，24 小時以上主要靠 27 日復現與氣候態。")
        print(f"但本結果僅代表**此配置**（單一模型族、目標為 {spec.label}、"
              "無 CME 到達資訊），")
        print("不足以斷言長 horizon 不可能有技巧。")

    if write_summary and skill:
        meta = {
            "label": spec.label,
            "param_code": spec.code,
            "grid": spec.grid,
            "storm_threshold": spec.storm_threshold,
            "splits": n_splits,
            "objective": objective,
            "sample_span_utc": [f"{panel.index.min():%Y-%m-%dT%H:%M:%SZ}",
                                f"{panel.index.max():%Y-%m-%dT%H:%M:%SZ}"],
            "generated_utc": f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}",
            "command": (f"python -m services.forecast.run --verify "
                        f"--target {spec.key} --splits {n_splits}"),
        }
        out = write_skill(spec.key, skill, meta)
        print(f"\n成績已寫入 {out}（API 與儀表板據此標示技巧與提前量）。")
    return 0


def cmd_predict(store: SwxStore, horizons: tuple[int, ...], write: bool,
                spec: TargetSpec = KP_TARGET) -> int:
    panel = _panel(store, spec=spec)
    features = build_features(panel, spec)
    # 起報錨點是**目標變數最後一筆觀測的時刻**，不是「現在」，也不是最後一筆特徵。
    #
    # 兩個理由。其一，資料落後時「現在」與錨點可能差好幾天，印成現在會讓人以為
    # 預報是新鮮的——目標時刻明明還在上週。其二，面板的最後一格由所有參數的
    # 聯集決定：太陽風每分鐘更新，Hp30 慢好幾小時，於是最後一格常常是
    # 「有太陽風、沒有目標值」的狀態。從那裡起報，模型看不到自己要外推的量，
    # 而且 horizon 會相對於一個沒有觀測的時刻計算——API 與儀表板據最新觀測
    # 還原 horizon 時就對不上（曾出現 1 小時預報被還原成 15.5 小時）。
    #
    # 錨點取自**資料層的觀測序列**而非面板：面板為了讓特徵連續，會把慢速參數
    # 沿用數格（Hp30 在 30 分鐘格點上沿用 3 小時），最後幾格的目標值其實是
    # 補出來的。從補出來的格子起報，等於宣稱我們有一筆並不存在的觀測。
    observed = store.series(spec.code, observed_only=True)
    if observed.empty:
        print(f"資料層沒有 {spec.code} 的觀測值，無法起報。")
        return 1
    anchor = pd.Timestamp(observed.index.max()).floor(spec.grid)
    if anchor not in features.index:
        pos = features.index.asof(anchor)
        if pd.isna(pos):
            print(f"{spec.code} 最後觀測 {anchor} 之前沒有可用特徵，無法起報。")
            return 1
        anchor = pos
    now = pd.Timestamp.now(tz="UTC")

    recs = []
    print(f"起報錨點（{spec.code} 最後觀測，對齊 {spec.grid} 格點）"
          f"{anchor:%Y-%m-%d %H:%MZ}　產生於 {now:%Y-%m-%d %H:%MZ}")
    stale_h = (now - anchor).total_seconds() / 3600.0
    if stale_h > 6:
        print(f"⚠ 資料已落後 {stale_h:.0f} 小時，以下預報的目標時刻皆為過去，"
              "僅供流程驗證，不可作為現況判讀。")
    print()
    print(f"{'horizon':>8} {'目標時刻':<20} {spec.short + ' 預報':>10} "
          f"{'P(≥' + format(spec.storm_threshold, 'g') + ')':>9}  模型")
    for h in horizons:
        X, y, _ = build_dataset(panel, h, features=features, spec=spec)
        if len(X) < 500:
            print(f"{h:>6}h  訓練樣本不足（{len(X)}），略過")
            continue

        model = GbmForecaster(storm_threshold=spec.storm_threshold).fit(X, y)
        latest = features.loc[[anchor]]
        if latest.notna().sum(axis=1).iloc[0] < len(features.columns) * 0.5:
            print(f"{h:>6}h  最新特徵缺漏過多，略過")
            continue

        value = float(model.predict(latest)[0])
        prob = float(model.predict_proba_storm(latest)[0])
        target = latest.index[0] + pd.Timedelta(hours=h)
        print(f"{h:>6}h  {target:%Y-%m-%d %H:%MZ}  {value:>10.2f} {prob:>9.1%}  gbm")

        recs.extend(
            [
                {"valid_time": target, "param_code": spec.code, "value": round(value, 2),
                 "unit": "1", "source_id": "swx_forecast", "source_tier": 1,
                 "data_type": DATA_TYPE_FCS, "confidence": forecast_confidence(h)},
                {"valid_time": target, "param_code": spec.prob_code,
                 "value": round(prob, 4),
                 "unit": "1", "source_id": "swx_forecast", "source_tier": 1,
                 "data_type": DATA_TYPE_FCS, "confidence": forecast_confidence(h)},
            ]
        )

    if recs and write:
        result = store.write(normalize(pd.DataFrame(recs)), source_id="swx_forecast")
        print(f"\n{result}")
        print("預報以 data_type=FCS 寫入，與來源自帶預測（PRD/PRM）區分。")
    elif recs:
        print("\n（未寫入資料層；加 --write 以寫入）")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SWX-SDA 短時預報引擎（Kp 3–48 h／Hp30 1–6 h）")
    ap.add_argument("--verify", action="store_true", help="執行驗證擂台")
    ap.add_argument("--predict", action="store_true", help="產生最新預報")
    ap.add_argument("--coverage", action="store_true", help="特徵覆蓋率體檢")
    ap.add_argument("--horizon", type=int, default=None, help="只跑單一 horizon")
    ap.add_argument("--splits", type=int, default=5, help="滾動驗證折數")
    ap.add_argument("--objective", default="csi", choices=["csi", "hss", "pod"],
                    help="操作點目標，於**訓練折**上選定：csi／hss 最大化該指標；"
                         "pod 在訓練折滿足 POD≥0.7 前提下最小化 FAR。"
                         "注意訓練折達標不保證測試折達標，程式會標示落差")
    ap.add_argument("--write", action="store_true", help="predict 時寫入資料層")
    ap.add_argument("--write-summary", action="store_true",
                    help="把驗證成績寫入 docs/forecast_skill.json，供 API 與儀表板引用")
    ap.add_argument("--target", default="kp", choices=sorted(TARGETS),
                    help="預報目標：kp（3 小時格點，3–48 h）或 hp30"
                         "（30 分鐘格點，1／3／6 h——構想書要求的 1 小時產品在此）")
    args = ap.parse_args(argv)

    store = SwxStore()
    spec = TARGETS[args.target]
    horizons = (args.horizon,) if args.horizon else spec.horizons

    if args.coverage:
        return cmd_coverage(store, spec)
    if args.verify:
        return cmd_verify(store, horizons, args.splits, args.objective, spec,
                          write_summary=args.write_summary)
    if args.predict:
        return cmd_predict(store, horizons, args.write, spec)

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
