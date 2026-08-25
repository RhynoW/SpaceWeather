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

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from swx_core import SwxStore, registry

#: 目標與特徵所用的共同時間格點（Kp 的原生解析度）
GRID_FREQ = "3h"

#: 預報 horizon（小時）。構想書要求 6 小時；本引擎延伸至 48 小時，
#: 但必須各自報告技巧分數——horizon 越長，可達到的技巧越低，不可混為一談。
HORIZONS = (3, 6, 12, 24, 48)

#: 超過此 horizon 的預報屬研究階段產出，不得用於作業決策。
#: 依驗證擂台：24 小時起測試折 BSS 轉負，48 小時由氣候平均勝出。
#: 與 API `advisory.not_for_operational_use_beyond_h` 同義，
#: 兩處一致性由 tests/test_api_contract.py 檢查。
OPERATIONAL_HORIZON_LIMIT_H = 12

TARGET = "KP_3H"
STORM_THRESHOLD = 5.0        # Kp ≥ 5 = G1 以上地磁暴


@dataclass(frozen=True)
class TargetSpec:
    """一組預報目標的時間格點與特徵尺度。

    **為什麼需要兩組目標**：構想書要求 1／3／6 小時三種產品，但 Kp 是 3 小時
    指數——以它為目標時，1 小時 horizon 只是把同一個 3 小時值換個說法，
    暴起始時刻本身就被糊掉 1–2 小時，量不出 1 小時等級的提前量。
    Hp30（Yamazaki et al. 2022）是同尺度但 30 分鐘解析的指數，
    1 小時預報必須建在它上面才有意義。

    兩組目標各自訓練、各自驗證，成績**不可互相比較**：格點不同、
    事件基率不同，MAE 與 POD 都不在同一個尺度上。
    """

    key: str                       # CLI 代稱
    code: str                      # 參數代碼（swx_observation.param_code）
    #: 事件機率的參數代碼。**不可由 code 拼出來**——Kp 的機率參數註冊為
    #: KP_STORM_PROB 而非 KP_3H_STORM_PROB，拼字串會寫進一個未註冊的參數。
    prob_code: str
    short: str                     # 特徵欄位前綴
    grid: str                      # pandas 頻率字串
    grid_h: float                  # 一格幾小時
    horizons: tuple[int, ...]
    storm_threshold: float
    lag_hours: tuple[float, ...]
    roll_hours: tuple[float, ...]
    recurrence_days: tuple[int, ...]
    geomag_cols: tuple[str, ...]   # 納入滯後／滾動統計的地磁欄位
    ffill_hours: float             # 對齊格點時，一個值最多沿用多久
    #: 慢於格點的參數是否放寬到自己的更新週期沿用。
    #: Kp 目標刻意維持 False——既有驗證報告的數字在該行為下產生，
    #: 改了就不再可複現；細格點的 Hp30 目標則非開不可。
    cadence_aware_ffill: bool
    label: str

    @property
    def target_col(self) -> str:
        return self.code.lower()

    def steps(self, hours: float) -> int:
        """把小時換算為格數（至少 1 格）。"""
        return max(1, int(round(hours / self.grid_h)))


def _h(hours: float) -> str:
    """特徵名稱裡的時數：整數不留小數點，否則 3.0h 與 3h 會變成兩個欄位。"""
    return f"{hours:g}"


#: Kp 目標（3 小時格點）。這組的欄位名稱與參數與既有驗證報告一致，不得更動，
#: 否則 docs/forecast_verification.md 的數字失去可複現性。
KP_TARGET = TargetSpec(
    key="kp", code="KP_3H", prob_code="KP_STORM_PROB",
    short="kp", grid="3h", grid_h=3.0,
    horizons=(3, 6, 12, 24, 48), storm_threshold=5.0,
    lag_hours=(3, 6, 9, 12, 24, 48),
    roll_hours=(24, 72, 168),
    recurrence_days=(27, 54),
    geomag_cols=("kp_3h", "ap_3h", "dst"),
    ffill_hours=6.0,
    cadence_aware_ffill=False,
    label="Kp（3 小時指數）",
)

#: Hp30 目標（30 分鐘格點）。構想書的 1 小時產品建在這組上。
#: 事件門檻沿用 5.0——Hp30 與 Kp 同尺度，但**不是同一個量**：
#: Hp30 上界不封頂，暴時峰值可高於 Kp，故兩組的 POD/FAR 不可橫向比較。
HP30_TARGET = TargetSpec(
    key="hp30", code="HP30", prob_code="HP30_STORM_PROB",
    short="hp30", grid="30min", grid_h=0.5,
    horizons=(1, 3, 6), storm_threshold=5.0,
    lag_hours=(0.5, 1, 1.5, 2, 3, 6, 12, 24),
    roll_hours=(3, 24, 72),
    recurrence_days=(27,),
    geomag_cols=("hp30", "ap30", "kp_3h", "dst"),
    ffill_hours=3.0,
    cadence_aware_ffill=True,
    label="Hp30（30 分鐘指數）",
)

TARGETS = {t.key: t for t in (KP_TARGET, HP30_TARGET)}



#: 兩組目標共用的輸入參數。缺哪一個就少哪一群特徵，不會使整個面板失敗。
_BASE_PARAMS = ("KP_3H", "AP_3H", "DST", "F107_OBS", "SW_V", "SW_N", "IMF_BZ",
                "M_FLARE_PROB", "X_FLARE_PROB")

_INGEST_HINT = {
    "KP_3H": "python -m services.ingest.run --source omni2_hourly",
    "HP30": ("python -m services.ingest.run --source gfz_hp30 "
             "--reparse --window-days 2100 --backfill"),
}


def load_panel(
    store: SwxStore,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    as_of: datetime | None = None,
    spec: TargetSpec = KP_TARGET,
) -> pd.DataFrame:
    """把各參數拉齊到目標的時間格點，回傳寬表。"""
    wanted = list(dict.fromkeys((spec.code, *_BASE_PARAMS, *(c.upper() for c in spec.geomag_cols))))
    frames: dict[str, pd.Series] = {}
    for code in wanted:
        s = store.series(code, start=start, end=end, as_of=as_of, observed_only=True)
        if s.empty:
            continue
        s = s[~s.index.duplicated(keep="last")].sort_index()
        frames[code.lower()] = s

    if spec.target_col not in frames:
        hint = _INGEST_HINT.get(spec.code, "python -m services.ingest.run --source all")
        raise RuntimeError(f"缺少目標變數 {spec.code}；請先執行 `{hint}`")

    grid = pd.date_range(
        min(s.index.min() for s in frames.values()),
        max(s.index.max() for s in frames.values()),
        freq=spec.grid,
        tz="UTC",
    )
    reg = registry()
    base_limit = spec.steps(spec.ffill_hours)
    panel = pd.DataFrame(index=grid)
    for name, s in frames.items():
        # 高於格點頻率的來源先在格內取平均，再對齊格點；ffill 距離有上限，
        # 避免用很舊的值假裝成當前狀態。
        limit = base_limit
        if spec.cadence_aware_ffill:
            # 比格點慢的參數要放寬到它自己的更新週期，否則格點越細、空洞越多：
            # Kp 是 3 小時值，在 30 分鐘格點上只沿用 6 格的話，六格裡有五格是空的，
            # build_dataset 的「半數特徵須非空」遮罩會把大部分樣本丟掉。
            param = reg.get(name.upper())
            cadence_s = getattr(param, "cadence_s", None) if param else None
            if cadence_s:
                limit = max(limit, spec.steps(cadence_s / 3600.0))
        panel[name] = s.resample(spec.grid).mean().reindex(grid).ffill(limit=limit)
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


def build_features(panel: pd.DataFrame, spec: TargetSpec = KP_TARGET) -> pd.DataFrame:
    """由寬表產生特徵矩陣。每一欄都只用 t 以前的資料。"""
    f = pd.DataFrame(index=panel.index)
    step = spec.steps

    # ── 地磁狀態 ────────────────────────────────────────────────────────
    for col in spec.geomag_cols:
        if col not in panel:
            continue
        s = panel[col]
        f[f"{col}_now"] = s
        for lag_h in spec.lag_hours:
            f[f"{col}_lag{_h(lag_h)}h"] = s.shift(step(lag_h))
        for win_h in spec.roll_hours:
            win = step(win_h)
            f[f"{col}_max{_h(win_h)}h"] = s.rolling(win, min_periods=2).max()
            f[f"{col}_mean{_h(win_h)}h"] = s.rolling(win, min_periods=2).mean()
        f[f"{col}_trend24h"] = s - s.shift(step(24))

    # Dst 恢復相判別：目前值相對過去 3 天最低點的回升幅度
    if "dst" in panel:
        dst_min = panel["dst"].rolling(step(72), min_periods=2).min()
        f["dst_recovery"] = panel["dst"] - dst_min
        f["dst_min72h"] = dst_min

    # ── 太陽風耦合 ──────────────────────────────────────────────────────
    if {"sw_v", "imf_bz"} <= set(panel.columns):
        f["sw_v_now"] = panel["sw_v"]
        f["imf_bz_now"] = panel["imf_bz"]
        f["imf_bz_min24h"] = panel["imf_bz"].rolling(step(24), min_periods=2).min()
        f["sw_v_max24h"] = panel["sw_v"].rolling(step(24), min_periods=2).max()
        if "sw_n" in panel:
            f["sw_pressure"] = 1.6726e-6 * panel["sw_n"] * panel["sw_v"] ** 2
        coupling = newell_coupling(panel["sw_v"], panel["imf_bz"])
        f["newell_now"] = coupling
        f["newell_mean24h"] = coupling.rolling(step(24), min_periods=2).mean()
        # L1 到地球約 30–60 分鐘。在 3 小時格點上這段延遲被格點本身吃掉，
        # 30 分鐘格點才看得見它——1 小時 horizon 的訊號主要來自這裡。
        if spec.grid_h <= 1.0:
            f["imf_bz_lag1h"] = panel["imf_bz"].shift(step(1))
            f["newell_lag1h"] = coupling.shift(step(1))
            f["newell_max3h"] = coupling.rolling(step(3), min_periods=2).max()

    # ── 27 日復現（太陽自轉）──────────────────────────────────────────
    # 27 天 = 216 個 3 小時格點。冕洞高速流會週期性回來，這是長 horizon
    # 少數有物理依據的訊號。
    tgt = spec.target_col
    if tgt in panel:
        for days in spec.recurrence_days:
            shifted = panel[tgt].shift(step(days * 24))
            f[f"{spec.short}_recur{days}d"] = shifted
            if days == spec.recurrence_days[0]:
                f[f"{spec.short}_recur{days}d_max24h"] = shifted.rolling(
                    step(24), min_periods=2).max()

    # ── 太陽活動與時間 ──────────────────────────────────────────────────
    if "f107_obs" in panel:
        f["f107"] = panel["f107_obs"]
        f["f107_mean81d"] = panel["f107_obs"].rolling(step(81 * 24), min_periods=30).mean()
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
    spec: TargetSpec = KP_TARGET,
) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """回傳 (X, y, y_storm)，索引為**起報時刻 t**（目標值在 t+horizon）。"""
    f = features if features is not None else build_features(panel, spec)
    y = panel[spec.target_col].shift(-spec.steps(horizon_h))
    y_storm = (y >= spec.storm_threshold).astype(int)

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
