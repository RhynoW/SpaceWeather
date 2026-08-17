"""tools/make_lock.py — 由目前環境產生 requirements.lock。

為什麼不直接用 `pip freeze`：本專案的開發環境是共用的 Python，
`pip freeze` 會吐出五百多個與本案無關的套件，鎖了等於沒鎖——
使用者無從分辨哪些是重現本案結果真正需要的。

這支工具從 `requirements.txt` 的直接相依出發，遞迴解析各套件自身宣告的
`Requires-Dist`，只鎖定**實際構成本案相依樹**的部分（目前約 55 個）。

    python tools/make_lock.py > requirements.lock

限制（誠實載明）：
  · 版本取自**產生當下的環境**，非跨平臺求解的結果，
    不等同 uv/pip-tools 產生的多平臺 lock。
  · 不含 extras 相依（`extra == ...` 的條件相依一律略過）。
  · 平臺相關的相依（如 Windows 專屬套件）會照現況寫入。
正式交付若需跨平臺重現，仍應改用 uv 或 pip-tools 產生真正的求解式 lock。
"""

from __future__ import annotations

import importlib.metadata as md
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_DEPTH = 3


def direct_requirements() -> list[str]:
    out = []
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            out.append(re.split(r"[><=!\[]", line)[0].strip())
    return out


def resolve(names: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    seen: set[str] = set()
    found: list[tuple[str, str]] = []
    missing: list[str] = []

    def walk(name: str, depth: int = 0) -> None:
        key = name.lower().replace("_", "-")
        if key in seen or depth > MAX_DEPTH:
            return
        seen.add(key)
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            missing.append(name)
            return
        found.append((dist.metadata["Name"], dist.version))
        for req in dist.requires or []:
            if "extra ==" in req:          # 不鎖 extras
                continue
            walk(re.split(r"[><=!;\[ ]", req)[0].strip(), depth + 1)

    for n in names:
        walk(n)
    return found, missing


def main() -> int:
    found, missing = resolve(direct_requirements())
    print("# SWX-SDA 相依鎖定檔（直接相依 + 遞移相依的實測版本）")
    print(f"# 產生環境：Python {sys.version.split()[0]} / {sys.platform}")
    print("# 用途：讓 README「重現本文數字」章節的結果可在他處重現。")
    print("# 重新產生： python tools/make_lock.py > requirements.lock")
    print("# 非跨平臺求解結果；正式交付請改用 uv 或 pip-tools。")
    print()
    for name, version in sorted(found, key=lambda t: t[0].lower()):
        print(f"{name}=={version}")
    if missing:
        print()
        print("# 未安裝於產生環境（選用相依）：" + ", ".join(sorted(set(missing))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
