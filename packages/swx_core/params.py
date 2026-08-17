"""swx_core.params — 參數登錄（param_registry）讀取與查詢（架構書 §6.3）。

configs/params.yaml 是單一權威來源：UI 標籤、API 欄位說明、品質檢核值域、
資料字典文件都從這裡出來，避免「文件說一套、系統做一套」。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pandas as pd
import yaml

from .config import config_dir


@dataclass(frozen=True)
class ParamSpec:
    code: str
    name_zh: str
    name_en: str
    unit: str
    domain: str
    valid_min: float | None
    valid_max: float | None
    spike_limit: float | None
    cadence_s: int | None
    scale: str | None
    impacts: tuple[str, ...]
    status: str

    @property
    def is_ready(self) -> bool:
        return self.status == "ready"


class ParamRegistry:
    """參數字典。以 code 查規格，並提供 UI／API 需要的衍生視圖。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else config_dir() / "params.yaml"
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        self._specs: dict[str, ParamSpec] = {}
        for item in raw.get("params", []):
            spec = ParamSpec(
                code=item["code"],
                name_zh=item.get("name_zh", item["code"]),
                name_en=item.get("name_en", item["code"]),
                unit=str(item.get("unit", "1")),
                domain=item.get("domain", "unknown"),
                valid_min=item.get("valid_min"),
                valid_max=item.get("valid_max"),
                spike_limit=item.get("spike_limit"),
                cadence_s=item.get("cadence_s"),
                scale=item.get("scale"),
                impacts=tuple(item.get("impacts") or ()),
                status=item.get("status", "planned"),
            )
            self._specs[spec.code] = spec
        self.impact_domains: dict[str, str] = raw.get("impact_domains", {})
        self.mission_levels: dict[str, dict] = raw.get("mission_levels", {})

    # ── 查詢 ────────────────────────────────────────────────────────────
    def __contains__(self, code: str) -> bool:
        return code in self._specs

    def __getitem__(self, code: str) -> ParamSpec:
        try:
            return self._specs[code]
        except KeyError as exc:  # 未註冊參數不得入庫（架構書 §6.3）
            raise KeyError(
                f"參數 {code!r} 未在 configs/params.yaml 註冊；"
                "新增參數請先註冊再入庫。"
            ) from exc

    def get(self, code: str, default=None) -> ParamSpec | None:
        return self._specs.get(code, default)

    @property
    def codes(self) -> list[str]:
        return list(self._specs)

    def by_domain(self, domain: str) -> list[ParamSpec]:
        return [s for s in self._specs.values() if s.domain == domain]

    def by_status(self, status: str) -> list[ParamSpec]:
        return [s for s in self._specs.values() if s.status == status]

    def by_impact(self, impact_domain: str) -> list[ParamSpec]:
        return [s for s in self._specs.values() if impact_domain in s.impacts]

    def unit(self, code: str) -> str:
        return self[code].unit

    # ── 衍生視圖 ────────────────────────────────────────────────────────
    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "param_code": s.code,
                    "name_zh": s.name_zh,
                    "name_en": s.name_en,
                    "unit": s.unit,
                    "domain": s.domain,
                    "valid_min": s.valid_min,
                    "valid_max": s.valid_max,
                    "cadence_s": s.cadence_s,
                    "scale": s.scale,
                    "impacts": ",".join(s.impacts),
                    "status": s.status,
                }
                for s in self._specs.values()
            ]
        )

    def to_data_dictionary_md(self) -> str:
        """產生資料字典 Markdown（交付文件由系統生成，不手寫）。"""
        lines = [
            "# 太空天氣參數資料字典",
            "",
            f"> 由 `{self.path.name}` 自動生成，共 {len(self._specs)} 個參數。",
            "",
            "| 代碼 | 名稱 | 單位 | 領域 | 值域 | 更新週期 | 國際分級 | 影響網域 | 狀態 |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for s in self._specs.values():
            rng = "—"
            if s.valid_min is not None or s.valid_max is not None:
                rng = f"{s.valid_min} ~ {s.valid_max}"
            cad = f"{s.cadence_s}s" if s.cadence_s else "—"
            lines.append(
                f"| `{s.code}` | {s.name_zh} | {s.unit} | {s.domain} | {rng} | "
                f"{cad} | {s.scale or '—'} | {', '.join(s.impacts) or '—'} | {s.status} |"
            )
        return "\n".join(lines) + "\n"


@lru_cache(maxsize=4)
def registry(path: str | None = None) -> ParamRegistry:
    """取得全域參數字典（快取）。"""
    return ParamRegistry(Path(path) if path else None)
