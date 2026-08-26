"""e-GNSS I95 圖表擷取。

**這組測試守的是換算邏輯，不是「擷取得到官方的真值」。** 圖是合成的：
官方圖檔屬授權未確認的資料，不進版控（見 configs/sources.yaml 的 terms），
所以測試自己畫一張版面相同的圖，驗證像素→指標值的換算、雜點抵抗力與
「沒有長條就不要編值」這三件事。真實端點改版只有 `tools/i95_smoke.py` 會紅燈
——這與 media_smoke 的分工相同：單元測試不連網。

換算原理：圖上 Normal 2／High 8 兩條門檻線是已知值，用它們定標，
不必 OCR 讀座標軸——軸上限每天不同（實測 20、21、46 都出現過）。
"""

from __future__ import annotations

import io

import pytest

from services.ingest.nlsc_egnss import extract_i95

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw           # noqa: E402

# 版面常數取自實際圖檔（640×480，繪圖區 x 81–602）
W, H = 640, 480
X0, X1 = 81, 602
Y_RED, Y_GREEN = 272, 378                  # 值 8 與值 2 的像素列


def _chart(values: dict[int, float], *, noise: bool = False,
           y_red: int = Y_RED, y_green: int = Y_GREEN) -> bytes:
    """畫一張版面與 PIVOT 相同的 I95 圖。

    顏色刻意用 JPEG 壓縮後的實測值（紅 142,48,48／綠 46,94,46），
    而不是純紅純綠——用純色畫的話，測試會通過而真實圖檔一個都抓不到。
    """
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    per_unit = (y_green - y_red) / 6.0     # 每個指標單位幾個像素

    def y_of(value: float) -> int:
        return int(round(y_green - (value - 2.0) * per_unit))

    for hour, value in values.items():
        xa = int(X0 + (X1 - X0) * hour / 24) + 3
        xb = int(X0 + (X1 - X0) * (hour + 1) / 24) - 2
        d.rectangle([xa, y_of(value), xb, y_green + 40], fill=(0, 0, 255))

    d.line([X0, y_red, X1, y_red], fill=(142, 48, 48), width=2)
    d.line([X0, y_green, X1, y_green], fill=(46, 94, 46), width=2)
    d.line([X0, y_of(4.0), X1, y_of(4.0)], fill=(255, 255, 100), width=2)

    if noise:
        # 標題文字的 JPEG 振鈴：實際圖檔在 y≈26 有 1–3 個藍色雜點，
        # 曾使「最上方的藍色像素」把 14.6 讀成 21.9
        for x in (240, 290, 305):
            d.point((x, 26), fill=(0, 0, 255))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def test_values_round_trip_through_the_chart():
    want = {0: 4.0, 3: 9.5, 6: 15.5, 11: 21.0}
    got = extract_i95(_chart(want))
    assert set(got) == set(want)
    for hour, value in want.items():
        assert got[hour] == pytest.approx(value, abs=0.3), f"{hour} 時"


def test_hours_without_bars_are_absent_not_zero():
    """當日尚未產生的時段不得補 0——0 會被讀成「電離層很平靜」。"""
    got = extract_i95(_chart({0: 5.0, 1: 6.0}))
    assert set(got) == {0, 1}
    assert 12 not in got


def test_title_speckle_does_not_become_a_bar_top():
    """標題的雜點若被當成長條頂端，會把 14 讀成 21——而且不會報錯。"""
    want = {5: 14.5}
    got = extract_i95(_chart(want, noise=True))
    assert got[5] == pytest.approx(14.5, abs=0.3)
    assert set(got) == {5}, f"雜點被誤判為長條：{got}"


def test_high_values_beyond_the_usual_axis_are_read():
    """澎湖網實測出現過 46。

    PIVOT 會自動縮放座標軸讓最高的長條放得下，所以此時門檻線的間距不同
    （實測本島 17.7 px/單位、澎湖 8.0）。定標線的用意正在於此——
    讀軸上限會被這種縮放騙到，讀門檻線不會。
    """
    got = extract_i95(_chart({0: 46.0}, y_red=350, y_green=398))
    assert got[0] == pytest.approx(46.0, abs=0.5)


def test_clipped_bar_raises_instead_of_reporting_a_low_value():
    """長條被畫布切掉時讀到的值必然偏低——偏低是最危險的方向。"""
    with pytest.raises(ValueError, match="截斷"):
        extract_i95(_chart({0: 46.0}))          # 用本島的軸距畫 46，必然超出畫布


def test_missing_threshold_lines_yield_nothing():
    """沒有定標線就無法換算。回空表，而不是猜一個座標軸。"""
    img = Image.new("RGB", (W, H), "white")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    assert extract_i95(buf.getvalue()) == {}


def test_chart_urls_are_mapped_to_network_names():
    """圖檔的報表編號要對應到網別；對不上就無法分網儲存。"""
    from swx_core import SourceSpec, SwxStore

    from services.ingest.nlsc_egnss import NlscEgnssConnector

    spec = SourceSpec(
        source_id="nlsc_egnss_i95", name="e-GNSS", connector="nlsc_egnss", tier=1,
        status="ready", provides=("I95",), cadence_s=3600, latency_budget_s=None,
        endpoint="https://example.invalid/rtkstatus.aspx", fmt="nlsc_egnss_html",
        local_fallback=None, fallback=(), notes=None, publication_lag_s=0,
        raw={"networks": {"22": "RTKVRSNet", "25": "Peng_Hu"}},
    )
    html = (
        '<img src="NFS/Pivot_Reports/Year.26/Month.Aug/Day.26/'
        '22_i95__RTKVRSNet_RTCM31_20260825235942%20-%2020260826235942.jpg">'
        '<img src="NFS/Pivot_Reports/Year.26/Month.Aug/Day.26/'
        '25_i95__Peng_Hu_RTCM31_20260825235942%20-%2020260826235942.jpg">'
    ).encode()

    conn = NlscEgnssConnector(spec, SwxStore.__new__(SwxStore))
    charts = conn._chart_urls(html)
    names = {c[0] for c in charts}
    assert names == {"RTKVRSNet", "Peng_Hu"}
    for _, url, day in charts:
        assert url.startswith("https://example.invalid/NFS/Pivot_Reports/")
        # 檔名的時間窗跨兩天，長條的小時屬**窗末**那一天
        assert day.strftime("%Y-%m-%d") == "2026-08-26"
