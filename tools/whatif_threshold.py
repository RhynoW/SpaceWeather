"""tools/whatif_threshold.py — L0–L4 門檻校準模擬（架構書 §9.2）。

構想書的 TRL 表把「分級門檻須與需求單位共同校準，避免過度告警或漏報」列為風險。
化解這個風險的具體手段，就是能在會議上當場回答：

    「這組門檻，過去五年會發出幾次 L3？平均多久一次？最長持續多久？」

方法借自 Sat_TraingDataExtension 的 fusion_fpr_sweep.py / ids_domain_fpr.py：
先能重播歷史、算出告警頻率，門檻才有辦法討論。

用法：
    python tools/whatif_threshold.py --param KP_3H --sweep 4,5,6,7,8
    python tools/whatif_threshold.py --rule ORB-L3-KP6 --sweep 5,6,7
    python tools/whatif_threshold.py --param AP_AVG --sweep 30,50,80,150 --from 2021-01-01
"""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages"))

import pandas as pd  # noqa: E402

import services  # noqa: E402,F401
from services.risk_engine.engine import RiskEngine, load_rules  # noqa: E402
from swx_core import SwxStore, registry  # noqa: E402


def sweep_rule(
    engine: RiskEngine,
    rule,
    values: list[float],
    *,
    start,
    end,
    param: str | None = None,
) -> pd.DataFrame:
    """對單一規則掃描門檻，回傳每個門檻的告警統計。"""
    rows = []
    span_days = (end - start).total_seconds() / 86400.0

    for v in values:
        probe = copy.deepcopy(rule)
        scaled = False
        for cond in probe.conditions:
            if param is None or cond.param == param:
                # 解除門檻同比例縮放，維持原本的遲滯寬度
                if isinstance(probe.clear_below, dict) and cond.param in probe.clear_below:
                    ratio = probe.clear_below[cond.param] / cond.value if cond.value else 0.8
                    probe.clear_below[cond.param] = v * ratio
                elif isinstance(probe.clear_below, (int, float)) and cond.value:
                    probe.clear_below = v * (probe.clear_below / cond.value)
                cond.value = v
                scaled = True
        if not scaled:
            continue

        eps, status = engine.evaluate_rule(probe, start=start, end=end)
        if status != "ok":
            rows.append({"threshold": v, "status": status})
            continue

        durations = [e.duration_h for e in eps]
        rows.append(
            {
                "threshold": v,
                "status": "ok",
                "n_alerts": len(eps),
                "per_year": round(len(eps) / span_days * 365.25, 1) if span_days else None,
                "days_between": round(span_days / len(eps), 1) if eps else None,
                "total_hours": round(sum(durations), 1),
                "duty_cycle_pct": round(100 * sum(durations) / (span_days * 24), 2)
                if span_days else None,
                "median_h": round(float(pd.Series(durations).median()), 1) if eps else None,
                "max_h": round(max(durations), 1) if eps else None,
            }
        )
    return pd.DataFrame(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="L0–L4 門檻校準模擬")
    ap.add_argument("--rule", default=None, help="只掃描指定規則 ID")
    ap.add_argument("--param", default=None, help="只調整指定參數的門檻")
    ap.add_argument("--sweep", required=True, help="門檻值清單，逗號分隔")
    ap.add_argument("--from", dest="start", default=None, help="起始日期（預設為資料起點）")
    ap.add_argument("--to", dest="end", default=None)
    args = ap.parse_args(argv)

    values = [float(v) for v in args.sweep.split(",")]
    store = SwxStore()
    reg = registry()

    rules = load_rules()
    if args.rule:
        rules = [r for r in rules if r.rule_id == args.rule]
    elif args.param:
        rules = [r for r in rules if args.param in r.params]
    if not rules:
        print("找不到符合條件的規則。")
        return 1

    probe_param = args.param or rules[0].params[0]
    # 只用觀測值：拿來源的預測值來統計告警頻率會失真（架構書 P3）
    series = store.series(probe_param, observed_only=True)
    if series.empty:
        print(f"資料層無 {probe_param} 資料。")
        return 1

    start = pd.Timestamp(args.start, tz="UTC") if args.start else series.index.min()
    end = pd.Timestamp(args.end, tz="UTC") if args.end else series.index.max()
    span_days = (end - start).total_seconds() / 86400.0

    spec = reg.get(probe_param)
    print(f"門檻校準模擬　參數 {probe_param}"
          f"（{spec.name_zh if spec else ''}，單位 {spec.unit if spec else '?'}）")
    print(f"回放期間 {start:%Y-%m-%d} → {end:%Y-%m-%d}（{span_days:.0f} 天 "
          f"≈ {span_days / 365.25:.1f} 年）\n")

    engine = RiskEngine(store)
    for rule in rules:
        if args.param and args.param not in rule.params:
            continue
        print(f"── {rule.rule_id}　{rule.name}　（{rule.domain} / {rule.level}）")
        table = sweep_rule(engine, rule, values, start=start, end=end, param=args.param)
        if table.empty or (table.get("status") == "unavailable").all():
            print("   缺資料，無法評估\n")
            continue
        print(table.to_string(index=False))
        print()

    print("判讀方式：")
    print("  n_alerts       此門檻在回放期間會發出幾次告警")
    print("  per_year       換算成每年幾次（與需求單位討論可接受頻率的主要依據）")
    print("  duty_cycle_pct 告警狀態佔全期時間的百分比，偏高代表門檻過鬆")
    print("  max_h          單次告警最長持續時數，偏長代表遲滯設定需檢討")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
