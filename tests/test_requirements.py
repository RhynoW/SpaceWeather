"""相依宣告完整性測試。

實測過的坑：`certifi` 被 `services/ingest/base.py` 直接 import，卻只因為
`requests` 把它拉進來而能運作。開發機有五百多個套件，這種漏宣告完全看不出來——
雲端只裝 requirements.txt，一旦上游改變相依關係就會在部署時才爆。

這個測試掃描原始碼的第三方 import，與 requirements.txt 比對。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("packages", "services", "apps", "tools")

# import 名稱 → 套件名稱（兩者不同時才需列出）
IMPORT_TO_PACKAGE = {
    "yaml": "pyyaml",
    "sklearn": "scikit-learn",
    "flask_cors": "flask-cors",
    "docx": "python-docx",
}

# 本專案自有模組與標準庫（不需宣告）
# 本專案自有模組。`stem` 是 apps/dashboard 下的同層模組（由 app.py 直接 import），
# 不是 PyPI 套件——漏列會讓相依測試誤報。
LOCAL = {"swx_core", "services", "geomag", "orbit_drag", "apps", "tools", "stem", "__future__"}
STDLIB = {
    "abc", "argparse", "ast", "base64", "collections", "contextlib", "copy", "csv",
    "dataclasses", "datetime", "enum", "functools", "glob", "hashlib", "importlib",
    "io", "itertools", "json", "logging", "math", "operator", "os", "pathlib",
    "random", "re", "shutil", "socket", "sqlite3", "ssl", "struct", "subprocess",
    "sys", "tarfile", "tempfile", "textwrap", "threading", "time", "typing",
    "urllib", "uuid",
    "warnings", "zoneinfo",
}


def _declared() -> set[str]:
    out = set()
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            out.add(re.split(r"[><=!\[]", line)[0].strip().lower())
    return out


def _imported() -> set[str]:
    pattern = re.compile(r"^\s*(?:import|from)\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.M)
    out = set()
    for d in SCAN_DIRS:
        for path in (ROOT / d).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            out |= set(pattern.findall(path.read_text(encoding="utf-8", errors="replace")))
    return {m for m in out if m not in LOCAL and m not in STDLIB}


def test_every_third_party_import_is_declared():
    declared = _declared()
    missing = sorted(
        mod for mod in _imported()
        if IMPORT_TO_PACKAGE.get(mod, mod).lower() not in declared
    )
    assert not missing, (
        f"以下套件被程式直接 import 但未宣告於 requirements.txt：{missing}。"
        "開發機可能因遞移相依而能運作，雲端部署會失敗。"
    )
