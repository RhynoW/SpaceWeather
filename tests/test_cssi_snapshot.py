"""CSSI 匯出：示範快照對自身種子檔的位元組一致性，以及 with_source 的來源界線。

README 對外宣稱「已定稿列全數一致」。這個數字曾悄悄從 2,054/2,054 退化到
1,947/2,054（寫入去重不比 data_type、回填預測列入庫時間落在未來、匯出混入
SWPC 預測），三個缺陷都不會報錯，只會讓 STK 拿到的檔案悄悄變錯。
因此把宣稱本身變成測試：示範快照有變動、或任何一層回歸，這裡就會失敗。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from swx_core import SwxStore, normalize, registry

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "data" / "demo"
SEED = DEMO / "seed" / "SW-All.txt"


@pytest.mark.skipif(not SEED.exists(), reason="無示範快照")
def test_demo_snapshot_settled_rows_are_byte_identical(monkeypatch, capsys):
    import sys
    sys.path.insert(0, str(ROOT / "tools"))
    import cssi_compare

    monkeypatch.setenv("SWX_DATA_DIR", str(DEMO))
    assert cssi_compare.compare(SEED, level="byte", show=5) == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    # 區段配置也要相符：來源缺 0、本系統多 0
    total = next(line for line in out.splitlines() if line.strip().startswith("合計"))
    assert total.split()[-2:] == ["0", "0"], total


def _rows(param, value, *, source, dtype, when="2026-10-30 00:00"):
    return normalize(pd.DataFrame({
        "valid_time": [pd.Timestamp(when, tz="UTC")], "param_code": [param],
        "value": [value], "unit": [registry()[param].unit],
        "source_id": [source], "data_type": [dtype],
    }))


def test_with_source_takes_predictions_only_from_celestrak(tmp_path):
    """同為 tier 1 的 SWPC 預測較晚入庫，也不得混進 with_source 的預測段。"""
    from services.exporter import stk_spaceweather as sk

    store = SwxStore(tmp_path)
    t1 = datetime(2026, 10, 28, tzinfo=timezone.utc)
    t2 = datetime(2026, 10, 29, tzinfo=timezone.utc)
    store.write(_rows("KP_3H", 1.333, source="celestrak_sw_all", dtype="PRD"),
                source_id="celestrak_sw_all", ingest_time=t1)
    store.write(_rows("KP_3H", 2.333, source="swpc_geomag_forecast", dtype="PRD"),
                source_id="swpc_geomag_forecast", ingest_time=t2)
    # 另一天：別的來源已有觀測 → 觀測勝過 CelesTrak 預測
    when = "2026-10-27 00:00"
    store.write(_rows("KP_3H", 1.333, source="celestrak_sw_all", dtype="PRD", when=when),
                source_id="celestrak_sw_all", ingest_time=t1)
    store.write(_rows("KP_3H", 4.0, source="gfz_nowcast", dtype="OBS", when=when),
                source_id="gfz_nowcast", ingest_time=t2)

    wide = sk.build_frame(store, mode=sk.MODE_SOURCE).set_index("date")
    assert wide.loc[pd.Timestamp("2026-10-30", tz="UTC"), "kp1"] == 13
    assert wide.loc[pd.Timestamp("2026-10-27", tz="UTC"), "kp1"] == 40
