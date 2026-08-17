"""swx_core — 太空天氣整合資訊系統（SWX-SDA）之共用核心。

模組分工：
  schema   swx_observation 資料契約（雙時間軸）
  params   param_registry 參數字典
  config   專案路徑與 sources.yaml 載入
  quality  品質控管管線（good/suspect/rejected）
  store    Parquet + DuckDB 資料層（as_of 回放）

三個子計畫只透過本套件與 services/* 的 API 互動，不共用彼此的內部結構
（架構書 P2：子計畫鬆耦合、契約強耦合）。
"""

from .config import (
    SourceCatalog, SourceSpec, catalog, config_dir, data_dir, data_origin, project_root,
)
from .params import ParamRegistry, ParamSpec, registry
from .quality import apply_quality, cross_source_check, interpolate_gaps, quality_summary
from .schema import (
    DATA_TYPE_FCS,
    DATA_TYPE_INT,
    DATA_TYPE_OBS,
    DATA_TYPE_PRD,
    DATA_TYPE_PRM,
    OBS_COLUMNS,
    QUALITY_GOOD,
    QUALITY_REJECTED,
    QUALITY_SUSPECT,
    Observation,
    empty_frame,
    normalize,
)
from .interpret import GUIDANCE, Guidance, assess, guidance_for
from .store import SwxStore, WriteResult

__version__ = "0.1.0"

__all__ = [
    "SourceCatalog", "SourceSpec", "catalog", "config_dir", "data_dir", "data_origin",
    "project_root",
    "ParamRegistry", "ParamSpec", "registry",
    "apply_quality", "cross_source_check", "interpolate_gaps", "quality_summary",
    "OBS_COLUMNS", "Observation", "empty_frame", "normalize",
    "QUALITY_GOOD", "QUALITY_SUSPECT", "QUALITY_REJECTED",
    "DATA_TYPE_OBS", "DATA_TYPE_INT", "DATA_TYPE_PRD", "DATA_TYPE_PRM", "DATA_TYPE_FCS",
    "SwxStore", "WriteResult",
    "GUIDANCE", "Guidance", "assess", "guidance_for",
    "__version__",
]
