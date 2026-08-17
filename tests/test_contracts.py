"""資料契約測試（架構書 P2：子計畫間只靠契約互動，契約破了要立刻知道）。

這些測試守的是**跨子計畫介面**，不是實作細節：
  · CSSI 格式讀寫必須完全可逆（否則 STK 拿到的驅動檔會失真）
  · 雙時間軸查詢必須無前視偏差（否則預報驗證的數字全部不可信）
  · 品質旗標與參數字典必須一致（否則資料會被靜默丟棄或誤放行）
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from swx_core import (
    DATA_TYPE_OBS,
    QUALITY_GOOD,
    QUALITY_REJECTED,
    QUALITY_SUSPECT,
    SwxStore,
    apply_quality,
    catalog,
    cssi,
    normalize,
    registry,
)
from swx_core.flare import class_to_flux, flux_to_class, mission_level, r_scale


# ── 參數字典 ────────────────────────────────────────────────────────────
def test_registry_loads_and_is_self_consistent():
    reg = registry()
    assert len(reg.codes) > 20
    for code in reg.codes:
        spec = reg[code]
        assert spec.unit, f"{code} 缺少單位"
        assert spec.status in ("ready", "planned")
        if spec.valid_min is not None and spec.valid_max is not None:
            assert spec.valid_min < spec.valid_max, f"{code} 值域顛倒"
        for domain in spec.impacts:
            assert domain in reg.impact_domains, f"{code} 的影響網域 {domain} 未定義"


def test_every_source_provides_registered_params():
    """來源宣告提供的參數必須都在字典裡，否則入庫會被判 unregistered。"""
    reg = registry()
    for source in catalog():
        for param in source.provides:
            assert param in reg, f"來源 {source.source_id} 宣告的 {param} 未註冊"


def test_unregistered_param_raises():
    with pytest.raises(KeyError):
        registry()["NO_SUCH_PARAM"]


# ── CSSI 格式（STK 介接的命脈）──────────────────────────────────────────
CSSI_SAMPLE = (
    "2021 01 01 2556 10  0  3  7  3  3 13  7  7  43   0   2   3   2   2   5   3   3"
    "   2 0.0 0  24  77.7 0  80.4  83.5  80.4  82.9  85.4"
)


def test_cssi_line_roundtrip_is_byte_identical():
    parsed = cssi.parse_line(CSSI_SAMPLE)
    assert cssi.format_line(parsed) == CSSI_SAMPLE.ljust(cssi.LINE_WIDTH)


def test_cssi_field_positions_match_gmat_reader():
    """GMAT 以 substr(92) 起讀 F10.7 區塊；欄位起點若飄掉，STK/GMAT 會讀到錯值。"""
    starts = {name: start for name, start, _end, _kind in cssi.FIELDS}
    assert starts["f107_adj"] == 92
    assert cssi.LINE_WIDTH == 130


def test_cssi_parses_observed_and_predicted_sections():
    text = "\n".join(
        [
            "BEGIN OBSERVED",
            CSSI_SAMPLE,
            "END OBSERVED",
            "BEGIN MONTHLY_PREDICTED",
            "2041 10 01 2837  1" + " " * 70 + "  10  70.0    69.2  70.5  69.8  68.8  69.0",
            "END MONTHLY_PREDICTED",
        ]
    )
    df = cssi.parse_text(text)
    assert set(df["section"]) == {"OBSERVED", "MONTHLY_PREDICTED"}
    assert df["f107_adj"].notna().all()


def test_kp_quantisation_is_thirds_for_observed():
    """Kp 定義在三分位；直接除以 10 會讓 KpSum 系統性偏低。"""
    assert cssi.kp_from_file(3) == pytest.approx(1 / 3)
    assert cssi.kp_from_file(7) == pytest.approx(2 / 3)
    assert cssi.kp_from_file(13) == pytest.approx(4 / 3)
    for raw in (0, 3, 7, 13, 27, 90):
        assert cssi.kp_to_file(cssi.kp_from_file(raw)) == raw


def test_observations_roundtrip_through_cssi():
    """CSSI → 長表 → CSSI 必須還原同一行（這是 STK 匯出正確性的根本）。"""
    wide = cssi.parse_text("BEGIN OBSERVED\n" + CSSI_SAMPLE + "\nEND OBSERVED")
    obs = cssi.to_observations(wide, source_id="test")
    back = cssi.from_observations(obs)
    back["bsrn"], back["nd"] = wide["bsrn"], wide["nd"]
    assert cssi.format_line(back.iloc[0]) == CSSI_SAMPLE.ljust(cssi.LINE_WIDTH)


# ── 品質控管 ────────────────────────────────────────────────────────────
def _obs(param: str, values, *, start="2024-01-01", freq="3h"):
    times = pd.date_range(start, periods=len(values), freq=freq, tz="UTC")
    return normalize(
        pd.DataFrame(
            {
                "valid_time": times,
                "param_code": param,
                "value": values,
                "unit": registry()[param].unit,
                "source_id": "test",
                "data_type": DATA_TYPE_OBS,
            }
        )
    )


def test_out_of_range_is_rejected_not_silently_kept():
    df = apply_quality(_obs("KP_3H", [1.0, 2.0, 99.0]))
    assert list(df["quality_flag"]) == [QUALITY_GOOD, QUALITY_GOOD, QUALITY_REJECTED]
    assert "above_valid_max" in str(df.iloc[2]["quality_reason"])


def test_missing_value_is_rejected():
    df = apply_quality(_obs("KP_3H", [1.0, None, 2.0]))
    assert df.iloc[1]["quality_flag"] == QUALITY_REJECTED


def test_spike_is_flagged_suspect_not_dropped():
    df = apply_quality(_obs("AP_AVG", [5.0, 6.0, 380.0], freq="24h"))
    assert QUALITY_SUSPECT in set(df["quality_flag"])
    assert df["value"].notna().all(), "突波應標記而非刪除，原值必須保留"


# ── 雙時間軸（回放無前視偏差）───────────────────────────────────────────
@pytest.fixture
def temp_store(tmp_path):
    return SwxStore(tmp_path)


def test_as_of_query_excludes_later_ingests(temp_store):
    """核心不變式：as_of 查詢絕不能看到那之後才入庫的資料。"""
    t0 = datetime(2024, 5, 10, tzinfo=timezone.utc)
    early = datetime(2024, 5, 10, 6, tzinfo=timezone.utc)
    late = datetime(2024, 5, 12, tzinfo=timezone.utc)

    first = _obs("KP_3H", [4.0], start="2024-05-10")
    temp_store.write(first, source_id="test", ingest_time=early)

    revised = _obs("KP_3H", [9.0], start="2024-05-10")   # 事後訂正
    temp_store.write(revised, source_id="test", ingest_time=late)

    # 回放到訂正之前 → 只能看到當時的 4.0
    at_early = temp_store.query("KP_3H", as_of=early)
    assert len(at_early) == 1
    assert at_early.iloc[0]["value"] == pytest.approx(4.0)

    # 不指定 as_of → 取最新版本
    latest = temp_store.query("KP_3H")
    assert latest.iloc[0]["value"] == pytest.approx(9.0)


def test_write_is_append_only_and_dedupes_unchanged(temp_store):
    df = _obs("KP_3H", [1.0, 2.0, 3.0])
    first = temp_store.write(df, source_id="test")
    assert first.rows_written == 3

    again = temp_store.write(df, source_id="test")
    assert again.rows_written == 0, "未變更的資料不應重複寫入"
    assert again.rows_skipped == 3

    changed = df.copy()
    changed.loc[1, "value"] = 5.0
    third = temp_store.write(changed, source_id="test")
    assert third.rows_written == 1, "只有變更的那筆該寫入"


def test_multi_source_prefers_primary_tier(temp_store):
    primary = _obs("KP_3H", [3.0])
    primary["source_id"], primary["source_tier"] = "primary", 1
    backup = _obs("KP_3H", [8.0])
    backup["source_id"], backup["source_tier"] = "backup", 2

    temp_store.write(backup, source_id="backup")
    temp_store.write(primary, source_id="primary")

    got = temp_store.query("KP_3H")
    assert len(got) == 1
    assert got.iloc[0]["source_id"] == "primary"


def test_rejected_rows_excluded_by_default(temp_store):
    temp_store.write(apply_quality(_obs("KP_3H", [1.0, 99.0])), source_id="test")
    assert len(temp_store.query("KP_3H")) == 1
    assert len(temp_store.query("KP_3H", include_rejected=True)) == 2


def test_partition_granularity_follows_cadence():
    assert SwxStore.partition_format("F107_OBS") == "%Y"      # 日尺度 → 年分區
    assert SwxStore.partition_format("IMF_BZ") == "%Y-%m"     # 分鐘尺度 → 月分區


# ── 太陽閃焰分級 ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "flux,expected_class,expected_r",
    [
        (1.0e-8, "A1.0", None),
        (5.2e-6, "C5.2", None),
        (1.0e-5, "M1.0", "R1"),
        (5.0e-5, "M5.0", "R2"),
        (1.0e-4, "X1.0", "R3"),
        (1.0e-3, "X10.0", "R4"),
        (2.0e-3, "X20.0", "R5"),
    ],
)
def test_flare_classification(flux, expected_class, expected_r):
    assert flux_to_class(flux) == expected_class
    assert r_scale(flux) == expected_r


def test_flare_class_string_roundtrip():
    for cls in ("A1.0", "B5.2", "C9.9", "M1.0", "X1.5", "X20.0"):
        assert flux_to_class(class_to_flux(cls)) == cls


def test_flare_mission_level_mapping():
    assert mission_level(5.0e-6) == "L0"     # C 級不觸發
    assert mission_level(1.0e-5) == "L1"     # M1 → R1
    assert mission_level(1.0e-4) == "L3"     # X1 → R3
    assert mission_level(2.0e-3) == "L4"     # X20 → R5


def test_daily_param_not_overwritten_by_sub_daily_rows():
    """日尺度參數不得被同日較晚時刻的列覆蓋。

    背景：OMNI 之類的來源會把日指數（F10.7）複製到每個小時。若 from_observations
    對同一天的每一列都寫入，就是「最後一筆勝出」——權威來源（tier 1，記於 00:00）
    的值會被較低階來源的重複值覆蓋，而且**不會報錯**，只是匯出給 STK 的檔案悄悄變錯。
    這個缺陷曾使 CSSI 匯出從 2,279 行一致掉到 252 行。
    """
    day = pd.Timestamp("2021-01-01", tz="UTC")
    rows = [
        {"valid_time": day, "param_code": "F107_OBS", "value": 80.4, "unit": "sfu",
         "source_id": "authoritative", "source_tier": 1, "data_type": "OBS"},
    ] + [
        {"valid_time": day + pd.Timedelta(hours=h), "param_code": "F107_OBS",
         "value": 77.7, "unit": "sfu", "source_id": "hourly_repeat",
         "source_tier": 3, "data_type": "OBS"}
        for h in range(1, 24)
    ]
    wide = cssi.from_observations(normalize(pd.DataFrame(rows)))
    assert len(wide) == 1
    assert wide.iloc[0]["f107_obs"] == pytest.approx(80.4), (
        f"日值被同日較晚的列覆蓋（得到 {wide.iloc[0]['f107_obs']}）"
    )


def test_primary_tier_wins_within_same_timestamp():
    """同一時刻多來源時，tier 最小者勝出。"""
    day = pd.Timestamp("2021-01-01", tz="UTC")
    rows = [
        {"valid_time": day, "param_code": "F107_OBS", "value": 99.9, "unit": "sfu",
         "source_id": "backup", "source_tier": 3, "data_type": "OBS"},
        {"valid_time": day, "param_code": "F107_OBS", "value": 80.4, "unit": "sfu",
         "source_id": "primary", "source_tier": 1, "data_type": "OBS"},
    ]
    wide = cssi.from_observations(normalize(pd.DataFrame(rows)))
    assert wide.iloc[0]["f107_obs"] == pytest.approx(80.4)
