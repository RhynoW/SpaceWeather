"""services.forecast.run — 預報引擎進入點。

用法：
    python -m services.forecast.run --verify                 全 horizon 驗證擂台
    python -m services.forecast.run --verify --horizon 48    單一 horizon
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

from .features import HORIZONS, STORM_THRESHOLD, build_dataset, build_features, feature_coverage, load_panel
from .models import GbmForecaster, default_models
from .verify import evaluate, transfer_warning, verdict

RULE = "─" * 78


def _panel(store: SwxStore, as_of: datetime | None = None) -> pd.DataFrame:
    return load_panel(store, as_of=as_of)


def cmd_coverage(store: SwxStore) -> int:
    panel = _panel(store)
    f = build_features(panel)
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
               objective: str = "csi") -> int:
    panel = _panel(store)
    features = build_features(panel)
    print(f"驗證擂台　樣本期間 {panel.index.min():%Y-%m-%d} → {panel.index.max():%Y-%m-%d}")
    print(f"滾動起報 {n_splits} 折，訓練集永遠在測試集之前並留 7 天 gap\n")

    summary = []
    for h in horizons:
        X, y, _ = build_dataset(panel, h, features=features)
        print(f"{RULE}\n【horizon {h} 小時】樣本 {len(X)}，"
              f"其中 Kp≥{STORM_THRESHOLD} 佔 {100 * (y >= STORM_THRESHOLD).mean():.1f}%\n{RULE}")
        table = evaluate(default_models(), X, y, n_splits=n_splits, objective=objective)
        if table.empty:
            print("資料不足，略過。\n")
            continue
        cols = [c for c in ["model", "tier", "n", "MAE", "MAE_lo", "MAE_hi", "RMSE",
                            "thr", "hits", "false_alarms", "misses", "correct_neg",
                            "POD", "POD_train", "FAR", "CSI", "HSS", "BSS",
                            "skill_vs_persistence"]
                if c in table.columns]
        print(table[cols].to_string(index=False))
        warn = transfer_warning(table, objective)
        if warn:
            print("")
            print(warn)
        print(f"\n結論：{verdict(table)}\n")
        best = table[table["status"] == "ok"].nsmallest(1, "MAE")
        if not best.empty:
            summary.append({"horizon_h": h, "best_model": best.iloc[0]["model"],
                            "MAE": best.iloc[0]["MAE"], "POD": best.iloc[0]["POD"],
                            "FAR": best.iloc[0]["FAR"]})

    if summary:
        print(f"{RULE}\n各 horizon 最佳模型\n{RULE}")
        print(pd.DataFrame(summary).to_string(index=False))
        print("\n判讀：技巧隨 horizon 下降的主因是 L1 太陽風僅約 30–60 分鐘先導期，")
        print("超過此範圍已無即時觀測可用，24 小時以上主要靠 27 日復現與氣候態。")
        print("但本結果僅代表**此配置**（單一模型族、目標為 Kp、無 CME 到達資訊），")
        print("不足以斷言長 horizon 不可能有技巧。")
    return 0


def cmd_predict(store: SwxStore, horizons: tuple[int, ...], write: bool) -> int:
    panel = _panel(store)
    features = build_features(panel)
    issued = pd.Timestamp.now(tz="UTC").floor("3h")

    recs = []
    print(f"起報時刻 {issued:%Y-%m-%d %H:%MZ}\n")
    print(f"{'horizon':>8} {'目標時刻':<20} {'Kp 預報':>8} {'P(Kp≥5)':>9}  模型")
    for h in horizons:
        X, y, _ = build_dataset(panel, h, features=features)
        if len(X) < 500:
            print(f"{h:>6}h  訓練樣本不足（{len(X)}），略過")
            continue

        model = GbmForecaster().fit(X, y)
        latest = features.loc[[features.index.max()]]
        if latest.notna().sum(axis=1).iloc[0] < len(features.columns) * 0.5:
            print(f"{h:>6}h  最新特徵缺漏過多，略過")
            continue

        kp = float(model.predict(latest)[0])
        prob = float(model.predict_proba_storm(latest)[0])
        target = latest.index[0] + pd.Timedelta(hours=h)
        print(f"{h:>6}h  {target:%Y-%m-%d %H:%MZ}  {kp:>8.2f} {prob:>9.1%}  gbm")

        recs.extend(
            [
                {"valid_time": target, "param_code": "KP_3H", "value": round(kp, 2),
                 "unit": "1", "source_id": "swx_forecast", "source_tier": 1,
                 "data_type": DATA_TYPE_FCS, "confidence": round(1 - abs(prob - 0.5) * 0 + 0.6, 2)},
                {"valid_time": target, "param_code": "KP_STORM_PROB", "value": round(prob, 4),
                 "unit": "1", "source_id": "swx_forecast", "source_tier": 1,
                 "data_type": DATA_TYPE_FCS},
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
    ap = argparse.ArgumentParser(description="SWX-SDA 48 小時預報引擎")
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
    args = ap.parse_args(argv)

    store = SwxStore()
    horizons = (args.horizon,) if args.horizon else HORIZONS

    if args.coverage:
        return cmd_coverage(store)
    if args.verify:
        return cmd_verify(store, horizons, args.splits, args.objective)
    if args.predict:
        return cmd_predict(store, horizons, args.write)

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
