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

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "Baseline":
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        raise NotImplementedError

    def predict_proba_storm(self, X: pd.DataFrame) -> np.ndarray:
        """回傳 P(Kp ≥ 5)。基線用經驗分布近似。"""
        pred = self.predict(X)
        # 以預測值與門檻的距離做 logistic 轉換；斜率由訓練殘差尺度決定
        scale = getattr(self, "_resid_scale", 1.0)
        return 1.0 / (1.0 + np.exp(-(pred - STORM_THRESHOLD) / max(scale, 1e-6)))


class PersistenceBaseline(Baseline):
    """持續性：預測值 = 起報時刻的觀測值。短 horizon 極難擊敗。"""

    name = "persistence"

    def fit(self, X, y):
        pred = self.predict(X)
        self._resid_scale = float(np.nanstd(y.to_numpy() - pred)) or 1.0
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        return X["kp_3h_now"].to_numpy(dtype=float)


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

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        if "kp_recur27d" not in X.columns:
            return np.full(len(X), np.nan)
        v = X["kp_recur27d"].to_numpy(dtype=float)
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

        storm = (y >= STORM_THRESHOLD).astype(int)
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
        if self._clf is None:
            pred = self.predict(X)
            return 1.0 / (1.0 + np.exp(-(pred - STORM_THRESHOLD)))
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


def default_models() -> list:
    """回傳預設模型組合（基線在前，便於報表排序）。"""
    return [
        PersistenceBaseline(),
        ClimatologyBaseline(),
        RecurrenceBaseline(),
        GbmForecaster(),
    ]
