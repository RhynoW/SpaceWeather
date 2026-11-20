"""部署更新後丟掉過期模組：Streamlit Cloud 推送後只重跑 app.py，不重載已 import 的模組。

實際發生：推送預報快照後，雲端短時預報頁拋
AttributeError（新版 app.py 呼叫舊版 products 沒有的 forecast_snapshot_view）。
"""

from __future__ import annotations

import importlib
import os
import sys
import time

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "apps" / "dashboard"))
import module_freshness as mf  # noqa: E402


@pytest.fixture
def pkg(tmp_path, monkeypatch):
    root = tmp_path / "fakepkg"
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "products.py").write_text("def old():\n    return 1\n", encoding="utf-8")
    (root / "exporter.py").write_text("X = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, mf._REGISTRY, raising=False)
    yield root
    for name in [n for n in sys.modules if n.split(".")[0] == "fakepkg"]:
        sys.modules.pop(name, None)


def _bump(path):
    later = time.time() + 5
    os.utime(path, (later, later))


def test_changed_file_drops_whole_package_and_reimport_is_fresh(pkg):
    roots = ("fakepkg",)
    assert mf.drop_stale(roots) == []                  # 第一輪：還沒載入任何東西
    importlib.import_module("fakepkg.products")
    importlib.import_module("fakepkg.exporter")
    mf.record(roots)
    assert mf.drop_stale(roots) == []                  # 沒有變更：不得清除

    (pkg / "products.py").write_text("def new():\n    return 2\n", encoding="utf-8")
    _bump(pkg / "products.py")
    dropped = mf.drop_stale(roots)
    # 整批丟掉（含未改的 exporter），避免新舊版本混在同一行程
    assert set(dropped) == {"fakepkg", "fakepkg.products", "fakepkg.exporter"}
    assert "fakepkg.exporter" not in sys.modules
    fresh = importlib.import_module("fakepkg.products")
    assert hasattr(fresh, "new") and not hasattr(fresh, "old")


def test_unrecorded_lazy_module_newer_than_generation_is_stale(pkg):
    roots = ("fakepkg",)
    mf.drop_stale(roots)                               # 建立這一代的起點
    importlib.import_module("fakepkg.exporter")        # 延遲 import，尚未 record
    _bump(pkg / "exporter.py")
    assert "fakepkg.exporter" in mf.drop_stale(roots)


def test_third_party_modules_are_never_dropped(pkg):
    import json  # noqa: F401

    import pandas  # noqa: F401

    roots = ("fakepkg",)
    mf.drop_stale(roots)
    importlib.import_module("fakepkg.products")
    mf.record(roots)
    _bump(pkg / "products.py")
    mf.drop_stale(roots)
    assert "json" in sys.modules and "pandas" in sys.modules
