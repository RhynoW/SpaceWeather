"""重新解析既有原始落地檔（`--reparse`）。

README 從一開始就寫著「原始落地：解析錯誤可重跑，不需重抓」，但在此之前
沒有任何入口能做到——解析規則一改（例如放寬 window_days 以回填歷史），
唯一的辦法是重新向來源下載整份檔案。這一組測試守住那句話：

  1. reparse 不連外，直接讀最新一份原始檔；
  2. 沒有原始檔時要清楚報錯，而不是靜默寫入空資料；
  3. reparse 不會再寫一份原始檔（否則每次重跑都複製一份 70 萬列的檔案）。
"""

from __future__ import annotations

import pandas as pd
import pytest

from services.ingest.base import Connector
from swx_core import SourceSpec, SwxStore, normalize


class _CountingConnector(Connector):
    """解析固定內容的假來源；記錄是否曾嘗試對外抓取。"""

    formats = ("test_fmt",)
    raw_ext = "txt"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fetch_calls = 0

    def fetch_bytes(self):
        self.fetch_calls += 1
        return b"3.0\n", "remote"

    def parse(self, payload: bytes) -> pd.DataFrame:
        value = float(payload.decode().strip().splitlines()[0])
        return normalize(pd.DataFrame([{
            "valid_time": pd.Timestamp("2026-01-01", tz="UTC"),
            "param_code": "KP_3H", "value": value, "unit": "1",
            "source_id": self.spec.source_id, "source_tier": 1, "data_type": "OBS",
        }]))


def _spec() -> SourceSpec:
    return SourceSpec(source_id="test_src", name="test", connector="x", tier=1,
                      status="ready", provides=("KP_3H",), cadence_s=10800,
                      latency_budget_s=None, endpoint="https://example.invalid/x",
                      fmt="test_fmt", local_fallback=None, fallback=(), notes=None,
                      publication_lag_s=0, raw={})


def test_reparse_reads_raw_instead_of_fetching(tmp_path):
    store = SwxStore(tmp_path)
    conn = _CountingConnector(_spec(), store)

    first = conn.run()                       # 正常抓取，會留下原始落地檔
    assert first.ok and conn.fetch_calls == 1
    assert first.raw_path.exists()

    # 來源端的內容換了，但我們只想重跑解析——不得再連外
    first.raw_path.write_bytes(b"7.5\n")
    again = conn.run(reparse=True)
    assert again.ok
    assert conn.fetch_calls == 1, "reparse 仍對外抓取"
    assert again.mode.startswith("reparse:")
    assert store.series("KP_3H").iloc[-1] == pytest.approx(7.5)


def test_reparse_does_not_duplicate_the_raw_file(tmp_path):
    store = SwxStore(tmp_path)
    conn = _CountingConnector(_spec(), store)
    conn.run()
    before = sorted((store.raw_root / "test_src").rglob("*.txt"))
    conn.run(reparse=True)
    after = sorted((store.raw_root / "test_src").rglob("*.txt"))
    assert before == after


def test_reparse_without_raw_file_fails_loudly(tmp_path):
    """沒有原始檔卻回報成功、寫入零列，是最難察覺的失敗。"""
    store = SwxStore(tmp_path)
    outcome = _CountingConnector(_spec(), store).run(reparse=True)
    assert not outcome.ok
    assert "原始落地檔" in (outcome.error or "")
