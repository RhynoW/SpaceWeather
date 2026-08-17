"""services.forecast.verify — 預報驗證擂台（架構書 §8.2）。

方法論移植自 Sat_TraingDataExtension/three_layer_common_eval.py：
**同一測試集、同一真值、同一操作點**，所有模型放在同一個擂台上比。
該案當初寫這支程式，正是為了回應「各層各用不同測試集、數值不可橫向比較」的
審查意見；本案面對的是完全同型的問題（基線 vs 統計 vs ML 預報）。

三個不可妥協的設計：

  滾動起報（rolling origin）  訓練集永遠在測試集之前，且中間留 gap，
                              避免同一場地磁暴同時出現在訓練與測試。
  相對基線的技巧分數          單看 MAE 沒有意義；要看「比持續性好多少」。
  信賴區間                    以 bootstrap 給區間，不報單一數字——
                              構想書 TRL 表已載明不宜承諾絕對準確。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .features import STORM_THRESHOLD


@dataclass
class ContingencyTable:
    """事件預報的 2×2 列聯表。"""

    hits: int          # 預報有、實際有
    false_alarms: int  # 預報有、實際無
    misses: int        # 預報無、實際有
    correct_neg: int   # 預報無、實際無

    @property
    def pod(self) -> float:
        """命中率（probability of detection）。"""
        d = self.hits + self.misses
        return self.hits / d if d else np.nan

    @property
    def far(self) -> float:
        """誤警率（false alarm ratio）。"""
        d = self.hits + self.false_alarms
        return self.false_alarms / d if d else np.nan

    @property
    def csi(self) -> float:
        """臨界成功指數。"""
        d = self.hits + self.false_alarms + self.misses
        return self.hits / d if d else np.nan

    @property
    def hss(self) -> float:
        """Heidke 技巧分數：相對隨機猜測的改善，0 = 無技巧。"""
        a, b, c, d = self.hits, self.false_alarms, self.misses, self.correct_neg
        n = a + b + c + d
        if n == 0:
            return np.nan
        expected = ((a + b) * (a + c) + (c + d) * (b + d)) / n
        denom = n - expected
        return (a + d - expected) / denom if denom else np.nan

    @property
    def pofd(self) -> float:
        """假警率（probability of false detection）＝ 誤報 ÷ 實際無事件數。

        與 FAR 不同：FAR 的分母是「所有發報數」，POFD 的分母是「實際無事件數」。
        兩者常被混用，但 TSS 要的是 POFD。
        """
        d = self.false_alarms + self.correct_neg
        return self.false_alarms / d if d else np.nan

    @property
    def tss(self) -> float:
        """True Skill Statistic（Peirce 技巧分數）＝ POD − POFD。

        稀有事件評估的社群標準指標。相對 HSS 的優勢在於**對事件基率不敏感**：
        本系統事件基率僅約 3%，correct_neg 高達一萬餘筆，HSS 會被這批
        「猜沒事就對」的樣本稀釋，TSS 不會。0 = 無技巧，1 = 完美。
        """
        return self.pod - self.pofd

    @property
    def balanced_accuracy(self) -> float:
        """平衡準確率＝（命中率 + 正確否定率）÷ 2，同樣不受基率灌水。"""
        return (self.pod + (1.0 - self.pofd)) / 2.0

    def to_dict(self) -> dict:
        return {
            "hits": self.hits, "false_alarms": self.false_alarms,
            "misses": self.misses, "correct_neg": self.correct_neg,
            "POD": round(self.pod, 3), "FAR": round(self.far, 3),
            "POFD": round(self.pofd, 4),
            "CSI": round(self.csi, 3), "HSS": round(self.hss, 3),
            "TSS": round(self.tss, 3), "BACC": round(self.balanced_accuracy, 3),
        }


def contingency(y_true_event: np.ndarray, y_pred_event: np.ndarray) -> ContingencyTable:
    t = np.asarray(y_true_event).astype(bool)
    p = np.asarray(y_pred_event).astype(bool)
    return ContingencyTable(
        hits=int(np.sum(t & p)),
        false_alarms=int(np.sum(~t & p)),
        misses=int(np.sum(t & ~p)),
        correct_neg=int(np.sum(~t & ~p)),
    )


def brier_score(y_true_event: np.ndarray, prob: np.ndarray) -> float:
    ok = np.isfinite(prob)
    if not ok.any():
        return np.nan
    return float(np.mean((prob[ok] - np.asarray(y_true_event)[ok]) ** 2))


def brier_skill_score(y_true_event: np.ndarray, prob: np.ndarray) -> float:
    """相對氣候機率的 Brier 技巧分數。0 以下代表比「永遠報氣候頻率」還差。"""
    y = np.asarray(y_true_event, dtype=float)
    climate = float(np.mean(y))
    bs = brier_score(y, prob)
    bs_ref = float(np.mean((climate - y) ** 2))
    return float(1 - bs / bs_ref) if bs_ref > 0 else np.nan


def bootstrap_ci(values: np.ndarray, stat=np.mean, n: int = 1000,
                 alpha: float = 0.05, seed: int = 42) -> tuple[float, float]:
    """bootstrap 信賴區間（移植自該案 bootstrap_ci.py 的用法）。"""
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    if len(v) < 3:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    draws = [stat(rng.choice(v, size=len(v), replace=True)) for _ in range(n)]
    return (float(np.percentile(draws, 100 * alpha / 2)),
            float(np.percentile(draws, 100 * (1 - alpha / 2))))


def rolling_origin_splits(
    index: pd.DatetimeIndex,
    *,
    n_splits: int = 5,
    min_train_days: int = 365,
    gap_days: int = 7,
):
    """滾動起報切分：訓練集永遠在測試集之前，中間留 gap。

    gap 的用意：地磁暴會持續數天，若訓練集結束後立刻接測試集，
    同一場事件的前半段在訓練、後半段在測試，模型等於偷看答案。
    """
    index = pd.DatetimeIndex(index).sort_values()
    start, end = index.min(), index.max()
    total_days = (end - start).days
    if total_days < min_train_days + gap_days + 30:
        raise ValueError(
            f"資料期間僅 {total_days} 天，不足以做 {n_splits} 折滾動驗證"
            f"（至少需 {min_train_days + gap_days + 30} 天）"
        )

    test_days = max(30, (total_days - min_train_days - gap_days) // n_splits)
    for k in range(n_splits):
        train_end = start + pd.Timedelta(days=min_train_days + k * test_days)
        test_start = train_end + pd.Timedelta(days=gap_days)
        test_end = test_start + pd.Timedelta(days=test_days)
        if test_start >= end:
            break
        yield (index <= train_end), ((index > test_start) & (index <= min(test_end, end)))


def pick_threshold(y_event: np.ndarray, prob: np.ndarray,
                   objective: str = "csi", target_pod: float = 0.7) -> float:
    """在**訓練折**上選事件判定門檻。

    為什麼不固定用 0.5：事件基率只有約 3%，校準良好的機率幾乎不會超過 0.5，
    用 0.5 會讓 POD 趨近 0。門檻必須依基率選，且只能用訓練資料選——
    在測試折上挑門檻等於偷看答案。

    **操作點是政策決定，不是技術決定**：
      csi     整體命中與誤警的平衡（預設）
      hss     相對隨機的技巧最大化，較 CSI 平衡
      pod     滿足目標 POD 的前提下讓 FAR 最小——預警系統通常要這個，
              代價是誤警增加。構想書訂 POD≥0.7、FAR≤0.4，屬此類。
    此選擇應由需求單位在門檻校準工作坊決定，工具只負責把代價攤開。
    """
    grid = np.arange(0.02, 0.85, 0.01)
    rows = [(float(thr), contingency(y_event, prob >= thr)) for thr in grid]

    if objective == "pod":
        feasible = [(thr, ct) for thr, ct in rows
                    if np.isfinite(ct.pod) and ct.pod >= target_pod]
        if feasible:
            return min(feasible, key=lambda r: (r[1].far if np.isfinite(r[1].far) else 1.0))[0]
        return max(rows, key=lambda r: r[1].pod if np.isfinite(r[1].pod) else -1)[0]

    key = (lambda ct: ct.hss) if objective == "hss" else (lambda ct: ct.csi)
    return max(rows, key=lambda r: key(r[1]) if np.isfinite(key(r[1])) else -1)[0]


def evaluate(
    models: list,
    X: pd.DataFrame,
    y: pd.Series,
    *,
    n_splits: int = 5,
    min_train_days: int = 365,
    gap_days: int = 7,
    prob_threshold: float | None = None,
    objective: str = "csi",
) -> pd.DataFrame:
    """同一擂台評估所有模型，回傳每個模型一列的成績表。"""
    y_event = (y >= STORM_THRESHOLD).astype(int)
    results: dict[str, dict[str, list]] = {}

    for train_mask, test_mask in rolling_origin_splits(
        X.index, n_splits=n_splits, min_train_days=min_train_days, gap_days=gap_days
    ):
        Xtr, ytr = X[train_mask], y[train_mask]
        Xte, yte = X[test_mask], y[test_mask]
        if len(Xtr) < 100 or len(Xte) < 30 or yte.nunique() < 2:
            continue

        for model in models:
            try:
                fitted = model.fit(Xtr, ytr)
                pred = np.asarray(fitted.predict(Xte), dtype=float)
                prob = np.asarray(fitted.predict_proba_storm(Xte), dtype=float)
                if prob_threshold is None:
                    prob_tr = np.asarray(fitted.predict_proba_storm(Xtr), dtype=float)
                    ok_tr = np.isfinite(prob_tr)
                    ev_tr = (ytr.to_numpy()[ok_tr] >= STORM_THRESHOLD).astype(int)
                    thr = pick_threshold(ev_tr, prob_tr[ok_tr], objective=objective)
                    pod_tr = contingency(ev_tr, prob_tr[ok_tr] >= thr).pod
                else:
                    thr = prob_threshold
                    pod_tr = float("nan")
            except Exception as exc:  # noqa: BLE001 - 單一模型失敗不應中斷整場評估
                results.setdefault(model.name, {}).setdefault("errors", []).append(str(exc))
                continue

            ok = np.isfinite(pred)
            if ok.sum() < 10:
                continue
            truth = yte.to_numpy()[ok]
            bucket = results.setdefault(model.name, {})
            bucket.setdefault("abs_err", []).extend(np.abs(pred[ok] - truth).tolist())
            bucket.setdefault("sq_err", []).extend(((pred[ok] - truth) ** 2).tolist())
            bucket.setdefault("y_event", []).extend(
                (truth >= STORM_THRESHOLD).astype(int).tolist()
            )
            bucket.setdefault("prob", []).extend(prob[ok].tolist())
            bucket.setdefault("pred_event", []).extend(
                (prob[ok] >= thr).astype(int).tolist()
            )
            bucket.setdefault("thr", []).append(thr)
            bucket.setdefault("pod_train", []).append(pod_tr)
            bucket.setdefault("tier", []).append(getattr(model, "tier", 9))

    rows = []
    for name, b in results.items():
        if "abs_err" not in b:
            rows.append({"model": name, "status": "failed",
                         "note": (b.get("errors") or ["unknown"])[0][:80]})
            continue
        abs_err = np.array(b["abs_err"])
        ct = contingency(np.array(b["y_event"]), np.array(b["pred_event"]))
        lo, hi = bootstrap_ci(abs_err)
        rows.append(
            {
                "model": name,
                "tier": int(np.median(b["tier"])),
                "n": len(abs_err),
                "MAE": round(float(np.mean(abs_err)), 3),
                "MAE_lo": round(lo, 3),
                "MAE_hi": round(hi, 3),
                "RMSE": round(float(np.sqrt(np.mean(b["sq_err"]))), 3),
                **ct.to_dict(),
                "thr": round(float(np.median(b.get("thr", [0.5]))), 2),
                "POD_train": round(float(np.nanmedian(b.get("pod_train", [np.nan]))), 3),
                "Brier": round(brier_score(np.array(b["y_event"]), np.array(b["prob"])), 4),
                "BSS": round(brier_skill_score(np.array(b["y_event"]), np.array(b["prob"])), 3),
                "status": "ok",
            }
        )

    df = pd.DataFrame(rows)
    if df.empty or "MAE" not in df.columns:
        return df

    # 相對持續性的技巧分數：單看 MAE 無意義，要看比基線好多少
    ref = df.loc[df["model"] == "persistence", "MAE"]
    if not ref.empty and ref.iloc[0] > 0:
        df["skill_vs_persistence"] = (1 - df["MAE"] / ref.iloc[0]).round(3)
    return df.sort_values(["tier", "MAE"]).reset_index(drop=True)


def transfer_warning(table: pd.DataFrame, objective: str, target_pod: float = 0.7) -> str | None:
    """操作點目標在訓練折達成、但測試折未達成時提出警告。

    這是本引擎最容易被誤讀的地方：`--objective pod` 的名稱容易讓人以為
    產出的 POD 就會 ≥ 0.7。實際上門檻只能在訓練折上選，機率分布在折間漂移，
    測試折的 POD 可能遠低於目標。不明講就是誤導。
    """
    if objective != "pod" or table.empty or "POD" not in table.columns:
        return None
    ok = table[(table["status"] == "ok") & (table["tier"] == 2)]
    if ok.empty:
        return None
    row = ok.iloc[0]
    pod_te, pod_tr = row.get("POD"), row.get("POD_train")
    if pd.notna(pod_te) and pod_te < target_pod:
        return (
            f"⚠ 操作點目標 POD ≥ {target_pod} 在**訓練折**達成"
            f"（POD_train {pod_tr}），但**測試折**僅 {pod_te}。"
            "機率分布在折間漂移，目標型操作點無外推能力——"
            "不可依訓練折的達標宣稱本引擎滿足該 KPI。"
        )
    return None


def verdict(table: pd.DataFrame) -> str:
    """一句話結論：ML 是否真的贏過基線。"""
    if table.empty or "skill_vs_persistence" not in table.columns:
        return "無足夠資料可評估。"
    ok = table[table["status"] == "ok"]
    if ok.empty:
        return "所有模型皆評估失敗。"
    best = ok.loc[ok["MAE"].idxmin()]
    baselines = ok[ok["tier"] == 0]
    best_baseline = baselines.loc[baselines["MAE"].idxmin()] if not baselines.empty else None

    if best_baseline is None:
        return f"最佳模型 {best['model']}（MAE {best['MAE']}），無基線可比。"
    if best["model"] == best_baseline["model"]:
        return (
            f"**基線勝出**：{best_baseline['model']} MAE {best_baseline['MAE']}，"
            "ML 模型未能超越。依 Tier 0 門檻，不應上線。"
        )
    gain = 1 - best["MAE"] / best_baseline["MAE"]
    return (
        f"{best['model']} MAE {best['MAE']}（95% CI {best['MAE_lo']}–{best['MAE_hi']}），"
        f"優於最佳基線 {best_baseline['model']}（{best_baseline['MAE']}）{gain:.1%}；"
        f"POD {best['POD']}、FAR {best['FAR']}、BSS {best['BSS']}。"
    )
