"""services.forecast.features — 預報特徵庫（架構書 §8）。

**無前視偏差是這個模組唯一不能妥協的性質**：時刻 t 的特徵只能用 t 以前的資料。
所有滯後與滾動統計都以 `shift`／`rolling` 在時間軸上向後看，
產生的每一列都可以誠實宣稱「這是當時就能算出來的」。

特徵分四群：

  地磁狀態    Kp/ap/Dst 的滯後值與滾動統計。地磁暴有明顯的持續性與恢復動力學，
              這是短 horizon（3–12 小時）最強的訊號。
  太陽風耦合  V、N、Bz 與 Newell 耦合函數 dΦ/dt。物理上這是磁層能量輸入率，
              但 L1 到地球只有約 30–60 分鐘傳播時間，**對 24 小時以上幾乎無預測力**。
  27 日復現   太陽自轉週期。冕洞高速流會週期性回來，這是長 horizon 少數有物理
              依據的訊號，也是 48 小時預報唯一勝過氣候平均的來源之一。
  時間與太陽  年積日（半年期效應：春秋分地磁活動偏強）、時辰、F10.7、活動區機率。
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from swx_core import SwxStore

#: 目標與特徵所用的共同時間格點（Kp 的原生解析度）
GRID_FREQ = "3h"

#: 預報 horizon（小時）。構想書要求 6 小時；本引擎延伸至 48 小時，
#: 但必須各自報告技巧分數——horizon 越長，可達到的技巧越低，不可混為一談。
HORIZONS = (3, 6, 12, 24, 48)

TARGET = "KP_3H"
STORM_THRESHOLD = 5.0        # Kp ≥ 5 = G1 以上地磁暴

_LAG_STEPS = (1, 2, 3, 4, 8, 16)          # ×3 小時 = 3, 6, 9, 12, 24, 48 小時前
_ROLL_WINDOWS = (8, 24, 56)               # ×3 小時 = 1 天、3 天、7 天


def load_panel(
    store: SwxStore,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    as_of: datetime | None = None,
) -> pd.DataFrame:
    """把各參數拉齊到共同時間格點，回傳寬表。"""
    wanted = ["KP_3H", "AP_3H", "DST", "F107_OBS", "SW_V", "SW_N", "IMF_BZ",
              "M_FLARE_PROB", "X_FLARE_PROB"]
    frames: dict[str, pd.Series] = {}
    for code in wanted:
        s = store.series(code, start=start, end=end, as_of=as_of, observed_only=True)
        if s.empty:
            continue
        s = s[~s.index.duplicated(keep="last")].sort_index()
        frames[code.lower()] = s

    if TARGET.lower() not in frames:
        raise RuntimeError(
            f"缺少目標變數 {TARGET}；請先執行 "
            "`python -m services.ingest.run --source omni2_hourly`"
        )

    grid = pd.date_range(
        min(s.index.min() for s in frames.values()),
        max(s.index.max() for s in frames.values()),
        freq=GRID_FREQ,
        tz="UTC",
    )
    panel = pd.DataFrame(index=grid)
    for name, s in frames.items():
        # 逐時來源先在 3 小時窗內取平均，再對齊格點；限制 ffill 距離避免
        # 用很舊的值假裝成當前狀態
        resampled = s.resample(GRID_FREQ).mean()
        panel[name] = resampled.reindex(grid).ffill(limit=2)
    return panel


def newell_coupling(v: pd.Series, bz: pd.Series, by: pd.Series | None = None) -> pd.Series:
    """Newell 耦合函數 dΦ/dt ∝ V^{4/3} · B_T^{2/3} · sin^{8/3}(θ_c/2)。

    缺 By 時以 |Bz| 近似 B_T，時鐘角取南向分量為主——這是簡化，
    在 Bz 為主導的地磁暴期間可接受，平靜期會低估。
    """
    bt = (bz.abs() if by is None else np.hypot(by, bz)).clip(lower=1e-6)
    theta = np.arctan2(by.abs() if by is not None else 0.0, -bz)
    theta = pd.Series(np.where(bz < 0, np.pi - theta.abs() if by is not None else np.pi, 0.3),
                      index=bz.index)
    return (v.clip(lower=1) ** (4 / 3)) * (bt ** (2 / 3)) * (np.sin(theta / 2).abs() ** (8 / 3))


def build_features(panel: pd.DataFrame) -> pd.DataFrame:
    """由寬表產生特徵矩陣。每一欄都只用 t 以前的資料。"""
    f = pd.DataFrame(index=panel.index)

    # ── 地磁狀態 ────────────────────────────────────────────────────────
    for col in ("kp_3h", "ap_3h", "dst"):
        if col not in panel:
            continue
        s = panel[col]
        f[f"{col}_now"] = s
        for lag in _LAG_STEPS:
            f[f"{col}_lag{lag * 3}h"] = s.shift(lag)
        for win in _ROLL_WINDOWS:
            f[f"{col}_max{win * 3}h"] = s.rolling(win, min_periods=2).max()
            f[f"{col}_mean{win * 3}h"] = s.rolling(win, min_periods=2).mean()
        f[f"{col}_trend24h"] = s - s.shift(8)

    # Dst 恢復相判別：目前值相對過去 3 天最低點的回升幅度
    if "dst" in panel:
        dst_min = panel["dst"].rolling(24, min_periods=2).min()
        f["dst_recovery"] = panel["dst"] - dst_min
        f["dst_min72h"] = dst_min

    # ── 太陽風耦合 ──────────────────────────────────────────────────────
    if {"sw_v", "imf_bz"} <= set(panel.columns):
        f["sw_v_now"] = panel["sw_v"]
        f["imf_bz_now"] = panel["imf_bz"]
        f["imf_bz_min24h"] = panel["imf_bz"].rolling(8, min_periods=2).min()
        f["sw_v_max24h"] = panel["sw_v"].rolling(8, min_periods=2).max()
        if "sw_n" in panel:
            f["sw_pressure"] = 1.6726e-6 * panel["sw_n"] * panel["sw_v"] ** 2
        coupling = newell_coupling(panel["sw_v"], panel["imf_bz"])
        f["newell_now"] = coupling
        f["newell_mean24h"] = coupling.rolling(8, min_periods=2).mean()

    # ── 27 日復現（太陽自轉）──────────────────────────────────────────
    # 27 天 = 216 個 3 小時格點。冕洞高速流會週期性回來，這是長 horizon
    # 少數有物理依據的訊號。
    if "kp_3h" in panel:
        f["kp_recur27d"] = panel["kp_3h"].shift(216)
        f["kp_recur27d_max24h"] = panel["kp_3h"].shift(216).rolling(8, min_periods=2).max()
        f["kp_recur54d"] = panel["kp_3h"].shift(432)

    # ── 太陽活動與時間 ──────────────────────────────────────────────────
    if "f107_obs" in panel:
        f["f107"] = panel["f107_obs"]
        f["f107_mean81d"] = panel["f107_obs"].rolling(648, min_periods=30).mean()
    for col in ("m_flare_prob", "x_flare_prob"):
        if col in panel:
            f[col] = panel[col]

    doy = panel.index.dayofyear.to_numpy()
    hour = panel.index.hour.to_numpy()
    f["doy_sin"] = np.sin(2 * np.pi * doy / 365.25)
    f["doy_cos"] = np.cos(2 * np.pi * doy / 365.25)
    # 半年期：春秋分附近地磁活動偏強（Russell-McPherron 效應）
    f["semiannual"] = np.sin(4 * np.pi * doy / 365.25)
    f["hour_sin"] = np.sin(2 * np.pi * hour / 24)
    f["hour_cos"] = np.cos(2 * np.pi * hour / 24)

    return f


def build_dataset(
    panel: pd.DataFrame,
    horizon_h: int,
    *,
    features: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """回傳 (X, y, y_storm)，索引為**起報時刻 t**（目標值在 t+horizon）。"""
    f = features if features is not None else build_features(panel)
    steps = horizon_h // 3
    y = panel[TARGET.lower()].shift(-steps)
    y_storm = (y >= STORM_THRESHOLD).astype(int)

    mask = y.notna() & f.notna().sum(axis=1).gt(len(f.columns) * 0.5)
    return f[mask], y[mask], y_storm[mask]


def feature_coverage(f: pd.DataFrame) -> pd.DataFrame:
    """各特徵的非空覆蓋率。

    這張表要放進驗證報告：若太陽風特徵覆蓋率低，模型實際上沒在用物理耦合，
    宣稱「以太陽風驅動預報」就是不誠實。
    """
    return (
        pd.DataFrame(
            {
                "feature": f.columns,
                "coverage": f.notna().mean().to_numpy(),
                "n_valid": f.notna().sum().to_numpy(),
            }
        )
        .sort_values("coverage")
        .reset_index(drop=True)
    )
