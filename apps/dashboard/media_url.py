"""apps/dashboard/media_url.py — 影像網址的**單一**解析實作。

會有這個模組，是因為曾經有兩份：`app.py` 與 `stem.py` 各自組網址，
新增 `kind: latest_json` 時只改了前者。後果是 STEM 頁讀不到 `item['url']`
（該類影像根本沒有這個欄位），KeyError 被卡片的 `except Exception` 吞掉，
顯示成「載入失敗」——**看起來像對方站台掛了，其實是本地邏輯漏改**。

任何新增的影像類型只要加在這裡，兩個呼叫端就同時支援。
"""

from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st


@st.cache_data(ttl=300)
def _latest_json_image(latest_url: str, template: str) -> str:
    """由索引 JSON 的時刻組出影像網址。

    有些產製者不提供固定的 `latest.jpg`，只給一份帶時刻的索引
    （NICT 的向日葵影像即是）。此時必須先讀索引再組網址——
    寫死某個時刻的網址會在下一個更新週期就失效。
    """
    import json
    import urllib.request

    with urllib.request.urlopen(latest_url, timeout=25) as resp:
        date = json.load(resp)["date"]          # 'YYYY-MM-DD HH:MM:SS'
    return template.format(
        Y=date[0:4], M=date[5:7], D=date[8:10],
        hhmmss=date[11:13] + date[14:16] + date[17:19],
    )


def image_url(item: dict) -> str:
    """影像的實際網址。

    固定網址者加上快取破除參數：這些端點的檔名固定（latest.jpg），
    瀏覽器會沿用快取而顯示舊圖——使用者以為看的是即時影像，
    實際上可能是幾小時前的。以「當前時間對更新週期取整」當參數：
    同一個更新週期內共用快取（不浪費頻寬），跨週期就強制重抓。
    """
    if item.get("kind") == "latest_json":
        return _latest_json_image(item["latest_url"], item["url_template"])

    url = item.get("url")
    if not url:
        raise KeyError(f"影像 {item.get('id')} 既無 url 也非已知的動態類型")
    cadence = int(item.get("cadence_s") or 900)
    bucket = int(datetime.now(timezone.utc).timestamp() // cadence)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}_ts={bucket}"
