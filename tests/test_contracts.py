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


def test_cssi_rejects_a_foreign_layout_that_claims_the_same_datatype():
    """同一個 `DATATYPE CSSISpaceWeather VERSION 1.3`，欄位版面可能不同。

    實例：COMSPOC（Spacebook）也發布這個格式的檔案，檔頭的 DATATYPE 與
    VERSION 與 CelesTrak 完全一致，但它宣告的 FORTRAN 格式沒有 1X 分隔
    （`I4,I3,I3,...`），實際行長 132 字元而非 130——**尾端五個 F6.1 欄位
    整體位移 2 格**。

    位移 2 格影響的是 F10.7 的 81 天平均，那是直接餵進大氣阻力模型的量。
    這種檔案必須**被拒絕**，不得安靜地讀出一組合理但錯誤的數字。
    本測試鎖住「大聲失敗」這個行為。
    """
    # 實測差異：F10.7 區塊前多一格空白，行尾再多一格 → 132 字元
    shifted = CSSI_SAMPLE.replace(" 0  80.4", " 0   80.4") + " "
    assert len(shifted) == 132, "測試素材未重現 COMSPOC 版面"

    with pytest.raises(ValueError):
        cssi.parse_line(shifted)

    # 對照組：未位移的同一列必須正常解析，證明拒絕的原因是版面而非內容
    assert cssi.parse_line(CSSI_SAMPLE)["f107_obs_c81"] == pytest.approx(82.9)


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


# ── 來源標註（公開資料引用義務）────────────────────────────────────────
def test_every_source_has_attribution():
    """每個資料源都必須標註產製者、出處與使用條款。

    本系統整合的全是外部機構產製的資料，引用時有標註義務。
    把它變成契約測試，是因為「新增來源時忘了填標註」不會有任何徵兆——
    資料照常入庫、畫面照常顯示，只有法遵出問題。
    """
    for source in catalog():
        attr = source.raw.get("attribution")
        assert attr, f"來源 {source.source_id} 缺少 attribution"
        for key in ("provider", "product", "url", "terms"):
            assert attr.get(key), f"來源 {source.source_id} 的 attribution 缺 {key}"


def test_every_image_has_attribution():
    """影像同理，且載入器會在缺標註時直接拋錯而非靜默略過。"""
    from swx_core import imagery

    items = imagery()
    assert items, "影像盤點為空"
    for item in items:
        attr = item.get("attribution")
        assert attr, f"影像 {item.get('id')} 缺少 attribution"
        for key in ("provider", "url", "terms"):
            assert attr.get(key), f"影像 {item.get('id')} 的 attribution 缺 {key}"


def test_restricted_source_terms_are_explicit():
    """非公開來源的授權限制必須寫進條款，不能只寫在註解裡。

    CWA SWOO 的端點非公開 API，本案經授權使用；第三方不得比照辦理。
    這個限制若只存在於程式註解，複用本專案的人不會看到。
    """
    spec = next(s for s in catalog() if s.source_id == "cwa_swoo")
    terms = spec.raw["attribution"]["terms"]
    assert "授權" in terms
    assert "第三方" in terms, "未載明第三方不得逕行取用"


# ── 自動更新的新鮮度判定 ────────────────────────────────────────────────
def test_freshness_ignores_future_ingest_times(tmp_path):
    """預測列的 ingest_time 落在未來，不得被當成「我們已經拿到資料」。

    背景：CelesTrak 月預測列的 valid_time 遠到 2041 年，回填模式據此推算的
    ingest_time 也在未來。若 max(ingest_time) 不濾掉這些，齡期會變成負值，
    **自動更新永遠不會觸發**——而且畫面一切正常，不會有任何徵兆。
    """
    from datetime import datetime, timezone

    from services.ingest.refresh import data_age_s, last_ingest_time

    store = SwxStore(tmp_path)
    now = datetime(2026, 8, 18, 12, tzinfo=timezone.utc)

    recent = _obs("KP_3H", [3.0], start="2026-08-18")
    store.write(recent, source_id="test",
                ingest_time=datetime(2026, 8, 18, 10, tzinfo=timezone.utc))

    future = _obs("F107_OBS", [120.0], start="2041-10-02", freq="24h")
    future["data_type"] = "PRM"          # 月預測
    store.write(future, source_id="test",
                ingest_time=datetime(2041, 10, 2, tzinfo=timezone.utc))

    latest = last_ingest_time(store, now=now)
    assert latest is not None
    assert latest.year == 2026, f"取到未來時刻 {latest}"

    age = data_age_s(store, now=now)
    assert age is not None and age > 0, "齡期不得為負"
    assert abs(age - 7200) < 60, f"齡期應約 2 小時，實得 {age}s"


def test_heavy_sources_excluded_from_auto_refresh():
    """重量級來源不得進入頁面載入路徑。

    實測 gfz_hp30 單一來源約 46 秒，佔全部擷取時間的六成；
    放進自動更新會讓每次逾時後的頁面載入卡住。
    """
    from services.ingest.refresh import HEAVY_SOURCES, live_sources

    auto = set(live_sources())
    assert not (auto & HEAVY_SOURCES), "重量級來源混入自動更新"
    assert "omni2_hourly" not in auto, "歷史回填來源混入自動更新"
    assert set(live_sources(include_heavy=True)) >= auto | HEAVY_SOURCES


def test_i95_is_in_the_auto_refresh_path():
    """e-GNSS I95 必須留在自動更新裡——它沒有回填管道。

    目錄列表被 WAF 阻擋、歷史檔名不可推測，漏掉的時段事後補不回來。
    任何把它移出自動更新的改動，都會安靜地在歷史序列上挖一個永久的洞。
    """
    from services.ingest.refresh import live_sources

    assert "nlsc_egnss_i95" in live_sources(), "I95 已不在自動更新路徑上"


def test_disable_switch_is_per_deployment(monkeypatch):
    """單一站台可停用來源，且不必改 sources.yaml。

    雲端展示站台與排程主機共用同一份設定檔；用設定檔關來源會兩邊一起關。
    """
    from services.ingest import refresh as R

    assert "nlsc_egnss_i95" in R.live_sources()
    monkeypatch.setenv("SWX_DISABLE_SOURCES", "nlsc_egnss_i95, cwa_swoo")
    assert R.disabled_sources() == {"nlsc_egnss_i95", "cwa_swoo"}
    after = set(R.live_sources())
    assert "nlsc_egnss_i95" not in after and "cwa_swoo" not in after
    assert "swpc_xray" in after, "停用開關波及了其他來源"


def test_refresh_result_distinguishes_why_a_source_has_no_data():
    """「沒有資料」的五種成因不得混為一談。

    抓失敗要去看網路、沒納入要去看設定、抓到但空的要去看對方版面——
    畫面只說「目前沒有資料」的話，值勤的人無從判斷該找誰。
    """
    from services.ingest.refresh import RefreshResult

    r = RefreshResult(ran=True, reason="強制更新",
                      attempted=["a", "b", "c"],
                      succeeded=["a", "b"], failed=[("c", "SSLError: bad chain")],
                      rows={"a": 12, "b": 0},
                      warnings={"a": ("Peng_Hu 的 I95 未取得（HTTPError）",)})

    assert r.status_of("a")[0] == "ok"
    assert "Peng_Hu" in r.status_of("a")[1], "部分成功的說明被吞掉"
    assert r.status_of("b")[0] == "empty"
    assert r.status_of("c") == ("failed", "SSLError: bad chain")
    assert r.status_of("d")[0] == "skipped"

    assert RefreshResult(ran=False, reason="資料齡期 5 分鐘").status_of("a")[0] == "not_run"


# ── 動畫來源 ────────────────────────────────────────────────────────────
def test_every_animation_has_attribution_and_valid_kind():
    from swx_core import animations

    items = animations()
    assert items, "動畫盤點為空"
    for item in items:
        attr = item.get("attribution")
        assert attr, f"動畫 {item.get('id')} 缺少 attribution"
        for key in ("provider", "url", "terms"):
            assert attr.get(key), f"動畫 {item.get('id')} 的 attribution 缺 {key}"
        assert item.get("kind") in ("video", "frames")
        if item["kind"] == "video":
            assert item.get("url"), f"{item['id']} 為 video 但無 url"
        else:
            assert item.get("index_url") and item.get("base_url"), \
                f"{item['id']} 為 frames 但缺 index_url/base_url"


def test_frame_sampling_is_evenly_spaced_and_keeps_both_ends():
    """抽樣必須等距並保留頭尾。

    取前 N 幀只會看到過去，取後 N 幀只會看到預測——兩者都讓動畫失去意義。
    Enlil 的序列橫跨過去數日到未來數日，頭尾都掉了就看不出 CME 何時抵達。
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "dashboard"))

    def sample(urls, max_frames):
        if len(urls) <= max_frames:
            return urls
        step = len(urls) / max_frames
        picked = [urls[min(len(urls) - 1, int(i * step))] for i in range(max_frames)]
        if picked[-1] != urls[-1]:
            picked[-1] = urls[-1]
        return picked

    urls = [f"f{i:03d}" for i in range(169)]
    got = sample(urls, 60)
    assert len(got) == 60
    assert got[0] == urls[0], "首幀遺失——看不到序列起點"
    assert got[-1] == urls[-1], "末幀遺失——看不到最新／最遠預測"
    assert got == sorted(got), "抽樣後順序錯亂"
    gaps = [int(got[i + 1][1:]) - int(got[i][1:]) for i in range(len(got) - 2)]
    assert max(gaps) - min(gaps) <= 1, f"抽樣不等距，間隔 {min(gaps)}–{max(gaps)}"

    # 幀數少於上限時應原樣回傳
    short = [f"f{i}" for i in range(10)]
    assert sample(short, 60) == short


def test_unregistered_param_code_is_rejected_on_write(tmp_path):
    """未註冊參數不得入庫（架構書 §6.3）。

    少了這道檢查，拼錯的參數代碼會安靜地長出一個新分區——寫入回報成功、
    查詢查不到、UI 少一條線，沒有任何一處會報錯。真的發生過：預報端以
    `f"{code}_STORM_PROB"` 拼出 `KP_3H_STORM_PROB`（正確為 `KP_STORM_PROB`），
    機率序列整批寫進一個沒人會去查的地方。
    """
    import pandas as pd
    import pytest

    from swx_core import SwxStore, normalize

    store = SwxStore(tmp_path)
    bad = normalize(pd.DataFrame([{
        "valid_time": pd.Timestamp("2026-01-01", tz="UTC"),
        "param_code": "KP_3H_STORM_PROB", "value": 0.5, "unit": "1",
        "source_id": "test", "source_tier": 1, "data_type": "FCS",
    }]))
    with pytest.raises(ValueError, match="未註冊"):
        store.write(bad, source_id="test")
    assert not (tmp_path / "swx_parquet" / "KP_3H_STORM_PROB").exists()
