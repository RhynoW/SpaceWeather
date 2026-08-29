"""tools.alongtrack_drivers — 驅動量的選擇，在軌道上值多少公里？

預報擂台量出「45 天期的 Ap 預報等於氣候值、F10.7 的最佳模型是持續性」
（docs/forecast_verification.md 結果三）之後，真正該問的是**那又怎樣**。
本工具把那些 sfu 與 nT 換算成沿跡公里數，並與**密度模型自身的偏差**
（MSIS 平靜期典型 ±15%）擺在同一張圖上比大小。

三個情境，同一條時間軸、同一個彈道係數、同一個取樣座標：

  swpc45      SWPC 45 日預報的逐日 F10.7 與 Ap（作業上實際會拿到的東西）
  arena_best  擂台選出的最佳模型：F10.7 用持續性、Ap 用氣候平均
  msis_hi/lo  驅動量與 swpc45 相同，只把密度乘上 1.15／0.85

若 swpc45 與 arena_best 的差遠小於 ±15% 帶寬，結論就是：
**在這個尺度上，換一個更好的驅動量預報並不會讓軌道預測變準**，
瓶頸在密度模型本身。這是個不利於「加強長期預報」的結論，照實呈現。

用法：
    python -m tools.alongtrack_drivers --alt 500 --days 45
    python -m tools.alongtrack_drivers --alt 400 --bc 0.022 --lat 23.5 --lon 121
"""

from __future__ import annotations

import argparse
import sys
from datetime import timezone

import numpy as np
import pandas as pd

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "packages"))

from orbit_drag import BC_REFERENCE, DEFAULT_BC, Scenario, compare, constant_drivers  # noqa: E402
from swx_core import SwxStore  # noqa: E402

FORECAST_SOURCE = "swpc_45day_forecast"


def swpc_forecast_drivers(store: SwxStore) -> pd.DataFrame:
    """SWPC 45 日預報的逐日 F10.7 與 Ap。

    f107a（81 日中心平均）預報端沒有，以最後一筆觀測值沿用——
    它在 45 天內的變化遠小於 f107 本身，且兩個情境用同一個值，不影響差值。
    """
    now = pd.Timestamp.now(tz=timezone.utc)
    # **source_id 必須進查詢，不能事後過濾。**
    # store.query 會對 (param, valid_time, grid_id) 去重，同一天的 F10.7 預測
    # 可能由 swpc_27day_outlook 勝出；事後再篩 45 日來源，那些日子就整天消失。
    # 實測症狀是 45 天只剩 9 天，而且不會有任何錯誤訊息。
    fc = store.query(["F107_OBS", "AP_AVG"], start=now - pd.Timedelta(days=2),
                     end=now + pd.Timedelta(days=60), source_id=FORECAST_SOURCE)
    if fc.empty:
        raise RuntimeError(
            "資料層沒有 45 日預報；請先執行 "
            f"`python -m services.ingest.run --source {FORECAST_SOURCE}`"
        )

    wide = (fc.pivot_table(index=fc["valid_time"].dt.floor("D"),
                           columns="param_code", values="value", aggfunc="last")
            .sort_index())
    wide.columns = [{"F107_OBS": "f107", "AP_AVG": "ap"}.get(c, c) for c in wide.columns]
    c81 = store.series("F107_OBS_C81", observed_only=True)
    wide["f107a"] = float(c81.iloc[-1]) if not c81.empty else wide["f107"].mean()
    wide.index = wide.index.tz_convert("UTC") if wide.index.tz else wide.index.tz_localize("UTC")
    return wide[["f107", "f107a", "ap"]].ffill()


def arena_best_drivers(store: SwxStore, dates) -> tuple[pd.DataFrame, dict]:
    """擂台選出的最佳模型：F10.7 持續性、Ap 氣候平均。

    這不是隨手挑的兩個基線，是 docs/forecast_verification.md 結果三的實測結論：
    F10.7 在每個提前量都由持續性勝出，Ap 在 3 天以上都由氣候平均勝出。
    """
    f107 = store.series("F107_OBS", observed_only=True)
    ap = store.series("AP_AVG", observed_only=True)
    if f107.empty or ap.empty:
        raise RuntimeError("資料層缺 F10.7 或 Ap 觀測值")

    daily_ap = ap.groupby(ap.index.floor("D")).mean()
    stats = {"f107_persist": float(f107.iloc[-1]),
             "ap_climatology": float(daily_ap.mean()),
             "ap_median": float(daily_ap.median()),
             "n_days": int(len(daily_ap))}
    c81 = store.series("F107_OBS_C81", observed_only=True)
    return constant_drivers(dates, f107=stats["f107_persist"], ap=stats["ap_climatology"],
                            f107a=float(c81.iloc[-1]) if not c81.empty else None), stats


def model_band(fc: pd.DataFrame, epochs, *, override: float | None = None):
    """密度模型的 ±1σ 倍率（逐時刻），回傳 (上界, 下界, 是否實測, sigma 序列)。

    **逐時刻而非單一常數**：實測顯示暴時的模式散布比平靜期大
    （ap≥50 的 1σ 為 0.282，ap<10 為 0.223）。用常數會在平靜段高估、
    暴時段低估，而暴時正是這個帶最有作業意義的時候。
    """
    from orbit_drag.calibration import sigma_log

    ap = fc["ap"].reindex(pd.DatetimeIndex(epochs)).ffill().bfill().to_numpy(dtype=float)
    if override is not None:
        sig = np.full(len(ap), float(override))
        calibrated = False
    else:
        pairs = [sigma_log(float(a)) for a in ap]
        sig = np.array([p[0] for p in pairs], dtype=float)
        calibrated = bool(pairs and all(p[1] for p in pairs))
    return np.exp(sig), np.exp(-sig), calibrated, sig


def main(argv: list[str] | None = None) -> int:
    ap_ = argparse.ArgumentParser(description="驅動量的選擇造成多少沿跡誤差")
    ap_.add_argument("--alt", type=float, default=500.0, help="初始高度 km")
    ap_.add_argument("--days", type=int, default=45, help="推算天數（上限即預報長度）")
    ap_.add_argument("--bc", type=float, default=DEFAULT_BC,
                     help=f"彈道係數 Cd·A/m（m^2/kg）。代表值：{BC_REFERENCE}")
    ap_.add_argument("--lat", type=float, default=0.0, help="密度取樣緯度")
    ap_.add_argument("--lon", type=float, default=0.0, help="密度取樣經度")
    ap_.add_argument("--model-bias", type=float, default=None,
                     help="密度模型 1σ（對數空間）。預設由 "
                          "docs/density_calibration.json 的實測值逐時刻決定；"
                          "給定時以此常數覆寫全段")
    args = ap_.parse_args(argv)

    store = SwxStore()
    fc = swpc_forecast_drivers(store)
    epochs = pd.DatetimeIndex(fc.index[:args.days])
    if len(epochs) < 2:
        print("預報天數不足，無法推算。")
        return 1

    best, stats = arena_best_drivers(store, epochs)
    hi, lo, calibrated, sig = model_band(fc, epochs, override=args.model_bias)
    band_label = (f"實測 1σ {sig.min():.2f}–{sig.max():.2f}" if calibrated
                  else f"未校準經驗值 {sig.mean():.2f}")
    scenarios = [
        Scenario("swpc45", "SWPC 45 日預報", fc),
        Scenario("arena_best", "擂台最佳（F10.7 持續性＋Ap 氣候）", best),
        Scenario("msis_hi", f"密度 +1σ（{band_label}）", fc, rho_scale=hi),
        Scenario("msis_lo", f"密度 -1σ（{band_label}）", fc, rho_scale=lo),
    ]

    table = compare(scenarios, epochs, args.alt, bc=args.bc, lat=args.lat, lon=args.lon)

    print(f"初始高度 {args.alt:.0f} km　彈道係數 {args.bc:g} m^2/kg　"
          f"取樣座標 ({args.lat:g}, {args.lon:g})")
    print(f"期間 {epochs[0]:%Y-%m-%d} → {epochs[-1]:%Y-%m-%d}（{len(epochs)} 天）")
    print(f"SWPC 預報 F10.7 {fc['f107'].min():.0f}–{fc['f107'].max():.0f} sfu、"
          f"Ap {fc['ap'].min():.0f}–{fc['ap'].max():.0f} nT")
    print(f"擂台最佳 F10.7 {stats['f107_persist']:.1f} sfu（持續性）、"
          f"Ap {stats['ap_climatology']:.1f} nT（{stats['n_days']} 天氣候平均）")
    print()

    pivot = table.pivot_table(index="epoch", columns="scenario", values="alongtrack_km")
    marks = [d for d in (7, 14, 30, args.days) if d <= len(epochs)]
    rows = []
    for d in marks:
        r = pivot.iloc[d - 1]
        rows.append({"天": d,
                     "換驅動量預報（km）": round(float(r["arena_best"]), 1),
                     "密度 +1sigma（km）": round(float(r["msis_hi"]), 1),
                     "密度 -1sigma（km）": round(float(r["msis_lo"]), 1)})
    print("相對「用 SWPC 45 日預報」的沿跡位置差")
    print(pd.DataFrame(rows).to_string(index=False))

    driver = abs(rows[-1]["換驅動量預報（km）"])
    model = max(abs(rows[-1]["密度 +1sigma（km）"]), abs(rows[-1]["密度 -1sigma（km）"]))
    print()
    print(f"第 {marks[-1]} 天：換驅動量預報差 {driver:.0f} km，"
          f"密度模型 ±1σ（{band_label}）差 {model:.0f} km。")
    if not calibrated:
        print("⚠ 密度不確定度**未經實測校準**（缺 docs/density_calibration.json）。")
    print()
    # 這一段是本工具的重點，也最容易被讀反，所以寫死在輸出裡而不是只寫在文件。
    print(f"**這個 {driver:.0f} 公里的差不是「修正量」，是不確定度。**")
    print("預報擂台（docs/forecast_verification.md 結果三）量到的是：45 天期的")
    print("F10.7 預報與持續性基線的 MAE 在同一量級（30.7 對 31.5 sfu），Ap 預報")
    print("與氣候平均也在同一量級。既然兩種驅動量假設的準確度分不出高下，")
    print("它們造成的沿跡差就**無法靠選一邊來消除**——那是這個提前量上的固有誤差。")
    print()
    if model > 3 * driver:
        print("此配置下密度模型偏差主導；改善驅動量預報的邊際效益低。")
    elif driver > 3 * model:
        print("此配置下驅動量假設主導；但依擂台結果，兩種假設的準確度分不出高下，")
        print("故正確的處置是把它計為不確定度，不是宣稱哪一種比較準。")
    else:
        print("兩者同量級。作業上應把兩者平方相加當作沿跡不確定度的下界，")
        print("而不是只報其中一項。")
    print()
    print("沿跡誤差以時間平方成長（Δa 近似線性，相位是它的積分），"
          "故天數加倍時差距約為四倍。")
    print("此為近圓軌道能量法的封閉解，未含 J2 共振、姿態變化與機動；"
          "用途是比較**量級**，不是預測某一顆衛星的實際位置。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
