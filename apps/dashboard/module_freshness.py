"""部署更新後，丟掉記憶體裡過期的專案模組。

Streamlit Cloud 在 git 推送後只會**重跑 app.py**，已載入 sys.modules 的
services／swx_core 等模組仍是舊版。新版 app.py 一呼叫舊模組沒有的函式就整頁
拋 AttributeError（實際發生：推送預報快照後，短時預報頁
`products.forecast_snapshot_view` 不存在；更早 I95 頁也踩過，當時只在單頁加退路）。
唯一的解法原本是手動 Reboot——研討會現場推送新快照時不能指望這件事。

作法：記下每個專案模組檔案的修改時間；之後每輪重跑先比對，任一檔案變了就把
**所有**專案模組自 sys.modules 移除，讓接下來的 import 全部重新載入。
不逐個 importlib.reload：模組彼此相依（products → exporter → orbit_drag），
只重載其中幾個會讓新舊版本混在同一個行程裡，比整批丟掉更難除錯。

本模組刻意不在清除名單內：它不依賴任何專案模組，自己過期也無害。
"""

from __future__ import annotations

import os
import sys
import time
import types

#: 專案自己的頂層模組名（第三方套件不在此列，不得清除）
PROJECT_ROOTS = ("services", "swx_core", "orbit_drag", "geomag", "tools", "stem", "media_url")
_REGISTRY = "_swx_module_mtimes"


def _registry() -> types.ModuleType:
    """跨重跑保存的狀態（掛在 sys.modules 上，app.py 每輪重跑都還在）。"""
    reg = sys.modules.get(_REGISTRY)
    if reg is None:
        reg = types.ModuleType(_REGISTRY)
        reg.mtimes = {}
        reg.since = time.time()      # 這一代模組開始載入的時刻
        sys.modules[_REGISTRY] = reg
    return reg


def _project_modules(roots=PROJECT_ROOTS) -> dict[str, str]:
    out = {}
    for name, mod in list(sys.modules.items()):
        if name.split(".")[0] in roots:
            path = getattr(mod, "__file__", None)
            if path:
                out[name] = path
    return out


def _mtime(path: str) -> float | None:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def drop_stale(roots=PROJECT_ROOTS) -> list[str]:
    """有專案模組的檔案在載入後被改過 → 全部移除並回傳被移除的名稱；否則回空清單。

    須在 import 專案模組**之前**呼叫。
    """
    reg = _registry()
    mods = _project_modules(roots)

    def stale(name: str, path: str) -> bool:
        m = _mtime(path)
        if name in reg.mtimes:
            return m != reg.mtimes[name]
        # 尚未記錄者（例如某頁才延遲 import 的模組）：檔案比這一代開始載入的時刻還新，
        # 就無法確定記憶體裡的是哪一版，寧可整批重載一次
        return m is not None and m > reg.since

    if not any(stale(n, p) for n, p in mods.items()):
        return []
    for name in mods:
        sys.modules.pop(name, None)
    reg.mtimes.clear()
    reg.since = time.time()
    return sorted(mods)


def record(roots=PROJECT_ROOTS) -> None:
    """記下目前已載入專案模組的檔案時間（只記尚未記錄者）。須在 import 之後呼叫。"""
    reg = _registry()
    for name, path in _project_modules(roots).items():
        if name not in reg.mtimes:
            m = _mtime(path)
            if m is not None:
                reg.mtimes[name] = m
