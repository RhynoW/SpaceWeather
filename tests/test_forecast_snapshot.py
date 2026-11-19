"""預報快照：雲端無法自行產生預報時的退路。

冷啟動實測：雲端空容器只有近幾天資料，訓練不了需要多年歷史的模型，
預報頁恆為「尚未產生」。快照隨版本發布；這裡守住三件事：
讀得出來、不偷看產生時刻之後的資料、儀表板會退回它並標示清楚。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from services import products
from swx_core import SwxStore

SNAP = products.forecast_snapshot_dir()
pytestmark = pytest.mark.skipif(not (SNAP / "meta.json").exists(), reason="無預報快照")


@pytest.mark.parametrize("target", ["hp30", "kp"])
def test_snapshot_view_is_complete_and_anchored(target):
    view, meta = products.forecast_snapshot_view(target)
    assert not view.own.empty and not view.observed.empty
    assert view.issued == pd.Timestamp(meta["anchor_utc"])
    # horizon 由錨點還原，必須落在該目標的產品 horizon 上
    hs = sorted(round((pd.Timestamp(t) - view.issued).total_seconds() / 3600, 2)
                for t in view.own.loc[view.own["param_code"] == view.spec.code, "valid_time"])
    assert hs == sorted(float(h) for h in view.spec.horizons)


@pytest.mark.parametrize("target", ["hp30", "kp"])
def test_snapshot_never_contains_data_ingested_after_generation(target):
    """觀測與官方對照都必須是產生當下已知的——不能拿事後的版本裝成當時的脈絡。"""
    meta = json.loads((SNAP / "meta.json").read_text(encoding="utf-8"))["targets"][target]
    gen = pd.Timestamp(meta["generated_utc"])
    view, _ = products.forecast_snapshot_view(target)
    for frame in (view.own, view.observed, view.reference):
        if not frame.empty:
            assert frame["ingest_time"].max() <= gen


def test_snapshot_is_separate_from_demo_store():
    """快照不得混進示範快照本體——那會讓 CSSI 快照比對與快照時刻失真。"""
    demo = SwxStore(SNAP.parent)
    fcs = demo.query(["HP30", "KP_3H"], source_id="swx_forecast",
                     start=pd.Timestamp("2026-11-01", tz="UTC"))
    assert fcs.empty
