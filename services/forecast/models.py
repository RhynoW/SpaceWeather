"""services.forecast.models — 預報模型分層（架構書 §8.1）。

三層策略，順序不可顛倒：

  Tier 0 基線   持續性（persistence）、氣候平均、27 日復現。
                **任何 ML 模型必須贏過這三者才准上線**——沒有這道門檻，
                很容易做出一個「看起來有 AI 但技巧為零」的系統。
  Tier 1 統計   以耦合函數與 Dst 恢復動力學為主的線性／邏輯模型，可解釋。
  Tier 2 ML     梯度提升樹。輸出**機率**而非單點值，因為地磁暴預報的
                本質不確定性高，單點值會給使用者錯誤的確定感。

外部基準：NOAA SWPC 官方 3 日預報（來源 swpc_geomag_forecast）。
它不是我們訓練的模型，但它是真正要比的對手——回測時無法取得其歷史版本
（SWPC 只發布當前一份），故只能自累積之日起前瞻比較，此限制必須在報告中載明。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .features import STORM_THRESHOLD


# ── Tier 0 基線 ─────────────────────────────────────────────────────────
class Baseline:
    """基線介面。基線不需要訓練資料以外的狀態，fit 可為空操作。"""

    name = "baseline"
    tier = 0
    #: 事件門檻。目標可換（Kp／Hp30），門檻必須跟著換，否則模型學的標籤
    #: 與擂台計分的標籤不是同一個定義——這種錯不會報錯，只會讓分數失真。
    #: None 代表該目標沒有事件定義（F10.7／Ap），此時不得產生機率。
    storm_threshold: float | None = STORM_THRESHOLD

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "Baseline":
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        raise NotImplementedError

    def predict_proba_storm(self, X: pd.DataFrame) -> np.ndarray:
        """回傳 P(目標 ≥ 門檻)。基線用經驗分布近似。"""
        if self.storm_threshold is None:
            return np.full(len(X), np.nan)
        pred = self.predict(X)
        # 以預測值與門檻的距離做 logistic 轉換；斜率由訓練殘差尺度決定
        scale = getattr(self, "_resid_scale", 1.0)
        return 1.0 / (1.0 + np.exp(-(pred - self.storm_threshold) / max(scale, 1e-6)))


class PersistenceBaseline(Baseline):
    """持續性：預測值 = 起報時刻的**目標自己**的觀測值。短 horizon 極難擊敗。

    `now_col` 必須跟著目標換。寫死 `kp_3h_now` 不會報錯——Hp30 與 F10.7 的
    特徵矩陣裡都有 kp_3h_now（Kp 是它們的輔助特徵），於是「持續性」悄悄
    變成「持續 Kp」。而 skill_vs_persistence 拿它當分母，分母錯了整欄都錯。
    """

    name = "persistence"
    now_col: str = "kp_3h_now"

    def fit(self, X, y):
        pred = self.predict(X)
        self._resid_scale = float(np.nanstd(y.to_numpy() - pred)) or 1.0
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        if self.now_col not in X.columns:
            # 安靜換一欄比棄權更糟：擂台會照常印出一個「持續性」的分數，
            # 而它持續的是另一個量。回 NaN 讓這一列在成績表上顯示 skipped。
            return np.full(len(X), np.nan)
        return X[self.now_col].to_numpy(dtype=float)


class ClimatologyBaseline(Baseline):
    """氣候平均：依年積日與時辰的長期平均。"""

    name = "climatology"

    def fit(self, X: pd.DataFrame, y: pd.Series):
        self._mean = float(np.nanmean(y))
        key = self._key(X)
        self._table = pd.Series(y.to_numpy(), index=key).groupby(level=0).mean()
        self._resid_scale = float(np.nanstd(y.to_numpy() - self.predict(X))) or 1.0
        return self

    @staticmethod
    def _key(X: pd.DataFrame) -> np.ndarray:
        month = X.index.month.to_numpy()
        hour3 = (X.index.hour.to_numpy() // 3)
        return month * 100 + hour3

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        key = self._key(X)
        return np.array([self._table.get(k, self._mean) for k in key], dtype=float)


class RecurrenceBaseline(Baseline):
    """27 日復現：預測值 = 一個太陽自轉週期前的同時刻值。"""

    name = "recurrence27d"

    def fit(self, X, y):
        pred = self.predict(X)
        ok = np.isfinite(pred)
        self._fallback = float(np.nanmean(y))
        self._resid_scale = float(np.nanstd(y.to_numpy()[ok] - pred[ok])) or 1.0
        return self

    @staticmethod
    def _recur_col(X: pd.DataFrame) -> str | None:
        """復現特徵的欄名隨目標而變（kp_recur27d／hp30_recur27d）。

        寫死欄名的後果不是報錯而是**靜默棄權**：欄位不存在 → 預測全為 NaN
        → 擂台把這個模型整個丟掉 → 成績表上少一列基線，讀者不會發現
        ML 模型少贏了一個對手。
        """
        for c in X.columns:
            if c.endswith("_recur27d"):
                return c
        return None

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        col = self._recur_col(X)
        if col is None:
            return np.full(len(X), np.nan)
        v = X[col].to_numpy(dtype=float)
        return np.where(np.isfinite(v), v, getattr(self, "_fallback", 2.0))


# ── Tier 2 機器學習 ─────────────────────────────────────────────────────
@dataclass
class GbmForecaster:
    """梯度提升樹：同時輸出 Kp 點估計與 P(Kp ≥ 5)。

    刻意選 HistGradientBoosting：原生支援缺值（太陽風特徵有大量空缺），
    不需要補值，也就不會因為補值把「沒資料」偽裝成「平靜」。
    """

    name: str = "gbm"
    tier: int = 2
    max_iter: int = 300
    learning_rate: float = 0.06
    max_depth: int | None = 6
    random_state: int = 42
    storm_threshold: float | None = STORM_THRESHOLD
    _reg: object = field(default=None, repr=False)
    _clf: object = field(default=None, repr=False)
    _columns: list[str] = field(default_factory=list, repr=False)

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "GbmForecaster":
        from sklearn.ensemble import (
            HistGradientBoostingClassifier,
            HistGradientBoostingRegressor,
        )

        self._columns = list(X.columns)
        common = dict(
            max_iter=self.max_iter,
            learning_rate=self.learning_rate,
            max_depth=self.max_depth,
            random_state=self.random_state,
            early_stopping=True,
            validation_fraction=0.15,
        )
        self._reg = HistGradientBoostingRegressor(**common).fit(X, y)

        if self.storm_threshold is None:      # 連續型目標：不做事件分類
            return self

        storm = (y >= self.storm_threshold).astype(int)
        # 事件極不平衡（Kp≥5 約佔 3%）；類別數不足時退回只做回歸。
        #
        # 刻意**不用** class_weight="balanced"：它會把少數類的機率整體膨脹，
        # 讓 Brier 技巧分數變成負值（比「永遠報氣候頻率」還差），機率產品因而不可用。
        # 正確作法是保留校準良好的機率，事件判定門檻另外選（見 verify.pick_threshold）。
        if storm.nunique() > 1 and storm.sum() >= 30:
            self._clf = HistGradientBoostingClassifier(**common).fit(X, storm)
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        return np.asarray(self._reg.predict(X[self._columns]), dtype=float)

    def predict_proba_storm(self, X: pd.DataFrame) -> np.ndarray:
        if self.storm_threshold is None:
            return np.full(len(X), np.nan)
        if self._clf is None:
            pred = self.predict(X)
            return 1.0 / (1.0 + np.exp(-(pred - self.storm_threshold)))
        return np.asarray(self._clf.predict_proba(X[self._columns])[:, 1], dtype=float)

    def feature_importance(self, X: pd.DataFrame, y: pd.Series, n_repeats: int = 5):
        """置換重要度。用來檢查模型是否真的在用太陽風特徵，還是只靠持續性。"""
        from sklearn.inspection import permutation_importance

        r = permutation_importance(
            self._reg, X[self._columns], y, n_repeats=n_repeats,
            random_state=self.random_state, scoring="neg_mean_absolute_error",
        )
        return (
            pd.DataFrame({"feature": self._columns, "importance": r.importances_mean})
            .sort_values("importance", ascending=False)
            .reset_index(drop=True)
        )


def default_models(storm_threshold: float | None = STORM_THRESHOLD,
                   *, now_col: str = "kp_3h_now") -> list:
    """回傳預設模型組合（基線在前，便於報表排序）。

    `now_col` 是持續性基線要持續的那一欄，隨目標而變（見 TargetSpec.now_col）。
    """
    persistence = PersistenceBaseline()
    persistence.now_col = now_col
    models = [
        persistence,
        ClimatologyBaseline(),
        RecurrenceBaseline(),
        GbmForecaster(storm_threshold=storm_threshold),
    ]
    for m in models[:3]:
        m.storm_threshold = storm_threshold
    return models
