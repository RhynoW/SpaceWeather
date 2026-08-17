"""orbit_drag.atmospheric — 熱氣層密度與大氣阻力（架構書 §7.1，議題四核心）。

模型：**MSIS 2.1**（pymsis 預設版本；亦可切換 version=0 取 NRLMSISE-00）。

移植自 Sat_TraingDataExtension/atmospheric_drag.py（A7），物理核心相同，
但有兩處實質改動：

1. 驅動參數改由 swx_core.SwxStore 取得，而不是讀死 CSV。密度計算因而自動
   享有雙時間軸能力——回放時用「當時已知」的 F10.7/Ap（架構書 P3）。

2. **改用暴時 ap 模式**（`geomagnetic_activity=-1`）。原案把日均 Ap 填滿整個
   7 元素 aps 陣列，但 MSIS 在預設模式下只讀第 0 元素（日均 Ap），
   3 小時解析度完全沒進到模型。這造成兩個問題：
     · 暴起始與恢復期密度誤差可達 40%（日均值把陡峭轉折抹平）
     · 更嚴重的是**物理層的前視洩漏**——2024-05-10 的日均 Ap 已含入當天
       17:00 才發生的暴，用它算當天 00:00 的密度，等於把還沒發生的事算進去
   改用暴時模式後，每個時刻只用該時刻**之前**的 ap 歷史。

物理鏈：
  近圓軌道阻力衰減：  da/dt = -B · ρ · √(μa)
  偏心軌道（King-Hele）：於近地點取密度，幾何因子 geom = e^{-z}[I₀(z)+2e·I₁(z)]，z = a·e/H
  逐衛星校準：        B_eff = median( -Δa_i / s_i )    ← 中位數穩健，排除機動離群
  阻力殘差：          drag_resid = Δa + B_eff · s      ← 純衰減≈0，機動=機動量

為何要逐衛星自我校準 B_eff：TLE 的 B* 覆蓋率僅約 11%，且與真實彈道係數關係不穩定。
以中位數回歸自校準可完全繞開 B*，同時吸收固定的模型偏差（緯度、地方時近似）。
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

RE = 6378.137            # 地球赤道半徑 km
MU = 398_600.4418        # 地心重力常數 km^3/s^2
SCALE_HEIGHT_KM = 50.0   # King-Hele 幾何因子用之大氣尺度高

# 太空天氣缺值時的保守預設（平靜期典型值）
_DEFAULT_F107 = 120.0
_DEFAULT_AP = 10.0


def load_space_weather(
    store=None,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    as_of: datetime | None = None,
) -> pd.DataFrame:
    """由資料層取 MSIS 所需的驅動參數，回傳以日期為索引的表。

    欄位：f107（觀測）、f107a（81 日中心平均）、ap（日平均）。
    as_of 不為 None 時進入回放模式，只使用該時刻前已入庫的資料。
    """
    from swx_core import SwxStore

    store = store or SwxStore()
    frames = {}
    for code, col in (("F107_OBS", "f107"), ("F107_OBS_C81", "f107a"), ("AP_AVG", "ap")):
        s = store.series(code, start=start, end=end, as_of=as_of)
        if not s.empty:
            frames[col] = s.groupby(s.index.floor("D")).mean()

    # 3 小時 ap 需往前多取 3 天，才夠組出 36–57 小時前的平均項
    ap3 = store.series(
        "AP_3H",
        start=(start - pd.Timedelta(days=3)) if start is not None else None,
        end=end, as_of=as_of, observed_only=True,
    )

    if not frames:
        raise RuntimeError(
            "資料層無 F10.7／Ap 資料；請先執行 "
            "`python -m services.ingest.run --source celestrak_sw_all`"
        )

    sw = pd.DataFrame(frames).sort_index()
    sw["f107"] = sw.get("f107", pd.Series(dtype=float)).ffill().bfill()
    if "f107a" not in sw:
        sw["f107a"] = sw["f107"]
    sw["f107a"] = sw["f107a"].fillna(sw["f107"])
    if "ap" not in sw:
        sw["ap"] = _DEFAULT_AP
    sw["ap"] = sw["ap"].fillna(_DEFAULT_AP)
    sw.attrs["ap3"] = ap3[~ap3.index.duplicated(keep="last")].sort_index()
    return sw


def _sw_arrays(epochs, sw: pd.DataFrame):
    """對齊每個 epoch 的 (f107, f107a, ap_daily)，缺值以保守預設補。"""
    days = pd.DatetimeIndex(pd.to_datetime(epochs, utc=True)).floor("D")
    aligned = sw.reindex(days)
    f = aligned["f107"].to_numpy(dtype=float)
    fa = aligned["f107a"].to_numpy(dtype=float)
    a = aligned["ap"].to_numpy(dtype=float)
    f = np.where(np.isnan(f), _DEFAULT_F107, f)
    fa = np.where(np.isnan(fa), f, fa)
    a = np.where(np.isnan(a), _DEFAULT_AP, a)
    return f, fa, a


def build_ap_history(epochs, sw: pd.DataFrame) -> np.ndarray | None:
    """組出 MSIS 暴時模式所需的 7 元素 aps 陣列。

    MSIS 規格（僅在 geomagnetic_activity=-1 時生效）：
        (0) 日均 Ap
        (1) 當前 3 小時 ap
        (2) 3 小時前   (3) 6 小時前   (4) 9 小時前
        (5) 12–33 小時前 8 筆平均
        (6) 36–57 小時前 8 筆平均

    每一項都只取該時刻**之前**的值，因此不會有前視洩漏。
    無 3 小時 ap 資料時回傳 None，呼叫端退回日均模式。
    """
    ap3 = sw.attrs.get("ap3")
    if ap3 is None or len(ap3) == 0:
        return None

    epochs = pd.DatetimeIndex(pd.to_datetime(epochs, utc=True))
    f, _fa, daily = _sw_arrays(epochs, sw)

    idx = ap3.index
    values = ap3.to_numpy(dtype=float)
    out = np.empty((len(epochs), 7), dtype=float)
    out[:, 0] = daily

    hour = pd.Timedelta(hours=1)
    for col, lag in enumerate((0, 3, 6, 9), start=1):
        pos = idx.searchsorted(epochs - lag * hour, side="right") - 1
        out[:, col] = np.where(pos >= 0, values[np.clip(pos, 0, len(values) - 1)], np.nan)

    for col, (lo, hi) in enumerate(((12, 33), (36, 57)), start=5):
        left = idx.searchsorted(epochs - (hi + 3) * hour, side="right")
        right = idx.searchsorted(epochs - lo * hour, side="right")
        means = np.full(len(epochs), np.nan)
        for i, (a_i, b_i) in enumerate(zip(left, right)):
            if b_i > a_i:
                means[i] = float(np.nanmean(values[a_i:b_i]))
        out[:, col] = means

    # 任一項缺值時退回該 epoch 的日均值，避免整批失效
    for col in range(1, 7):
        out[:, col] = np.where(np.isfinite(out[:, col]), out[:, col], out[:, 0])
    return np.where(np.isfinite(out), out, _DEFAULT_AP)


def density(
    epochs,
    alt_km,
    sw: pd.DataFrame | None = None,
    *,
    lat: float = 0.0,
    lon: float = 0.0,
    as_of: datetime | None = None,
    storm_time: bool = True,
) -> np.ndarray:
    """向量化 MSIS 2.1 總質量密度 (kg/m³)。

    storm_time=True（預設）使用 3 小時 ap 歷史；無此資料時自動退回日均 Ap 模式。

    lat/lon 預設固定於 (0, 0)，是**參考點而非全球代表值**。

    對 `drag_residual()` 的 B_eff 自校準而言，固定座標造成的常數偏差會被校準吸收；
    但對交付的 `storm_ratio` **並非如此**——實測顯示評估座標會使暴時比值變動達
    約 15%（臺灣位置相對 (0,0) 約 −8%，見 tools/density_cross_check.py --coords）。
    需要對應特定任務區域時，**必須傳入該區域的實際座標**。
    """
    import pymsis

    epochs = pd.DatetimeIndex(pd.to_datetime(epochs, utc=True))
    if sw is None:
        sw = load_space_weather(start=epochs.min(), end=epochs.max(), as_of=as_of)

    dates = np.array([e.to_pydatetime().replace(tzinfo=None) for e in epochs])
    alt = np.asarray(alt_km, dtype=float)
    f, fa, a = _sw_arrays(epochs, sw)

    aps = build_ap_history(epochs, sw) if storm_time else None
    if aps is not None:
        # 暴時模式：MSIS 讀完整 7 元素 ap 歷史
        result = pymsis.calculate(
            dates, np.full(len(alt), lon), np.full(len(alt), lat), alt, f, fa, aps,
            geomagnetic_activity=-1,
        )
    else:
        # 退回日均模式：MSIS 只讀第 0 元素，其餘填同值不影響結果
        result = pymsis.calculate(
            dates, np.full(len(alt), lon), np.full(len(alt), lat), alt, f, fa,
            np.tile(a[:, None], (1, 7)),
        )
    return np.asarray(result)[..., 0].ravel()


def drag_residual(df: pd.DataFrame, sw: pd.DataFrame | None = None) -> pd.DataFrame:
    """單顆衛星的逐轉換阻力殘差。

    輸入需含 epoch、sma_km（可選 eccentricity）。
    回傳 epoch / da_km / drag_pred_da / drag_resid_da / rho / alt_km，
    並於 .attrs['B_eff'] 帶回校準後的等效彈道係數。
    """
    from scipy.special import ive

    d = df.sort_values("epoch").reset_index(drop=True)
    if len(d) < 4:
        return pd.DataFrame()

    a = d["sma_km"].to_numpy(dtype=float)
    e = (
        d["eccentricity"].to_numpy(dtype=float)
        if "eccentricity" in d.columns
        else np.zeros(len(a))
    )
    t = pd.to_datetime(d["epoch"], utc=True)
    dt_s = np.diff(t.astype("int64").to_numpy()) / 1e9
    da = np.diff(a)

    # King-Hele：阻力主要作用於近地點 → 於近地點高度取密度
    a0, e0 = a[:-1], e[:-1]
    alt_p = np.clip(a0 * (1.0 - e0) - RE, 80.0, 1000.0)   # MSIS 有效域
    rho_p = density(t.iloc[:-1], alt_p, sw)
    z = np.clip(a0 * e0 / SCALE_HEIGHT_KM, 0.0, 1e6)
    geom = ive(0, z) + 2.0 * e0 * ive(1, z)                # ive = exp(-z)·Iν，數值穩定
    s = rho_p * np.sqrt(MU * a0) * dt_s * geom             # 阻力 da 形狀項（>0）

    ok = np.isfinite(s) & (s > 0) & np.isfinite(da)
    ratios = -da[ok] / s[ok]                               # 阻力使 a 下降 → -da/s > 0
    B_eff = float(np.median(ratios[ratios > 0])) if np.any(ratios > 0) else 0.0

    pred = -B_eff * s
    out = pd.DataFrame(
        {
            "epoch": t.to_numpy()[1:],
            "da_km": da,
            "drag_pred_da": pred,
            "drag_resid_da": da - pred,
            "rho": rho_p,
            "alt_km": alt_p,
        }
    )
    out.attrs["B_eff"] = B_eff
    return out


def is_reentry_decay(df: pd.DataFrame) -> bool:
    """自然再入守門：深近地點 + 單調快速衰減 → True。

    再入的劇烈非線性衰減無法用準 secular 阻力模型消除，殘差會爆量而被誤判為機動。
    此守門讓上層能區分「自然再入」與「reboost 太空站」——後者近地點高且有週期性正跳。
    這正是構想書技術瓶頸④（地磁暴造成的衰減被誤判為機動）的處理點。
    """
    d = df.sort_values("epoch").reset_index(drop=True)
    if len(d) < 5:
        return False

    a = d["sma_km"].to_numpy(dtype=float)
    e = (
        d["eccentricity"].to_numpy(dtype=float)
        if "eccentricity" in d.columns
        else np.zeros(len(a))
    )
    rp_alt = a * (1.0 - e) - RE
    t = pd.to_datetime(d["epoch"], utc=True)
    t_days = (t - t.iloc[0]).dt.total_seconds().to_numpy() / 86400.0

    # 只看近 45 天：HEO 目標早年近地點很高，全期中位數會漏判末期俯衝
    recent = t_days >= (t_days[-1] - 45.0)
    if recent.sum() < 4:
        recent = np.ones(len(a), dtype=bool)
    rp_r, t_r = rp_alt[recent], t_days[recent]
    perigee_now = float(np.median(rp_r[-5:])) if recent.sum() >= 5 else float(rp_alt[-1])
    rate = float(np.polyfit(t_r, rp_r, 1)[0]) if recent.sum() >= 2 else 0.0  # km/day

    return bool(perigee_now < 150.0 or (perigee_now < 350.0 and rate < -2.0))


def density_ratio(
    epochs,
    alt_km: float,
    *,
    quiet_f107: float = 70.0,
    quiet_ap: float = 4.0,
    sw: pd.DataFrame | None = None,
    as_of: datetime | None = None,
    lat: float = 0.0,
    lon: float = 0.0,
) -> pd.DataFrame:
    """密度與兩種基準的比值（密度修正因子的模型側）。

    輸出兩個比值，用途不同，不可混用：
      ratio_vs_solar_min  對「太陽極小 + 地磁寧靜」的比值 —— 呈現絕對量級用
      storm_ratio         對「同一 F10.7、地磁寧靜」的比值 —— **軌道傳播修正因子**

    議題四交付的 `rho_correction` 用的是 storm_ratio。未來接上觀測反演（A9）後，
    可再與實測 ρ_obs/ρ_model 比對驗證修正因子是否合理。
    """
    epochs = pd.DatetimeIndex(pd.to_datetime(epochs, utc=True))
    if sw is None:
        sw = load_space_weather(start=epochs.min(), end=epochs.max(), as_of=as_of)

    alt = np.full(len(epochs), float(alt_km))
    rho = density(epochs, alt, sw, lat=lat, lon=lon)

    f, fa, ap = _sw_arrays(epochs, sw)

    def _calm(frame: pd.DataFrame) -> pd.DataFrame:
        """把一份驅動參數表改成地磁寧靜態。

        **日均 Ap 與 3 小時 ap 歷史都必須一起換掉**。只換日均值的話，
        暴時模式仍會讀到真實的 ap 歷史，基準與擾動態變成同一件事，
        比值恆為 1——這個錯誤會讓修正因子產品整個失效且不易察覺。
        """
        out = frame.copy()
        out["ap"] = quiet_ap
        ap3 = frame.attrs.get("ap3")
        out.attrs = dict(frame.attrs)
        if ap3 is not None and len(ap3):
            out.attrs["ap3"] = pd.Series(quiet_ap, index=ap3.index, dtype=float)
        return out

    # 基準一：深平靜期（太陽極小 + 地磁寧靜）。用途是呈現絕對量級，
    #         但它同時混入了太陽週期的影響，不適合當阻力修正因子。
    quiet_sw = pd.DataFrame(
        {"f107": quiet_f107, "f107a": quiet_f107, "ap": quiet_ap},
        index=sw.index,
    )
    quiet_sw.attrs["ap3"] = pd.Series(
        quiet_ap, index=sw.attrs.get("ap3", pd.Series(dtype=float)).index, dtype=float
    )
    rho_quiet = density(epochs, alt, quiet_sw, lat=lat, lon=lon)

    # 基準二：**同一 F10.7、地磁寧靜**。兩者相除即可分離出「地磁暴造成的密度增量」，
    #         這才是軌道傳播要套用的修正因子——否則會把太陽週期當成事件效應，
    #         在太陽極大期產生十倍以上的假修正。
    rho_calm = density(epochs, alt, _calm(sw), lat=lat, lon=lon)

    return pd.DataFrame(
        {
            "valid_time": epochs,
            "alt_km": alt,
            "rho": rho,
            "rho_quiet": rho_quiet,
            "rho_calm": rho_calm,
            "ratio_vs_solar_min": np.where(rho_quiet > 0, rho / rho_quiet, np.nan),
            "storm_ratio": np.where(rho_calm > 0, rho / rho_calm, np.nan),
            "f107": f,
            "ap": ap,
        }
    )
