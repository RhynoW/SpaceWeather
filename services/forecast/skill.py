"""services.forecast.skill — 驗證成績的讀寫（docs/forecast_skill.json）。

單獨成一個模組的理由很實際：API 與儀表板只需要「這個 horizon 的實測技巧」，
不該為此把 sklearn 與整個模型堆疊載進行程。

檔案放在 `docs/` 而非 `data/exports/` 也是刻意的——它是**宣稱的證據**，
必須與引用它的報告同進版控；`data/` 不進版控，雲端部署就讀不到。
"""

from __future__ import annotations

import json
from pathlib import Path

SKILL_PATH = Path(__file__).resolve().parents[2] / "docs" / "forecast_skill.json"

#: 成績表中要寫入 JSON 的欄位。構想書四項 KPI 在此各有對應：
#: 命中率 POD、誤警率 FAR、提前量 lead_h_med，可信度則由 horizon 決定
#: （見 run.forecast_confidence），一併存進每個 horizon 的 `confidence`。
SKILL_FIELDS = ("model", "tier", "n", "MAE", "MAE_lo", "MAE_hi", "RMSE", "thr",
                "hits", "false_alarms", "misses", "correct_neg",
                "POD", "POD_train", "FAR", "CSI", "HSS", "Brier", "BSS",
                "episodes", "ep_recall", "lead_h_med", "lead_h_mean", "lead_n",
                "skill_vs_persistence")


def load_skill(path: Path | None = None) -> dict:
    """讀回驗證成績；檔案不存在或損毀時回空 dict。

    刻意不拋例外：成績缺席時介面該說「尚未產生」，而不是整頁壞掉。
    但呼叫端**必須**處理空值，不得把缺席顯示成 0 或「無風險」。
    """
    try:
        return json.loads((path or SKILL_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_skill(target_key: str, entries: dict, meta: dict,
                path: Path | None = None) -> Path:
    """把一個目標的成績併入 JSON。

    以目標為鍵合併而非整份覆寫——跑 `--target hp30` 不該把 kp 的成績洗掉。
    """
    p = path or SKILL_PATH
    doc = load_skill(p)
    doc.setdefault("schema", 1)
    doc.setdefault("targets", {})
    doc["targets"][target_key] = {**meta, "horizons": entries}
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                 encoding="utf-8")
    return p


def skill_models(entry: dict | None) -> tuple[dict | None, dict | None]:
    """從一個 horizon 的成績表挑出 (上線模型, 最佳基線)。

    JSON 存的是**整座擂台**而非結論，因為結論會變、原始成績不會。
    但呼叫端要的是「這個 horizon 現在用的是誰、贏了誰」，故在此還原：
    上線模型＝MAE 最低者；最佳基線＝tier 0 中 MAE 最低者。
    兩者可能是同一列——那正是「ML 未勝出」的情況，不該藏起來。
    """
    models = (entry or {}).get("models") or []
    if not models:
        return None, None

    def _mae(m):
        return m.get("MAE") if m.get("MAE") is not None else float("inf")

    best = min(models, key=_mae)
    baselines = [m for m in models if (m.get("tier") or 0) == 0]
    return best, (min(baselines, key=_mae) if baselines else None)


def horizon_entry(skill: dict, target_key: str, horizon_h: float) -> dict | None:
    """查某目標某 horizon 的成績。horizon 以整數字串為鍵（'1'、'48'）。"""
    key = str(int(horizon_h)) if float(horizon_h).is_integer() else str(horizon_h)
    return ((skill.get("targets", {}) or {}).get(target_key, {})
            .get("horizons", {}) or {}).get(key)


def latest_forecast_batch(fcs):
    """只留最近一次起報的預報列。

    資料層會累積每一次 `--predict --write` 的結果，各批的起報錨點不同。
    不過濾就會把上週的 6 小時預報與今天的 1 小時預報混在同一張表上，
    還原出來的 horizon 也會出現 15.5 小時這種不存在於任何產品的值。
    以 `ingest_time` 取最新一批——同一次寫入的所有列共用同一個入庫時刻。
    """
    if fcs is None or fcs.empty or "ingest_time" not in fcs.columns:
        return fcs
    return fcs[fcs["ingest_time"] == fcs["ingest_time"].max()]
