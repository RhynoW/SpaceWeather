"""services — SWX-SDA 服務層（擷取／模型／預報／風險／匯出／API）。

路徑啟動：把 <root>/packages 加入 sys.path，讓 `python -m services.*` 在未安裝
套件的情況下也能直接執行（開發與封閉環境部署皆常見）。
正式部署建議改用 `pip install -e .`，屆時本段為無作用。
"""

from __future__ import annotations

import sys
from pathlib import Path

_PACKAGES = Path(__file__).resolve().parents[1] / "packages"
if _PACKAGES.is_dir() and str(_PACKAGES) not in sys.path:
    sys.path.insert(0, str(_PACKAGES))

# Windows 主控台預設 cp950，輸出繁中與符號會拋 UnicodeEncodeError。
# 沿用 Sat_TraingDataExtension 各腳本的作法，統一在此改為 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
