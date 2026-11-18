"""查詢效能最佳化的等價性契約（架構書 §16.2 G8）。

兩項最佳化都宣稱「只少讀檔、不改結果」。這裡把宣稱變成逐列比對：

  · 分區裁剪：只讀與查詢窗重疊的分區。分區鍵就是 valid_time，所以不可能漏列——
    但邊界（年初 00:00、月底最後一刻、含端點的 end）最容易差一格。
  · latest() 由新到舊逐分區找：與全掃結果相同，含 as_of 回放與年月分區混用。
  · 規則引擎單輪快取：同一輪 evaluate() 內重用序列，結果與逐條規則各自查詢相同；
    且**不跨輪**——新資料寫入後下一輪必須看得到。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from services.risk_engine.engine import RiskEngine
from swx_core import SwxStore

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "data" / "demo"


def _full_scan(store: SwxStore, monkeypatch):
    """關掉裁剪：partition_globs 一律回傳整個參數的 glob。"""
    monkeypatch.setattr(store, "partition_globs",
                        lambda params=None, start=None, end=None: store.globs(params))


def _write(store, param, times, values, *, ingest="2027-01-01", source="t", tier=1):
    store.write(pd.DataFrame({
        "valid_time": pd.DatetimeIndex(times, tz="UTC"), "param_code": param,
        "value": values, "unit": "", "source_id": source, "source_tier": tier,
        "quality_flag": "good", "data_type": "OBS",
    }), ingest_time=pd.Timestamp(ingest, tz="UTC").to_pydatetime(), dedupe=False)


@pytest.fixture
def boundary_store(tmp_path):
    s = SwxStore(tmp_path)
    # KP_3H：年分區。跨 2025→2026 年界，含恰在年初 00:00 的列
    _write(s, "KP_3H", ["2025-12-31T21:00", "2026-01-01T00:00", "2026-01-01T03:00",
                        "2026-06-30T21:00", "2026-07-01T00:00"], [1, 2, 3, 4, 5])
    # 後來的修訂（較新 ingest）只動年初那一列
    _write(s, "KP_3H", ["2026-01-01T00:00"], [9], ingest="2027-02-01")
    return s


WINDOWS = [
    (None, None),
    ("2026-01-01T00:00", None),
    (None, "2025-12-31T23:59:59.999999"),
    ("2025-12-31T21:00", "2026-01-01T00:00"),     # end 恰在年界，含端點
    ("2026-01-01T00:00:00.000001", "2026-12-31"),
    ("2024-01-01", "2024-12-31"),                 # 無資料的年份
    ("2026-07-01T00:00", "2026-07-01T00:00"),     # 單點窗
]


@pytest.mark.parametrize("start,end", WINDOWS)
@pytest.mark.parametrize("as_of", [None, "2027-01-15"])
def test_pruned_query_equals_full_scan_at_boundaries(boundary_store, monkeypatch,
                                                     start, end, as_of):
    kw = dict(start=start and pd.Timestamp(start, tz="UTC"),
              end=end and pd.Timestamp(end, tz="UTC"),
              as_of=as_of and pd.Timestamp(as_of, tz="UTC"))
    pruned = boundary_store.query("KP_3H", **kw)
    _full_scan(boundary_store, monkeypatch)
    full = boundary_store.query("KP_3H", **kw)
    pd.testing.assert_frame_equal(pruned, full)


def test_pruning_actually_skips_partitions(boundary_store):
    globs = boundary_store.partition_globs(
        ["KP_3H"], start=pd.Timestamp("2026-03-01", tz="UTC"),
        end=pd.Timestamp("2026-03-02", tz="UTC"))
    assert len(globs) == 1 and "/2026/" in globs[0]


@pytest.mark.parametrize("as_of", [None, "2027-01-15", "2026-12-01"])
def test_latest_equals_full_scan(boundary_store, monkeypatch, as_of):
    a = as_of and pd.Timestamp(as_of, tz="UTC")
    fast = boundary_store.latest("KP_3H", as_of=a)
    df = boundary_store.query("KP_3H", as_of=a, observed_only=True)
    slow = None if df.empty else df.iloc[-1]
    if slow is None:
        assert fast is None
    else:
        # Series 名稱是列號（全掃與單分區的位置不同），比內容不比位置
        pd.testing.assert_series_equal(fast, slow, check_names=False)


def test_latest_skips_newer_partition_without_observations(tmp_path):
    """最新分區只有被拒收的列時，要往前一個分區找，而不是回傳 None。"""
    s = SwxStore(tmp_path)
    _write(s, "KP_3H", ["2025-06-01T00:00"], [4.0])
    bad = pd.DataFrame({
        "valid_time": pd.DatetimeIndex(["2026-06-01T00:00"], tz="UTC"),
        "param_code": "KP_3H", "value": [3.0], "unit": "", "source_id": "t",
        "source_tier": 1, "quality_flag": "rejected", "data_type": "OBS"})
    s.write(bad, dedupe=False)
    assert s.latest("KP_3H")["value"] == 4.0


def test_same_source_same_ingest_time_does_not_overwrite(tmp_path):
    """append-only：相同 (source, ingest_time) 再寫一次，前一批不得消失。"""
    s = SwxStore(tmp_path)
    _write(s, "KP_3H", ["2026-10-01T00:00"], [7.0])
    _write(s, "KP_3H", ["2026-10-01T03:00"], [2.0])
    assert s.query("KP_3H")["value"].tolist() == [7.0, 2.0]


def test_latest_with_mixed_year_and_month_partitions_falls_back(tmp_path):
    """年月分區混用時，名稱排序會選錯分區（2026-08 排在 2026 前）——必須全掃。"""
    s = SwxStore(tmp_path)
    _write(s, "KP_3H", ["2026-10-01T00:00"], [7.0])            # 年分區 2026/
    _write(s, "KP_3H", ["2026-08-01T00:00"], [2.0], ingest="2027-01-02")
    month = s.parquet_root / "KP_3H" / "2026-08"
    month.mkdir()
    (moved,) = (s.parquet_root / "KP_3H" / "2026").glob("*20270102*.parquet")
    moved.replace(month / moved.name)                           # 模擬舊 cadence 的月分區
    assert s.query("KP_3H")["value"].tolist() == [2.0, 7.0]
    assert s.latest("KP_3H")["value"] == 7.0


# ── 規則引擎單輪快取 ────────────────────────────────────────────────────
@pytest.mark.skipif(not (DEMO / "swx_parquet").is_dir(), reason="需要示範快照")
@pytest.mark.parametrize("start,end", [("2024-05-08", "2024-05-15"),
                                       ("2026-10-20", "2026-10-28")])
def test_memoized_evaluate_equals_per_rule_evaluation(start, end):
    store = SwxStore(DEMO)
    eng = RiskEngine(store)
    kw = dict(start=pd.Timestamp(start, tz="UTC").to_pydatetime(),
              end=pd.Timestamp(end, tz="UTC").to_pydatetime())
    eps, status = eng.evaluate(**kw)

    # 參照：不經 evaluate()，逐條規則各自查詢（_memo 為 None，不快取）
    ref_eps, ref_status = [], []
    for rule in eng.rules:
        e, st = eng.evaluate_rule(rule, **kw)
        ref_eps.extend(e)
        ref_status.append((rule.rule_id, st, len(e)))

    key = lambda e: (e.rule_id, e.start, e.end, e.level, e.peak_param,  # noqa: E731
                     None if pd.isna(e.peak_value) else round(float(e.peak_value), 9))
    assert sorted(map(key, eps)) == sorted(map(key, ref_eps))
    assert list(zip(status["rule_id"], status["status"], status["n_episodes"])) == ref_status


def test_memo_does_not_leak_across_evaluations(tmp_path):
    s = SwxStore(tmp_path)
    t = pd.date_range("2026-11-01", periods=8, freq="3h", tz="UTC")
    _write(s, "KP_3H", t, [2.0] * 8, ingest="2026-11-02")
    eng = RiskEngine(s)
    kw = dict(start=datetime(2026, 11, 1, tzinfo=timezone.utc),
              end=datetime(2026, 11, 3, tzinfo=timezone.utc))
    before = [e for e in eng.evaluate(**kw)[0] if "KP" in e.peak_param]
    _write(s, "KP_3H", t[4:], [9.0] * 4, ingest="2026-11-02T12:00")
    after = [e for e in eng.evaluate(**kw)[0] if "KP" in e.peak_param]
    assert not before and after, "第二輪必須看到新寫入的 Kp 9"
    assert eng._memo is None


def test_evaluate_queries_each_series_once(tmp_path, monkeypatch):
    s = SwxStore(tmp_path)
    _write(s, "KP_3H", pd.date_range("2026-11-01", periods=8, freq="3h", tz="UTC"),
           [2.0] * 8)
    eng = RiskEngine(s)
    calls = []
    orig = s.series
    monkeypatch.setattr(s, "series", lambda p, **kw: calls.append(
        (p, kw.get("grid_id"))) or orig(p, **kw))
    eng.evaluate(start=datetime(2026, 11, 1, tzinfo=timezone.utc),
                 end=datetime(2026, 11, 3, tzinfo=timezone.utc))
    assert len(calls) == len(set(calls)), "同一輪內同一序列被查了不只一次"
    assert sum(1 for p, _ in calls if p == "KP_3H") == 1
