"""pytest 共用設定：把 packages 與專案根加入匯入路徑。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "packages"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
