"""services.ingest.nlsc_egnss — e-GNSS 電離層誤差指標 I95（國土測繪中心）。

I95 是本案唯一有**作業單位公告門檻**的判據（>8 警戒、20–30 環境良好可嘗試、
持續 >30 應避開），也是唯一直接量到「RTK 在意的那個量」的資料：
它是 PIVOT 由基準站網觀測算出的網內電離層殘差指標，對應雙差後**消不掉的
空間梯度**——那正是整數模糊度固定失敗的原因。絕對 TEC 高不必然難固定，
梯度大才難固定，所以 TWTEC 那個全臺純量無法替代它。

**取得方式的三個限制**（決定了本模組為何長這樣）：

  1. 官方只以 PIVOT 產生的 **JPG 圖表**發布，沒有數值端點。
  2. 目錄列表被 WAF 阻擋、歷史檔名不可推測，因此**沒有回填管道**——
     歷史只能自開始輪詢之日起累積。
  3. 憑證鏈標準驗證失敗（與中央氣象署 SWOO 同源問題），需 tls_relaxed_strict。

因此本模組**由圖表擷取數值**，屬非官方衍生值，一律標 quality_flag=suspect
並降一級 tier。取得官方數值授權後，這個模組應該被丟掉，不是被改進。

擷取原理：圖上三條門檻線（Normal 2／Medium 4／High 8）是**已知值**，
用其中兩條就能把像素位置線性換算成指標值，不需要 OCR 讀座標軸——
座標軸上限每天不同（實測 20、21、46 都出現過），讀軸反而更脆弱。
"""

from __future__ import annotations

import io
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import numpy as np
import pandas as pd

from swx_core import empty_frame, normalize

from .base import Connector

#: 圖上兩條門檻線的已知值，用來把像素換算成指標值
_GREEN_VALUE = 2.0      # Normal Activity
_RED_VALUE = 8.0        # High Activity

#: 一根長條要被認定存在，該列的藍色像素必須占欄寬的比例。
#: 不設下限的話，標題文字的 JPEG 振鈴（1–3 個雜點）會被當成長條頂端，
#: 把 14.6 讀成 21.9——而且不會報錯，只會安靜地讀錯。
_BAR_FILL_RATIO = 0.6

#: 長條頂端距畫布上緣小於此值即視為被截斷（讀到的值會偏低）
_CLIP_MARGIN_PX = 2

_CHART_RE = re.compile(
    r'(?P<url>[^"\']*Pivot_Reports/[^"\']*?(?P<report>\d+)_i95__(?P<network>[A-Za-z_]+)'
    r'_RTCM\d+_(?P<start>\d{14})%20-%20(?P<end>\d{14})\.jpg)',
    re.IGNORECASE,
)


def _masks(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """紅／綠／藍三個顏色遮罩。

    門檻用相對關係而非絕對色值：JPEG 壓縮後紅線實測為 (142,48,48)、
    綠線 (46,94,46)，用「純紅／純綠」去比對會一個都找不到。
    """
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    red = (r > g + 40) & (r > b + 40)
    green = (g > r + 20) & (g > b + 20)
    blue = (b > 120) & (b > r + 60) & (b > g + 60)
    return red, green, blue


def _line_center(mask: np.ndarray) -> tuple[int, float]:
    """門檻線的 (峰值列, 線中心列)。

    線寬約 2 像素，只取峰值列會有半像素的系統偏差；兩條線各偏一邊時，
    定標的跨距就少 1 像素。這個誤差會**隨外推距離放大**——實測用峰值列
    讀澎湖網的 46 會變成 46.9。取線中心可消掉大部分。
    """
    counts = mask.sum(axis=1)
    peak = int(counts.argmax())
    near = [r for r in np.flatnonzero(counts >= 0.6 * counts[peak]) if abs(r - peak) <= 3]
    return peak, float(np.mean(near)) if near else float(peak)


def extract_i95(image: bytes) -> dict[int, float]:
    """由 PIVOT 的 I95 圖表擷取逐時值，回傳 {GPS 時: 指標值}。

    只回傳**畫得出長條**的小時；當日尚未產生的時段不補值也不回 0
    ——回 0 會被讀成「電離層很平靜」，而事實是「還沒有這個時段的資料」。
    """
    from PIL import Image                      # 延後匯入：API 與規則引擎不需要它

    rgb = np.asarray(Image.open(io.BytesIO(image)).convert("RGB")).astype(int)
    red, green, blue = _masks(rgb)

    if not red.any() or not green.any():
        return {}
    red_peak, y_red = _line_center(red)
    _green_peak, y_green = _line_center(green)
    if y_green <= y_red:                       # 綠線（值小）必須在紅線下方
        return {}

    # 門檻線橫跨繪圖區，其左右端點即為 x 軸範圍——比找黑色框線穩定
    xs = np.flatnonzero(red[red_peak])
    x0, x1 = int(xs.min()), int(xs.max())
    if x1 - x0 < 24:
        return {}

    scale = (_RED_VALUE - _GREEN_VALUE) / (y_green - y_red)     # 每像素幾個指標單位

    out: dict[int, float] = {}
    for hour in range(24):
        xa = int(x0 + (x1 - x0) * hour / 24) + 2
        xb = int(x0 + (x1 - x0) * (hour + 1) / 24) - 1
        column = blue[:, xa:xb]
        if column.size == 0:
            continue
        rows = np.flatnonzero(column.sum(axis=1) >= _BAR_FILL_RATIO * column.shape[1])
        if not rows.size:
            continue
        top = int(rows.min())
        if top <= _CLIP_MARGIN_PX:
            # 長條被畫布切掉：PIVOT 會自動縮放座標軸使最高的長條放得下，
            # 所以這只會在版面改變時發生。此時讀到的值必然**偏低**——
            # 而偏低正是最危險的方向（把該避開的時段讀成可作業）。
            raise ValueError(f"{hour} 時的長條被畫布截斷，版面可能已改版")
        out[hour] = round(_GREEN_VALUE + (y_green - top) * scale, 1)
    return out


class NlscEgnssConnector(Connector):
    """由 e-GNSS 服務網頁取得三個網（本島 VRS／金門／澎湖）的 I95。

    **注意 reparse 的語意**：原始落地存的是 HTML 頁面，圖檔另外抓取。
    重新解析同一份 HTML 會去抓**當下**的圖，而不是當時那批——因為圖檔 URL
    帶時間窗，舊窗的檔案在站上已不存在（實測回 404）。故本來源的重解析
    只適用於「解析規則改了、圖仍是同一批」的情境。
    """

    formats = ("nlsc_egnss_html",)
    raw_ext = "html"

    def _chart_urls(self, payload: bytes) -> list[tuple[str, str, datetime]]:
        """回傳 [(網別, 圖檔絕對網址, 該圖的日期)]，同一網只取一張。"""
        html = payload.decode("utf-8", errors="replace")
        networks = {str(k): v for k, v in (self.spec.raw.get("networks") or {}).items()}
        seen: dict[str, tuple[str, str, datetime]] = {}
        for m in _CHART_RE.finditer(html):
            report = m.group("report")
            name = networks.get(report, m.group("network"))
            if name in seen:
                continue
            # 檔名的時間窗 <前一日>235942 - <當日>235942：長條的小時屬**窗末那一天**
            day = datetime.strptime(m.group("end"), "%Y%m%d%H%M%S").replace(
                hour=0, minute=0, second=0, tzinfo=timezone.utc)
            seen[name] = (name, urljoin(self.spec.endpoint or "", m.group("url")), day)
        return list(seen.values())

    def parse(self, payload: bytes) -> pd.DataFrame:
        charts = self._chart_urls(payload)
        if not charts:
            return empty_frame()

        recs: list[dict] = []
        errors: list[str] = []
        for name, url, day in charts:
            try:
                values = extract_i95(self.fetch_related(url))
                if not values:
                    raise RuntimeError("圖表無可辨識的長條（版面可能已改版）")
            except Exception as exc:                          # noqa: BLE001
                # 單一網失敗不該讓另外兩個網也沒有資料，但**必須說出來**：
                # 部分成功仍回報 ok，不出聲的話缺的那個網會安靜消失。
                errors.append(f"{name}: {type(exc).__name__}")
                self.warn(f"{name} 的 I95 未取得（{type(exc).__name__}）")
                continue

            for hour, value in values.items():
                recs.append({
                    "valid_time": day + timedelta(hours=hour),
                    "param_code": "I95",
                    "value": value,
                    "unit": "1",
                    # 分網儲存。三個網差異可以很大（2026-08-26 00Z 澎湖 46、
                    # 本島 4），取全國單一值會同時誤導兩邊。
                    "grid_id": name,
                    "source_id": self.spec.source_id,
                    # tier 2 而非來源宣告的 1：資料產製者是權威的，
                    # **本案的圖表擷取不是**。降一級讓它在跨源比對時
                    # 讓位給日後取得的官方數值。
                    "source_tier": 2,
                    "data_type": "OBS",
                    "quality_flag": "suspect",
                    "quality_reason": "由官方圖表擷取之非官方衍生值（精度約 ±0.5）",
                })

        if errors and not recs:
            raise RuntimeError(f"三個網的 I95 圖表皆無法取得：{'; '.join(errors)}")
        if not recs:
            return empty_frame()
        return normalize(pd.DataFrame(recs))
