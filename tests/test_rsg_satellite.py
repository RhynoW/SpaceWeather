"""R/S/G 狀態與展望、衛星健康網域、以及「觀測優先於預測」的契約測試。

架構書 §16.2 G9–G10。守的事：
  · NOAA 3 日預報的 R／S 機率與 GOES 電子通量解析正確（素材為 2026-11-19 實檔）。
  · 同一時刻觀測一律勝過預測——曾發生 Kp 預報 3.67 蓋掉同時刻觀測。
  · 規則不得由預報值觸發（預報 Kp 9 不得產生「地磁暴」）；只有預測值的參數仍可用。
  · 衛星健康規則：質子 S2–S5、電子以持續時間分級、Kp 代理上限 L3。
  · 72 小時駐留門檻在 nowcast 內確實可觸發（回看窗自動延伸）。
  · R/S/G 狀態：無資料與未達門檻分開；「現在」過期即無資料；展望只取 NOAA 預報。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd
import pytest

from services import products
from services.api.app import create_app
from services.ingest.run import build
from services.risk_engine.engine import RiskEngine, load_rules
from swx_core import SwxStore, catalog

THREE_DAY = b""":Product: 3-Day Forecast
:Issued: 2026 Nov 19 1230 UTC
# Prepared by the U.S. Dept. of Commerce, NOAA, Space Weather Prediction Center
#
A. NOAA Geomagnetic Activity Observation and Forecast

NOAA Kp index breakdown Nov 19-Nov 21 2026

             Nov 19       Nov 20       Nov 21
00-03UT       5.00 (G1)    3.00         2.33
03-06UT       2.67         3.33         2.67

B. NOAA Solar Radiation Activity Observation and Forecast

Solar Radiation Storm Forecast for Nov 19-Nov 21 2026

              Nov 19  Nov 20  Nov 21
S1 or greater    5%      1%      1%

C. NOAA Radio Blackout Activity and Forecast

Radio Blackout Forecast for Nov 19-Nov 21 2026

              Nov 19        Nov 20        Nov 21
R1-R2           25%           15%           15%
R3 or greater    5%            5%            5%
"""

NOW = pd.Timestamp("2026-11-19T12:00Z")


def _write(store, param, times, values, *, dtype="OBS", source="test", tier=1,
           ingest="2026-11-19T12:00Z"):
    store.write(pd.DataFrame({
        "valid_time": pd.DatetimeIndex(times), "param_code": param, "value": values,
        "unit": "", "source_id": source, "source_tier": tier, "quality_flag": "good",
        "data_type": dtype,
    }), ingest_time=pd.Timestamp(ingest).to_pydatetime(), dedupe=False)


# ── 解析 ────────────────────────────────────────────────────────────────
def test_three_day_forecast_parser(tmp_path):
    c = build(catalog()["swpc_3day_forecast"], SwxStore(tmp_path))
    df = c.parse(THREE_DAY)
    got = {(r.param_code, r.valid_time.strftime("%m-%d")): round(r.value, 2)
           for r in df.itertuples()}
    assert got == {
        ("S1_PROB", "11-19"): 0.05, ("S1_PROB", "11-20"): 0.01, ("S1_PROB", "11-21"): 0.01,
        ("R12_PROB", "11-19"): 0.25, ("R12_PROB", "11-20"): 0.15, ("R12_PROB", "11-21"): 0.15,
        ("R3_PROB", "11-19"): 0.05, ("R3_PROB", "11-20"): 0.05, ("R3_PROB", "11-21"): 0.05,
    }
    assert set(df["data_type"]) == {"PRD"}
    assert "KP_3H" not in set(df["param_code"]), "Kp 由 swpc_geomag_forecast 提供，不得重複入庫"


def test_three_day_forecast_parser_handles_year_rollover(tmp_path):
    text = (THREE_DAY.replace(b"2026 Nov 19 1230", b"2026 Dec 31 1230")
            .replace(b"Nov 19  Nov 20  Nov 21", b"Dec 31  Jan 01  Jan 02")
            .replace(b"Nov 19        Nov 20        Nov 21", b"Dec 31        Jan 01        Jan 02"))
    df = build(catalog()["swpc_3day_forecast"], SwxStore(tmp_path)).parse(text)
    days = sorted(df["valid_time"].dt.strftime("%Y-%m-%d").unique())
    assert days == ["2026-12-31", "2027-01-01", "2027-01-02"]


def test_goes_electrons_parse_as_elec2(tmp_path):
    payload = json.dumps([
        {"time_tag": "2026-11-19T15:00:00Z", "satellite": 19, "flux": 1044.4, "energy": ">=2 MeV"},
        {"time_tag": "2026-11-19T15:05:00Z", "satellite": 19, "flux": 980.0, "energy": ">=2 MeV"},
    ]).encode()
    df = build(catalog()["swpc_electrons"], SwxStore(tmp_path)).parse(payload)
    assert list(df["param_code"].unique()) == ["ELEC2"]
    assert df["value"].round(1).tolist() == [1044.4, 980.0]


# ── 觀測優先於預測 ──────────────────────────────────────────────────────
def test_observation_beats_later_ingested_forecast_at_same_time(tmp_path):
    s = SwxStore(tmp_path)
    t = ["2026-11-18T03:00Z"]
    _write(s, "KP_3H", t, [2.0], source="celestrak_sw_all", ingest="2026-11-18T06:00Z")
    _write(s, "KP_3H", t, [3.67], dtype="PRD", source="swpc_geomag_forecast",
           ingest="2026-11-18T12:00Z")          # 同 tier、較晚入庫
    row = s.query("KP_3H").iloc[0]
    assert (row["value"], row["data_type"]) == (2.0, "OBS")
    # 要預測列者以 source_id 指定，仍取得到
    assert s.query("KP_3H", source_id="swpc_geomag_forecast")["value"].tolist() == [3.67]


def test_rules_are_not_triggered_by_forecast_values(tmp_path):
    s = SwxStore(tmp_path)
    t = pd.date_range("2026-11-19T00:00Z", periods=4, freq="3h")
    _write(s, "KP_3H", t[:2], [2.0, 2.0])                         # 觀測：平靜
    _write(s, "KP_3H", t[2:], [9.0, 9.0], dtype="PRD",           # 觀測尚未到，只有預報
           source="swpc_geomag_forecast")
    eps, _ = RiskEngine(s).evaluate(start=t[0].to_pydatetime(), end=t[-1].to_pydatetime())
    assert not [e for e in eps if e.peak_param == "KP_3H"], "預報 Kp 9 不得觸發規則"


def test_prediction_only_params_still_feed_pre_alerts(tmp_path):
    s = SwxStore(tmp_path)
    t = pd.date_range("2026-11-19T00:00Z", periods=3, freq="1D")
    _write(s, "X_FLARE_PROB", t, [0.5, 0.5, 0.5], dtype="PRD", source="swpc_solar_regions")
    eps, _ = RiskEngine(s).evaluate(start=t[0].to_pydatetime(), end=t[-1].to_pydatetime())
    assert any(e.rule_id == "HF-PRE-XPROB" for e in eps)


# ── 衛星健康規則 ────────────────────────────────────────────────────────
def _sat_eval(store, start, end):
    rules = [r for r in load_rules() if r.domain == "SATELLITE_HEALTH"]
    eps, status = RiskEngine(store, rules).evaluate(start=start, end=end)
    return {e.rule_id: e for e in eps}, status


def test_satellite_health_rules_are_loaded_and_routed():
    from services.notify.routing import load_routing

    rules = [r for r in load_rules() if r.domain == "SATELLITE_HEALTH"]
    assert len(rules) == 10
    assert {r.inference for r in rules if "KP_3H" in r.params} == {"proxy"}
    assert max(r.level for r in rules if r.inference == "proxy") == "L3", "代理判據上限 L3"
    routed = set(load_routing().recipients)
    assert {w for r in rules for w in r.notify} <= routed


@pytest.mark.parametrize("pfu,expected", [
    (50.0, set()), (150.0, {"SAT-L1-S2"}), (2000.0, {"SAT-L1-S2", "SAT-L2-S3"}),
    (2e4, {"SAT-L1-S2", "SAT-L2-S3", "SAT-L3-S4"}),
    (2e5, {"SAT-L1-S2", "SAT-L2-S3", "SAT-L3-S4", "SAT-L4-S5"}),
])
def test_proton_levels_follow_noaa_s_scale(tmp_path, pfu, expected):
    s = SwxStore(tmp_path)
    t = pd.date_range("2026-11-19T00:00Z", periods=6, freq="5min")
    _write(s, "PROT10", t, [pfu] * 6)
    got, _ = _sat_eval(s, t[0].to_pydatetime(), t[-1].to_pydatetime())
    assert {k for k in got if k.endswith(("S2", "S3", "S4", "S5"))} == expected


@pytest.mark.parametrize("hours,expected", [
    (0.1, set()),                                          # 10 分鐘：未達 15 分鐘駐留
    (1.0, {"SAT-L1-ELEC"}),
    (25.0, {"SAT-L1-ELEC", "SAT-L2-ELEC24"}),
    (73.0, {"SAT-L1-ELEC", "SAT-L2-ELEC24", "SAT-L3-ELEC72"}),
])
def test_electron_levels_follow_duration(tmp_path, hours, expected):
    s = SwxStore(tmp_path)
    end = pd.Timestamp("2026-11-19T12:00Z")
    t = pd.date_range(end - pd.Timedelta(hours=hours), end, freq="5min")
    _write(s, "ELEC2", t, [1500.0] * len(t))
    got, _ = _sat_eval(s, (end - pd.Timedelta(days=4)).to_pydatetime(), end.to_pydatetime())
    assert {k for k in got if "ELEC" in k} == expected


def test_72h_dwell_rule_can_fire_in_nowcast(tmp_path):
    """nowcast 預設回看 48 小時；不延伸的話 72 小時門檻永遠不可能成立。"""
    s = SwxStore(tmp_path)
    end = pd.Timestamp("2026-11-19T12:00Z")
    t = pd.date_range(end - pd.Timedelta(hours=74), end, freq="5min")
    _write(s, "ELEC2", t, [1500.0] * len(t))
    nc = RiskEngine(s).nowcast(as_of=end.to_pydatetime())
    row = nc[nc["domain"] == "SATELLITE_HEALTH"].iloc[0]
    assert row["level"] == "L3"


def test_surface_charging_is_proxy(tmp_path):
    s = SwxStore(tmp_path)
    t = pd.date_range("2026-11-19T00:00Z", periods=3, freq="3h")
    _write(s, "KP_3H", t, [9.0, 9.0, 9.0])
    got, _ = _sat_eval(s, t[0].to_pydatetime(), t[-1].to_pydatetime())
    assert got["SAT-L3-G5"].inference == "proxy"


# ── R/S/G 狀態 ──────────────────────────────────────────────────────────
@pytest.fixture
def rsg_store(tmp_path):
    s = SwxStore(tmp_path)
    # R：30 小時前 M5（R2）；之後平靜，最新一筆 5 分鐘前
    _write(s, "XRAY_LONG", [NOW - pd.Timedelta(hours=30), NOW - pd.Timedelta(minutes=5)],
           [6e-5, 2e-6])
    # S：只有 3 小時前的資料 → 「現在」過期
    _write(s, "PROT10", [NOW - pd.Timedelta(hours=3)], [0.5])
    # G：觀測 Kp 6（G2）2 小時前；同時刻另有較晚入庫的預報 Kp 4，不得蓋掉觀測
    #（若滲入，「現在」會變成平靜）。它屬於今日的 NOAA 預報，展望照算。
    _write(s, "KP_3H", [NOW - pd.Timedelta(hours=2)], [6.0])
    _write(s, "KP_3H", [NOW - pd.Timedelta(hours=2)], [4.0], dtype="PRD",
           source="swpc_geomag_forecast", ingest="2026-11-19T12:30Z")
    # 展望：NOAA Kp 預報與 R/S 機率
    _write(s, "KP_3H", [NOW + pd.Timedelta(hours=3), NOW + pd.Timedelta(hours=27)],
           [5.0, 3.0], dtype="PRD", source="swpc_geomag_forecast")
    probs = build(catalog()["swpc_3day_forecast"], s).parse(THREE_DAY)
    s.write(probs, ingest_time=NOW.to_pydatetime())
    return s


def test_rsg_observed_windows(rsg_store):
    st = products.rsg_status(rsg_store, now=NOW)
    r, sc, g = (st["observed"][k] for k in "RSG")
    assert r["past_48_24h"]["level"] == "R2"
    assert r["past_24h"] == {"level": None, "value": pytest.approx(2e-6), "has_data": True}
    assert r["current"]["has_data"] and r["current"]["level"] is None     # 平靜，不是無資料
    assert sc["current"]["has_data"] is False                              # 3 小時前：過期
    assert sc["past_48_24h"]["has_data"] is False
    assert g["current"]["level"] == "G2", "同時刻的預報值蓋掉了觀測"
    assert g["past_24h"]["value"] == 6.0


def test_rsg_outlook_uses_noaa_forecast(rsg_store):
    out = products.rsg_status(rsg_store, now=NOW)["outlook"]
    assert [d["date_utc"] for d in out] == ["2026-11-19", "2026-11-20", "2026-11-21"]
    assert out[0]["G"] == {"level": "G1", "kp_max": 5.0, "has_data": True}
    assert out[1]["G"]["level"] is None and out[1]["G"]["kp_max"] == 3.0
    assert out[2]["G"]["has_data"] is False
    assert out[0]["R"] == {"r1_r2_prob": 0.25, "r3_plus_prob": 0.05}
    assert out[2]["S"] == {"s1_plus_prob": 0.01}



def test_rsg_outlook_follows_newest_product_after_2200_issue(rsg_store):
    """NOAA 於 22:00 UTC 發布隔天起的三天。發布後到 UTC 換日前，展望要換成新的三天，
    不能固定從「今天」排起——否則第一欄是被取代的舊預報、真正的第三天被切掉
    （冷啟動實測：雲端只有最新一份時，第一欄整欄「無資料」）。"""
    newer = (THREE_DAY.replace(b"1230 UTC", b"2200 UTC")
             .replace(b"Nov 21", b"Nov 22").replace(b"Nov 20", b"Nov 21")
             .replace(b"Nov 19", b"Nov 20").replace(b"Issued: 2026 Nov 20", b"Issued: 2026 Nov 19"))
    late = pd.Timestamp("2026-11-19T22:30Z")
    probs = build(catalog()["swpc_3day_forecast"], rsg_store).parse(newer)
    rsg_store.write(probs, ingest_time=late.to_pydatetime())
    out = products.rsg_status(rsg_store, now=late)["outlook"]
    assert [d["date_utc"] for d in out] == ["2026-11-20", "2026-11-21", "2026-11-22"]
    assert out[0]["R"] == {"r1_r2_prob": 0.25, "r3_plus_prob": 0.05}, "應取新一份的第一天"
    assert out[2]["S"] == {"s1_plus_prob": 0.01}


def test_rsg_outlook_survives_write_dedupe(rsg_store):
    """新一份預報只有第三天的數值有變時，去重只寫入那一列；展望仍須是新一份的三天。

    長時間運作的站台實測：舊判定取「入庫時間最新的那批列」，結果顯示
    11-22～11-24、後兩天無資料（clone 只抓一次，所有列同批寫入，看不出來）。
    """
    # 20、21 日與舊預報同值（去重會略過），只有 22 日是新的
    newer = b""":Product: 3-Day Forecast
:Issued: 2026 Nov 19 2200 UTC
B. NOAA Solar Radiation Activity Observation and Forecast

Solar Radiation Storm Forecast for Nov 20-Nov 22 2026

              Nov 20  Nov 21  Nov 22
S1 or greater    1%      1%     10%

C. NOAA Radio Blackout Activity and Forecast

Radio Blackout Forecast for Nov 20-Nov 22 2026

              Nov 20        Nov 21        Nov 22
R1-R2           15%           15%           35%
R3 or greater    5%            5%           10%
"""
    late = pd.Timestamp("2026-11-19T22:30Z")
    probs = build(catalog()["swpc_3day_forecast"], rsg_store).parse(newer)
    res = rsg_store.write(probs, ingest_time=late.to_pydatetime())      # 預設去重
    assert res.rows_written == 3, "前提：去重只寫入 22 日的三列"
    out = products.rsg_status(rsg_store, now=late)["outlook"]
    assert [d["date_utc"] for d in out] == ["2026-11-20", "2026-11-21", "2026-11-22"]
    assert all(d["S"]["s1_plus_prob"] is not None for d in out), "三天都要有值"

def test_api_scales_endpoint(rsg_store, monkeypatch):
    monkeypatch.setattr(products.pd.Timestamp, "now",
                        classmethod(lambda cls, tz=None: NOW))
    body = create_app(rsg_store).test_client().get("/v1/scales").get_json()
    assert body["observed"]["G"]["current"]["level"] == "G2"
    assert len(body["outlook"]) == 3 and "data" in body



def test_gfz_hp30_nowcast_parse_and_url():
    """近即時 Hp30：負值為缺值碼；網址帶查詢期間（端點是樣板，不是固定網址）。"""
    from swx_core import SwxStore as _S

    conn = build(catalog()["gfz_hp30_nowcast"], _S())
    url = conn.endpoint_url()
    assert "{" not in url and "start=" in url and "index=Hp30" in url
    payload = b'{"datetime": ["2026-11-19T21:00:00Z", "2026-11-19T21:30:00Z", "2026-11-19T22:00:00Z"],'               b' "Hp30": [1.667, -1, 2.0], "meta": {}}'
    df = conn.parse(payload)
    assert list(df["value"]) == [1.667, 2.0]
    assert set(df["data_type"]) == {"OBS"} and set(df["param_code"]) == {"HP30"}
    assert catalog()["gfz_hp30_nowcast"].tier > catalog()["gfz_hp30"].tier, "定稿值須優先"
