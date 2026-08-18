"""swx_core.config — 專案路徑與來源設定載入。

沿用 Sat_TraingDataExtension/backend_duckdb_v2.py 的 `Settings.from_env` 模式：
所有路徑可用環境變數覆寫，預設值適用於直接 clone 後即可執行。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml


def project_root() -> Path:
    """專案根目錄（本檔位於 <root>/packages/swx_core/config.py）。"""
    env = os.getenv("SWX_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[2]


def config_dir() -> Path:
    return Path(os.getenv("SWX_CONFIG_DIR", project_root() / "configs"))


def data_dir() -> Path:
    """資料根目錄。

    解析順序：
      1. `SWX_DATA_DIR` 明示指定
      2. `data/`——若其中已有觀測分區，直接用
      3. `data/demo/`——**僅當 data/ 沒有觀測分區時**才退回

    第 3 條是為雲端展示（Streamlit Cloud 等）而設：那裡是全新 clone，
    執行時資料尚未擷取，若不退回示範快照，整個介面會是空白。
    刻意設計成「有真資料就絕不用示範快照」，避免本機開發時
    悄悄讀到過期的快照卻沒發現。
    """
    explicit = os.getenv("SWX_DATA_DIR")
    if explicit:
        d = Path(explicit)
        d.mkdir(parents=True, exist_ok=True)
        return d

    d = project_root() / "data"
    live = d / "swx_parquet"
    if not (live.is_dir() and any(live.iterdir())):
        demo = d / "demo"
        if (demo / "swx_parquet").is_dir():
            return demo
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_origin() -> dict:
    """目前資料來源的性質。展示層據此決定是否顯示 DEMO 橫幅。

    僅顯示資料齡期不足以防止誤讀：示範快照裡的資料**在其自身時間軸上是新的**，
    齡期看起來可能很正常，使用者仍會誤以為是即時作業資料。
    因此把「這是不是示範資料」做成明確欄位，而不是讓人從齡期推論。
    """
    root = data_dir()
    is_demo = root.name == "demo"
    snapshot = None
    if is_demo:
        seed = root / "seed" / "SW-All.txt"
        if seed.exists():
            for line in seed.read_text(encoding="utf-8", errors="replace").splitlines()[:6]:
                if line.startswith("UPDATED "):
                    snapshot = line[len("UPDATED "):].strip()
                    break
    return {
        "data_origin": "demo" if is_demo else "local",
        "is_demo": is_demo,
        "operational": False,          # 本系統目前全域皆非作業級，見 README「限制與非目標」
        "snapshot_time": snapshot,
        "root": str(root),
    }


@lru_cache(maxsize=2)
def imagery() -> list[dict]:
    """影像來源盤點（configs/imagery.yaml）。

    影像為外部機構產製，本系統僅嵌入呈現。每一筆都必須帶 attribution——
    載入時就檢查，缺標註直接拋錯，不讓未標註的影像有機會顯示出去。
    """
    path = config_dir() / "imagery.yaml"
    if not path.exists():
        return []
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    items = list(doc.get("images") or [])
    missing = [i.get("id", "?") for i in items if not i.get("attribution")]
    if missing:
        raise ValueError(f"影像來源缺少 attribution，不得呈現：{missing}")
    return items


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    name: str
    connector: str | None
    tier: int
    status: str
    provides: tuple[str, ...]
    cadence_s: int | None
    latency_budget_s: int | None
    endpoint: str | None
    fmt: str | None
    local_fallback: str | None
    fallback: tuple[str, ...]
    notes: str | None
    publication_lag_s: int
    raw: dict

    @property
    def is_ready(self) -> bool:
        return self.status == "ready" and bool(self.connector)

    def local_path(self) -> Path | None:
        if not self.local_fallback:
            return None
        p = Path(self.local_fallback)
        return p if p.is_absolute() else project_root() / p


class SourceCatalog:
    """configs/sources.yaml 的物件視圖。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else config_dir() / "sources.yaml"
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        self.defaults: dict = raw.get("defaults", {})
        self._sources: dict[str, SourceSpec] = {}
        for item in raw.get("sources", []):
            spec = SourceSpec(
                source_id=item["source_id"],
                name=item.get("name", item["source_id"]),
                connector=item.get("connector"),
                tier=int(item.get("tier", 1)),
                status=item.get("status", "planned"),
                provides=tuple(item.get("provides") or ()),
                cadence_s=item.get("cadence_s"),
                latency_budget_s=item.get("latency_budget_s"),
                endpoint=item.get("endpoint"),
                fmt=item.get("format"),
                local_fallback=item.get("local_fallback"),
                fallback=tuple(item.get("fallback") or ()),
                notes=item.get("notes"),
                publication_lag_s=int(
                    item.get("publication_lag_s",
                             raw.get("defaults", {}).get("publication_lag_s", 3600))
                ),
                raw=item,
            )
            self._sources[spec.source_id] = spec

    def __getitem__(self, source_id: str) -> SourceSpec:
        return self._sources[source_id]

    def __iter__(self):
        return iter(self._sources.values())

    def __len__(self) -> int:
        return len(self._sources)

    @property
    def ids(self) -> list[str]:
        return list(self._sources)

    def ready(self) -> list[SourceSpec]:
        return [s for s in self._sources.values() if s.is_ready]

    def providing(self, param_code: str) -> list[SourceSpec]:
        """回傳提供該參數的來源，依 tier 排序（主來源優先）。"""
        hits = [s for s in self._sources.values() if param_code in s.provides]
        return sorted(hits, key=lambda s: s.tier)

    @property
    def timeout_s(self) -> int:
        return int(self.defaults.get("request_timeout_s", 30))

    @property
    def retry_attempts(self) -> int:
        return int((self.defaults.get("retry") or {}).get("attempts", 3))

    @property
    def retry_backoff_s(self) -> float:
        return float((self.defaults.get("retry") or {}).get("backoff_s", 5))


@lru_cache(maxsize=4)
def catalog(path: str | None = None) -> SourceCatalog:
    return SourceCatalog(Path(path) if path else None)
