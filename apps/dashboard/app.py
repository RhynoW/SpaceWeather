"""apps/dashboard/app.py — SWX-SDA 太空天氣儀表板（架構書 §11）。

設計原則沿用架構書 P5：**「沒資料」與「沒事」必須分得清楚**。
所有面板都顯示資料齡期；缺資料的網域顯示灰色「無資料」而非綠色「正常」，
因為綠燈會讓值勤人員誤以為已經確認過該網域無異常。

啟動：
    streamlit run apps/dashboard/app.py

資料來源可切換：
    直接讀資料層（預設，免啟動 API）
    或設定 SWX_API_BASE 走 REST API（驗證 API 契約用）
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "packages"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pandas as pd  # noqa: E402
import plotly.express as px  # noqa: E402
import plotly.graph_objects as go  # noqa: E402
import streamlit as st  # noqa: E402

import services  # noqa: E402,F401
from services.exporter import drag_correction, stk_spaceweather  # noqa: E402
from services.risk_engine.engine import RiskEngine, load_rules  # noqa: E402
from services.risk_engine.eventcard import build_event_cards  # noqa: E402
from swx_core import (  # noqa: E402
    SwxStore, catalog, data_origin, imagery, quality_summary, registry,
)
from swx_core.flare import flux_to_class, r_scale  # noqa: E402

st.set_page_config(page_title="SWX-SDA 太空天氣儀表板", page_icon="🛰", layout="wide")

LEVEL_COLOR = {
    "L0": "#2e7d32", "L1": "#f9a825", "L2": "#ef6c00",
    "L3": "#d84315", "L4": "#b71c1c", "—": "#616161",
}
LEVEL_NAME = {"L0": "正常", "L1": "注意", "L2": "警戒", "L3": "嚴重", "L4": "重大", "—": "無資料"}
DOMAIN_NAME = {
    "ORBIT_PREDICTION": "軌道預報", "GNSS_PNT": "GNSS 定位授時",
    "HF_COMM": "HF 通信", "VHF_UHF": "VHF/UHF", "SATCOM": "衛星通信",
}


@st.cache_resource
def get_store() -> SwxStore:
    return SwxStore()


@st.cache_data(ttl=300)
def load_series(param: str, days: int) -> pd.DataFrame:
    store = get_store()
    end = datetime.now(timezone.utc)
    df = store.query(param, start=end - timedelta(days=days), end=end + timedelta(days=3))
    return df


@st.cache_data(ttl=300)
def load_health() -> pd.DataFrame:
    return get_store().health()


@st.cache_data(ttl=300)
def load_nowcast() -> pd.DataFrame:
    return RiskEngine(get_store()).nowcast()


@st.cache_data(ttl=300)
def load_episodes(days: int):
    end = datetime.now(timezone.utc)
    engine = RiskEngine(get_store())
    eps, status = engine.evaluate(start=end - timedelta(days=days), end=end)
    return pd.DataFrame([e.to_dict() for e in eps]), status


@st.cache_data(ttl=300)
def load_event_cards(days: int) -> list[dict]:
    """近 N 日的事件卡（dict 形式），供值勤模式與事件卡頁共用。"""
    end = datetime.now(timezone.utc)
    engine = RiskEngine(get_store())
    eps, _status = engine.evaluate(start=end - timedelta(days=days), end=end)
    if not eps:
        return []
    cards = build_event_cards(eps, store=get_store())
    return [c.to_dict() for c in cards]


def age_badge(age_s: float | None) -> str:
    if age_s is None or pd.isna(age_s):
        return "⬜ 無資料"
    if age_s < 3600:
        return f"🟢 {age_s / 60:.0f} 分鐘前"
    if age_s < 86400:
        return f"🟡 {age_s / 3600:.1f} 小時前"
    return f"🔴 {age_s / 86400:.1f} 天前"





# ── 影像呈現 ────────────────────────────────────────────────────────────
def image_url(item: dict) -> str:
    """加上快取破除參數。

    這些端點的檔名固定（latest.jpg），瀏覽器會沿用快取而顯示舊圖——
    使用者以為看的是即時影像，實際上可能是幾小時前的。
    以「當前時間對更新週期取整」當參數：同一個更新週期內共用快取（不浪費頻寬），
    跨週期就強制重抓。
    """
    cadence = int(item.get("cadence_s") or 900)
    bucket = int(datetime.now(timezone.utc).timestamp() // cadence)
    sep = "&" if "?" in item["url"] else "?"
    return f"{item['url']}{sep}_ts={bucket}"


def _attr_line(item: dict) -> str:
    a = item.get("attribution", {})
    return (f"來源：{a.get('provider', '未標註')}　"
            f"[原始網址]({a.get('url', '')})　｜{a.get('terms', '')}")


def render_image_card(item: dict, *, compact: bool = False) -> None:
    """單張影像卡。**來源標註與影像同框**，不摺疊、不移到頁尾。"""
    st.markdown(f"**{item['title']}**")
    st.caption(item.get("instrument", ""))
    try:
        st.image(image_url(item), width='stretch')
    except Exception:
        # 封閉網路或對方站台異常時，明確說「載入失敗」而非留白
        st.warning(f"影像無法載入：{item.get('instrument', item['title'])}")
    if not compact and item.get("note"):
        st.markdown(
            f"<div style='font-size:13px;line-height:1.6'>{item['note']}</div>",
            unsafe_allow_html=True)
    st.caption(_attr_line(item))


@st.cache_data(ttl=600)
def _imagery_safe() -> list[dict]:
    try:
        return imagery()
    except Exception:
        return []


def images_by_id(*ids: str) -> list[dict]:
    index = {i["id"]: i for i in _imagery_safe()}
    return [index[i] for i in ids if i in index]


def render_current_sun() -> None:
    """當前太陽三連圖：黑子、日珥、磁圖。

    這三個波段刻意選在一起：白光看**有沒有黑子**、304Å 看**會不會噴**、
    磁圖看**極性複不複雜**。三者合起來才回答「未來幾天風險高不高」，
    任何單一張都不夠。
    """
    items = images_by_id("sdo_white_light", "sdo_euv_304", "sdo_magnetogram")
    if not items:
        return
    st.subheader("當前太陽")
    st.caption("白光看黑子有無｜304Å 看日珥會不會噴｜磁圖看極性是否複雜——三者合看才判斷得了未來風險")
    for col, item in zip(st.columns(len(items)), items):
        with col:
            render_image_card(item, compact=True)


def render_solar_wind_sim() -> None:
    """太陽風／CME 傳播模擬。系統中少數具 1–3 天提前量的資訊。"""
    items = images_by_id("swpc_enlil", "swpc_geospace_velocity")
    if not items:
        return
    st.subheader("太陽風傳播模擬")
    st.caption(
        "**模式輸出，不是觀測。** 判讀重點是 CME 前緣何時抵達地球（圖中 Earth 標記）——"
        "這是本系統少數具 1–3 天提前量的資訊，但抵達時間典型誤差達數小時至十餘小時。"
    )
    for col, item in zip(st.columns(len(items)), items):
        with col:
            render_image_card(item, compact=True)

# ── 背景自動更新 ────────────────────────────────────────────────────────
# 為什麼用背景執行緒而不是直接呼叫：完整擷取實測約 47 秒，
# 放在頁面載入路徑上會讓每次互動都卡住。改成背景跑，
# 頁面立刻以現有資料渲染，新資料在下一次互動時出現。
#
# 為什麼不用排程器：Streamlit Cloud 容器會因閒置休眠，沒有可靠的常駐排程。
# 「有人開頁面時才確保資料夠新」正好符合展示層的需求，也不浪費資源。
REFRESH_MAX_AGE_MIN = 60.0


@st.cache_resource
def _refresh_state() -> dict:
    """跨 session 共用的更新狀態（cache_resource 在整個 app 只有一份）。"""
    return {"thread": None, "result": None, "progress": None, "error": None}


def _run_refresh(state: dict, *, force: bool, include_heavy: bool) -> None:
    from services.ingest.refresh import refresh_if_stale

    def progress(i, n, sid):
        state["progress"] = (i, n, sid)

    try:
        state["result"] = refresh_if_stale(
            max_age_s=REFRESH_MAX_AGE_MIN * 60.0, force=force,
            include_heavy=include_heavy, on_progress=progress,
        )
        state["error"] = None
    except Exception as exc:      # noqa: BLE001 - 背景執行緒不可讓例外逃逸
        state["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        state["progress"] = None
        # 資料換新後，既有的 store 與各頁快取都要作廢，否則畫面仍是舊值
        st.cache_data.clear()
        get_store.clear()


def start_refresh(*, force: bool = False, include_heavy: bool = False) -> bool:
    """啟動背景更新。已在執行中則不重複啟動。"""
    import threading

    state = _refresh_state()
    thread = state.get("thread")
    if thread is not None and thread.is_alive():
        return False
    t = threading.Thread(target=_run_refresh, args=(state,),
                         kwargs={"force": force, "include_heavy": include_heavy},
                         daemon=True)
    state["thread"] = t
    t.start()
    return True


def refresh_status_line() -> None:
    """側欄顯示資料齡期與更新狀態，並提供手動更新。"""
    from services.ingest.refresh import data_age_s, live_sources

    state = _refresh_state()
    thread = state.get("thread")
    running = thread is not None and thread.is_alive()

    try:
        age = data_age_s()
    except Exception:
        age = None

    if age is None:
        st.sidebar.markdown("**資料齡期**　⚪ 尚無資料")
    else:
        mins = age / 60.0
        icon = "🟢" if mins <= REFRESH_MAX_AGE_MIN else "🟡"
        label = f"{mins:.0f} 分鐘前" if mins < 120 else f"{mins / 60:.1f} 小時前"
        st.sidebar.markdown(f"**資料齡期**　{icon} {label}")

    if running:
        prog = state.get("progress")
        st.sidebar.caption(
            f"更新中… {prog[0]}/{prog[1]}　{prog[2]}" if prog else "更新中…"
        )
    else:
        result = state.get("result")
        if state.get("error"):
            st.sidebar.error(f"更新失敗：{state['error'][:80]}")
        elif result is not None and result.ran:
            st.sidebar.caption(f"上次更新：{result.summary()}")

    c1, c2 = st.sidebar.columns(2)
    if c1.button("更新", disabled=running, width='stretch',
                 help=f"重新擷取 {len(live_sources())} 個近即時來源（約 30–50 秒，背景執行）"):
        start_refresh(force=True)
        st.rerun()
    if c2.button("完整", disabled=running, width='stretch',
                 help="另含 gfz_hp30（30 分鐘解析地磁指數，單獨約 46 秒）"):
        start_refresh(force=True, include_heavy=True)
        st.rerun()


def autostart_refresh() -> None:
    """開站時檢查一次：資料齡期超過門檻就在背景更新。"""
    state = _refresh_state()
    if state.get("checked"):
        return
    state["checked"] = True
    try:
        from services.ingest.refresh import data_age_s

        age = data_age_s()
    except Exception:
        return
    # age is None（雲端冷啟動，只有示範快照）也要更新——那正是最該更新的時候
    if age is None or age > REFRESH_MAX_AGE_MIN * 60.0:
        start_refresh(force=False)

# ── 現況橫幅 ────────────────────────────────────────────────────────────
# 以階梯狀色塊呈現四類現象的當前強度。用階梯而非單一色球，是因為
# 「現在是第幾階、離下一階多遠」比「現在是什麼顏色」更有判讀價值。
BANNER_ITEMS = [
    # (標題, 參數, 階梯門檻由低到高, 階梯標籤, 單位說明)
    ("太陽閃焰", "XRAY_LONG", [1e-5, 5e-5, 1e-4, 1e-3],
     ["R1", "R2", "R3", "R4+"], "GOES X 射線"),
    ("太陽質子", "PROT10", [10, 100, 1000, 10000],
     ["S1", "S2", "S3", "S4+"], "≥10 MeV"),
    ("地磁擾動", "KP_3H", [5, 6, 7, 8],
     ["G1", "G2", "G3", "G4+"], "Kp"),
    ("HF 吸收", "DRAP_TW_MHZ", [5, 10, 15, 20],
     ["輕", "中", "重", "極重"], "臺灣周邊 MHz"),
]
BANNER_COLORS = ["#2E7D32", "#F9A825", "#EF6C00", "#C62828"]


def _banner_level(value, thresholds) -> int:
    """回傳達到的階數（0 = 未達第一階）。"""
    if value is None or pd.isna(value):
        return -1                      # -1 代表無資料，與 0（平靜）不同
    n = 0
    for t in thresholds:
        if float(value) >= t:
            n += 1
    return n


def render_status_banner(store, *, hours: int = 6) -> None:
    """四類現象的當前強度階梯。無資料顯示灰階，不顯示綠色。"""
    now = datetime.now(timezone.utc)
    cols = st.columns(len(BANNER_ITEMS))
    for col, (title, code, thresholds, labels, unit) in zip(cols, BANNER_ITEMS):
        df = store.query(code, start=now - timedelta(hours=hours), end=now)
        value = None if df.empty else float(df.sort_values("valid_time").iloc[-1]["value"])
        lvl = _banner_level(value, thresholds)

        with col:
            if lvl < 0:
                head = "<span style='color:#9E9E9E'>無資料</span>"
            elif lvl == 0:
                head = "<span style='color:#2E7D32'>平靜</span>"
            else:
                head = f"<span style='color:{BANNER_COLORS[lvl - 1]}'>{labels[lvl - 1]}</span>"
            st.markdown(
                f"<div style='font-size:13px;color:#9aa4b2'>{title}"
                f"<span style='float:right'>{unit}</span></div>"
                f"<div style='font-size:20px;font-weight:700;margin:2px 0 6px'>{head}</div>",
                unsafe_allow_html=True,
            )
            # 由高到低堆疊，最高階在上（與強度直覺一致）
            rungs = []
            for i in range(len(thresholds) - 1, -1, -1):
                on = lvl >= i + 1
                bg = BANNER_COLORS[i] if on else ("#3a3f47" if lvl >= 0 else "#2b2f35")
                fg = "#fff" if on else "#6b7280"
                rungs.append(
                    f"<div style='background:{bg};color:{fg};font-size:11px;"
                    f"text-align:center;padding:3px 0;margin-bottom:2px;"
                    f"border-radius:2px'>{labels[i]}</div>"
                )
            st.markdown("".join(rungs), unsafe_allow_html=True)
            st.caption("—" if value is None else f"{value:.3g}")

# ── 側欄 ────────────────────────────────────────────────────────────────
_ORIGIN = data_origin()
if _ORIGIN["is_demo"]:
    st.warning(
        "⚠️ **DEMO DATA — NOT OPERATIONAL**　"
        f"本站台使用示範快照（產製於 {_ORIGIN['snapshot_time'] or '未知時刻'}），"
        "**不是即時作業資料**。快照內的資料在其自身時間軸上看起來是新的，"
        "但實際上不會更新——請勿據以做任何作業判斷。"
    )

st.sidebar.title("🛰 SWX-SDA")
st.sidebar.caption("太空天氣整合資訊與 SDA 應用系統")
page = st.sidebar.radio(
    "頁面",
    ["值勤模式", "太空環境總覽", "太陽與行星際影像", "參數時序", "事件卡",
     "太陽閃焰", "48 小時預報", "地磁基準場", "軌道與密度修正",
     "資料健康", "門檻校準", "名詞與判讀", "使用指南"],
)
lookback = st.sidebar.slider("回顧天數", 1, 60, 7)
st.sidebar.divider()
autostart_refresh()
refresh_status_line()
st.sidebar.divider()
st.sidebar.caption(
    "設計原則：缺資料顯示灰色「無資料」而非綠色「正常」——\n"
    "綠燈會讓人誤以為已確認該網域無異常。"
)


# ── 0. 值勤模式（一屏掌握全局）──────────────────────────────────────────
if page == "值勤模式":
    from swx_core.interpret import (
        BAND_ALERT, BAND_NOTABLE, BAND_QUIET, BAND_UNKNOWN, GUIDANCE,
    )

    st.title("值勤模式")
    st.caption(
        f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC　"
        "設計目標：事件發生時 10 秒內掌握全局，不必逐頁切換"
    )

    store = get_store()
    reg = registry()
    now = datetime.now(timezone.utc)

    render_status_banner(store)
    st.divider()

    # ── 第一列：三網域燈號 ──────────────────────────────────────────
    # 「無資料」與「正常」必須用不同顏色。綠燈代表已確認無異常，
    # 沒有資料時給綠燈是這套系統最不能犯的錯。
    nowcast = load_nowcast()
    INFER_NOTE = {
        "observed": "", "modelled": "（模型推算）",
        "proxy": "（間接推估）", "unavailable": "",
    }
    cols = st.columns(max(len(nowcast), 1))
    for col, (_, row) in zip(cols, nowcast.iterrows()):
        lvl = str(row.get("level", "—"))
        available = bool(row.get("data_available", False))
        infer = str(row.get("inference") or "observed")
        with col:
            if not available or lvl == "—":
                st.markdown(
                    f"<div style='padding:14px;border-radius:8px;background:#3a3a3a;"
                    f"border-left:6px solid #888'><b>{row['domain']}</b><br>"
                    f"<span style='font-size:2em'>無資料</span><br>"
                    f"<small>不代表無風險</small></div>",
                    unsafe_allow_html=True,
                )
            else:
                color = LEVEL_COLOR.get(lvl, "#888")
                st.markdown(
                    f"<div style='padding:14px;border-radius:8px;background:#2b2b2b;"
                    f"border-left:6px solid {color}'><b>{row['domain']}</b><br>"
                    f"<span style='font-size:2em;color:{color}'>{lvl}</span>"
                    f"<br><small>{row.get('active_rules', '—')} {INFER_NOTE.get(infer, '')}</small></div>",
                    unsafe_allow_html=True,
                )

    st.divider()
    left, right = st.columns([3, 2])

    # ── 左：最近事件卡 ──────────────────────────────────────────────
    with left:
        st.subheader("最近事件")
        try:
            events = load_event_cards(7)
        except Exception as exc:      # 資料層異常不應讓整頁掛掉
            events = []
            st.warning(f"事件卡載入失敗：{type(exc).__name__}")
        if not events:
            st.info("近 7 日無事件卡。**這代表規則未觸發，不代表所有通道都有資料**——右側資料齡期為準。")
            if _ORIGIN["is_demo"]:
                # 示範站台多半處於平靜期，事件欄位會是空的。導向歷史案例，
                # 但**不把 2024 年的事件偽裝成近期事件**——那正是本系統要防的誤讀。
                st.caption(
                    "示範站台通常落在平靜期。要看事件卡的實際樣貌，"
                    "請至「事件卡」頁選擇期間預設 **2024-05 Gannon G5**"
                    "（該事件判為 L4／G4，含間接推估分項與密度修正產品）。"
                )
        else:
            for card in events[:4]:
                lvl = card.get("mission_level", "—")
                scale = card.get("international_scale")
                status = card.get("status", "draft")
                badge = {"draft": "📝 待人工確認", "issued": "✅ 已發布",
                         "superseded": "🗄 已被取代"}.get(status, status)
                head = f"**{lvl}**" + (f"　國際 {scale}" if scale else "")
                with st.container(border=True):
                    c1, c2 = st.columns([3, 2])
                    c1.markdown(f"{head}　{card.get('type', '')}")
                    c2.markdown(f"<div style='text-align:right'>{badge}</div>",
                                unsafe_allow_html=True)
                    tl = card.get("timeline", {})
                    st.caption(
                        f"`{card.get('event_id', '')}`　起始 {tl.get('onset_utc', '—')}"
                        f"　持續 {tl.get('duration_h', '—')} h"
                        f"　可信度 {card.get('confidence', '—')}"
                    )
                    recs = card.get("recommendations") or []
                    if recs:
                        st.markdown("　".join(f"▸ {r}" for r in recs[:2]))
                    impacts = card.get("impacts") or []
                    proxy_n = sum(1 for i in impacts if i.get("inference") == "proxy")
                    if proxy_n:
                        st.warning(f"{proxy_n} 個影響分項為**間接推估**，非直接觀測")
            if len(events) > 4:
                st.caption(f"另有 {len(events) - 4} 筆，詳見「事件卡」頁")

    # ── 右：資料齡期警報 + 關鍵指標 ─────────────────────────────────
    with right:
        st.subheader("資料齡期")
        stale = []
        for code in ("KP_3H", "DST", "IMF_BZ", "XRAY_LONG", "PROT10", "DRAP_TW_MHZ"):
            if code not in reg:
                continue
            df = store.query(code, start=now - timedelta(days=3), end=now)
            spec = reg[code]
            if df.empty:
                stale.append((code, None, True))
                continue
            age = (now - df["valid_time"].max()).total_seconds()
            bad = bool(spec.cadence_s and age > 5 * spec.cadence_s)
            stale.append((code, age, bad))
        for code, age, bad in stale:
            label = reg[code].name_zh if code in reg else code
            if age is None:
                st.markdown(f"⚪ **{label}**　無資料")
            else:
                st.markdown(f"{'🔴' if bad else '🟢'} **{label}**　{age_badge(age)}")

        st.subheader("關鍵指標")
        badge = {BAND_QUIET: "🟢", BAND_NOTABLE: "🟡", BAND_ALERT: "🔴", BAND_UNKNOWN: "⚪"}
        rows = []
        for code in ("KP_3H", "DST", "IMF_BZ", "SW_V", "XRAY_LONG"):
            g = GUIDANCE.get(code)
            if g is None:
                continue
            df = store.query(code, start=now - timedelta(days=2), end=now)
            v = None if df.empty else float(df.sort_values("valid_time").iloc[-1]["value"])
            band = g.band(v)
            rows.append({"": badge[band], "指標": g.name,
                         "值": "—" if v is None else f"{v:g}",
                         "判讀": band})
        st.dataframe(pd.DataFrame(rows), width='stretch', hide_index=True)
        st.caption("判讀基準為教學參考，非系統告警門檻。詳見「名詞與判讀」頁。")

    st.divider()
    st.caption(
        "本頁為濃縮視圖。完整規則狀態（含 `unavailable` 者）見「事件卡」頁；"
        "各通道品質分布見「資料健康」頁。"
        "**24 小時以上的預報為研究階段產出，本頁刻意不予呈現。**"
    )


# ── 1. 太空環境總覽 ─────────────────────────────────────────────────────
elif page == "太空環境總覽":
    st.title("太空環境總覽")
    st.caption(f"更新於 {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")

    render_status_banner(get_store())
    st.divider()
    render_current_sun()
    st.divider()
    render_solar_wind_sim()
    st.divider()

    st.caption(f"更新於 {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")

    nowcast = load_nowcast()
    cols = st.columns(max(len(nowcast), 1))
    for col, (_, row) in zip(cols, nowcast.iterrows()):
        level = row["level"]
        color = LEVEL_COLOR.get(level, "#616161")
        note = "（間接推估）" if row.get("inference") == "proxy" else ""
        col.markdown(
            f"""<div style="border-left:6px solid {color};padding:.6rem 1rem;
                 background:rgba(128,128,128,.08);border-radius:4px">
                 <div style="font-size:.85rem;opacity:.75">
                   {DOMAIN_NAME.get(row['domain'], row['domain'])}</div>
                 <div style="font-size:1.7rem;font-weight:700;color:{color}">
                   {level} {LEVEL_NAME.get(level, '')}</div>
                 <div style="font-size:.75rem;opacity:.65">{note or row['active_rules']}</div>
               </div>""",
            unsafe_allow_html=True,
        )

    st.divider()
    c1, c2 = st.columns([2, 1])

    with c1:
        st.subheader("關鍵指標近況")
        for param, label in (("KP_3H", "Kp 指數"), ("DST", "Dst (nT)"),
                             ("XRAY_LONG", "X 射線通量 (W/m²)")):
            df = load_series(param, lookback)
            if df.empty:
                st.info(f"{label}：無資料")
                continue
            obs = df[df["data_type"].isin(["OBS", "INT"])]
            fcs = df[~df["data_type"].isin(["OBS", "INT"])]
            fig = go.Figure()
            fig.add_scatter(x=obs["valid_time"], y=obs["value"], name="觀測",
                            line=dict(width=2))
            if not fcs.empty:
                fig.add_scatter(x=fcs["valid_time"], y=fcs["value"], name="預測",
                                line=dict(width=2, dash="dot"))
            if param == "KP_3H":
                fig.add_hline(y=5, line_dash="dash", line_color="#d84315",
                              annotation_text="G1 門檻")
            if param == "XRAY_LONG":
                fig.update_yaxes(type="log")
                for flux, name in ((1e-5, "M"), (1e-4, "X")):
                    fig.add_hline(y=flux, line_dash="dot", line_color="#888",
                                  annotation_text=f"{name} 級")
            fig.update_layout(height=210, margin=dict(l=0, r=0, t=26, b=0),
                              title=label, showlegend=True)
            st.plotly_chart(fig, width='stretch')

    with c2:
        st.subheader("資料齡期")
        health = load_health()
        if health.empty:
            st.info("尚無資料。請先執行 `python -m services.ingest.run --source all`")
        else:
            for _, row in health.sort_values("age_s").head(14).iterrows():
                st.write(f"**{row['param_code']}** {age_badge(row['age_s'])}")
                if row.get("degraded"):
                    st.caption(f"⚠ 超過 {row['source_id']} 的延遲預算")


# ── 2. 參數時序 ─────────────────────────────────────────────────────────
elif page == "參數時序":
    st.title("參數時序")
    reg = registry()
    store = get_store()
    available = store.available_params()

    c1, c2 = st.columns([3, 1])
    params = c1.multiselect(
        "參數", available,
        default=[p for p in ("KP_3H", "DST", "F107_OBS") if p in available][:2],
        format_func=lambda p: f"{p} — {reg[p].name_zh}" if p in reg else p,
    )
    as_of = c2.text_input("回放至（UTC，可留空）", "",
                          help="填入時刻即進入回放模式，只顯示當時已入庫的資料")

    for param in params:
        spec = reg.get(param)
        kw = {}
        if as_of.strip():
            try:
                kw["as_of"] = pd.Timestamp(as_of, tz="UTC").to_pydatetime()
            except Exception:
                st.warning(f"無法解析時刻：{as_of}")
        end = datetime.now(timezone.utc)
        df = store.query(param, start=end - timedelta(days=lookback),
                         end=end + timedelta(days=3), **kw)
        st.subheader(f"{param} — {spec.name_zh if spec else ''}")
        if df.empty:
            st.info("此期間無資料（回放模式下代表當時尚未取得）")
            continue
        fig = px.line(df, x="valid_time", y="value", color="data_type",
                      hover_data=["source_id", "quality_flag"])
        fig.update_layout(height=320, margin=dict(l=0, r=0, t=10, b=0),
                          yaxis_title=spec.unit if spec else "")
        st.plotly_chart(fig, width='stretch')
        st.caption(
            f"{len(df)} 列｜來源 {', '.join(sorted(df['source_id'].dropna().unique()))}"
            f"｜品質 {dict(df['quality_flag'].value_counts())}"
        )


# ── 3. 事件卡 ───────────────────────────────────────────────────────────
elif page == "事件卡":
    st.title("事件卡")
    episodes, status = load_episodes(lookback)

    if episodes.empty:
        st.success(f"近 {lookback} 天無規則命中（L0）。")
    else:
        engine = RiskEngine(get_store())
        end = datetime.now(timezone.utc)
        eps, _ = engine.evaluate(start=end - timedelta(days=lookback), end=end)
        cards = build_event_cards(eps, store=get_store())
        for card in cards:
            d = card.to_dict()
            color = LEVEL_COLOR.get(d["mission_level"], "#616161")
            with st.container(border=True):
                c1, c2, c3 = st.columns([2, 1, 1])
                c1.markdown(f"### :{'red' if d['mission_level'] in ('L3', 'L4') else 'orange'}"
                            f"[{d['event_id']}]")
                c1.caption(d["type"])
                c2.metric("任務風險等級", d["mission_level"],
                          LEVEL_NAME.get(d["mission_level"], ""))
                c3.metric("國際分級", d["international_scale"] or "—",
                          f"可信度 {d['confidence']}")

                t = d["timeline"]
                st.write(f"**時間軸**　{t['onset_utc']} → {t['expected_end_utc']}"
                         f"（{t['duration_h']} 小時，峰值 {t['peak_utc']}）")

                st.write("**分項影響**")
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "網域": DOMAIN_NAME.get(i["domain"], i["domain"]),
                                "等級": i["level"],
                                "推估方式": "間接推估" if i.get("inference") == "proxy" else "直接判定",
                                "已排除因素": "、".join(i["exclusions_checked"]) or "—",
                                "說明": i["statement"],
                            }
                            for i in d["impacts"]
                        ]
                    ),
                    hide_index=True, width='stretch',
                )
                if d["recommendations"]:
                    st.write("**建議處置**")
                    for r in d["recommendations"]:
                        st.write(f"- {r}")
                st.caption(f"通報對象：{'、'.join(d['notify'])}　|　"
                           f"SDA：{d['sda_hooks']}")
                with st.expander("完整 JSON（SDA 介接格式）"):
                    st.json(d)

    st.divider()
    st.subheader("規則狀態")
    unavailable = status[status["status"] == "unavailable"]
    if not unavailable.empty:
        st.warning(
            f"{len(unavailable)} 條規則因缺資料無法判定——這是「沒資料」而非「沒事」。"
            f"涉及網域：{', '.join(sorted(set(unavailable['domain'])))}"
        )
    st.dataframe(status, hide_index=True, width='stretch')


# ── 4. 太陽閃焰 ─────────────────────────────────────────────────────────
elif page == "太陽閃焰":
    st.title("太陽閃焰")
    st.caption(
        "X 射線以光速抵達（約 8 分 20 秒），**沒有預警空間**——本頁為即時偵測與"
        "影響評估，不是預報。能提前的只有活動區的 M/X 級機率。"
    )

    store = get_store()
    end = datetime.now(timezone.utc)
    flares = store.query("FLARE_PEAK", start=end - timedelta(days=lookback), end=end)

    c1, c2, c3 = st.columns(3)
    if flares.empty:
        c1.metric("期間閃焰數", 0)
    else:
        peak = flares.loc[flares["value"].idxmax()]
        c1.metric("期間閃焰數", len(flares))
        c2.metric("最強閃焰", flux_to_class(peak["value"]) or "—",
                  f"{peak['valid_time']:%m-%d %H:%M}Z")
        c3.metric("NOAA R 級", r_scale(peak["value"]) or "未達 R1")

    for code, label in (("M_FLARE_PROB", "M 級機率"), ("X_FLARE_PROB", "X 級機率")):
        prob = store.query(code, start=end - timedelta(days=lookback), end=end)
        if not prob.empty:
            st.metric(label, f"{prob.iloc[-1]['value']:.0%}")

    xray = store.query("XRAY_LONG", start=end - timedelta(days=lookback), end=end)
    if not xray.empty:
        fig = go.Figure()
        fig.add_scatter(x=xray["valid_time"], y=xray["value"], name="0.1–0.8 nm")
        for flux, name, color in ((1e-6, "C", "#888"), (1e-5, "M", "#f9a825"),
                                  (1e-4, "X", "#b71c1c")):
            fig.add_hline(y=flux, line_dash="dot", line_color=color,
                          annotation_text=f"{name} 級")
        fig.update_yaxes(type="log", title="W/m²")
        fig.update_layout(height=360, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, width='stretch')

    if not flares.empty:
        st.subheader("閃焰事件")
        table = pd.DataFrame(
            {
                "峰值時刻 (UTC)": flares["valid_time"].dt.strftime("%Y-%m-%d %H:%M"),
                "分級": flares["value"].map(lambda v: flux_to_class(v) or "—"),
                "峰值通量 (W/m²)": flares["value"].map(lambda v: f"{v:.2e}"),
                "NOAA R 級": flares["value"].map(lambda v: r_scale(v) or "—"),
            }
        ).sort_values("峰值時刻 (UTC)", ascending=False)
        st.dataframe(table, hide_index=True, width='stretch')

    drap = store.query(["DRAP_TW_MHZ", "DRAP_MAX_MHZ"],
                       start=end - timedelta(days=lookback), end=end)
    if not drap.empty:
        st.subheader("D 層吸收（SWPC D-RAP）")
        st.caption("最高受影響頻率：值越高代表越多 HF 頻段因 D 層吸收而不可用。")
        fig = px.line(drap, x="valid_time", y="value", color="param_code")
        fig.update_layout(height=260, margin=dict(l=0, r=0, t=10, b=0),
                          yaxis_title="MHz")
        st.plotly_chart(fig, width='stretch')


# ── 5. 48 小時預報 ──────────────────────────────────────────────────────
elif page == "48 小時預報":
    st.title("48 小時預報")

    st.error(
        "🚫 **24 小時以上的預報為研究階段產出，不建議用於作業決策。**　"
        "測試折 BSS 於 24h 起轉負（不優於「永遠報氣候頻率」），"
        "且訓練折 POD ≈ 0.83、測試折僅 0.02–0.38，過擬合落差存在於**所有 horizon**。"
        "1–12 小時的技巧為正且勝過持續性基線，可作為輔助參考。"
    )
    store = get_store()
    end = datetime.now(timezone.utc)

    st.warning(
        "**技巧隨 horizon 迅速下降是物理必然**：L1 太陽風只有約 30–60 分鐘傳播時間，"
        "24 小時以上僅剩 27 日復現與氣候態可用。48 小時 horizon 上氣候平均勝過 ML 模型，"
        "依 Tier 0 門檻不應上線。詳見 `docs/forecast_verification.md`。"
    )

    fcs = store.query(["KP_3H", "KP_STORM_PROB"], start=end - timedelta(days=2),
                      end=end + timedelta(days=3))
    own = fcs[fcs["source_id"] == "swx_forecast"]
    swpc = fcs[fcs["source_id"] == "swpc_geomag_forecast"]
    obs = fcs[(fcs["param_code"] == "KP_3H") & fcs["data_type"].isin(["OBS", "INT"])]

    fig = go.Figure()
    if not obs.empty:
        fig.add_scatter(x=obs["valid_time"], y=obs["value"], name="觀測",
                        line=dict(width=3))
    if not swpc.empty:
        s = swpc[swpc["param_code"] == "KP_3H"]
        fig.add_scatter(x=s["valid_time"], y=s["value"], name="NOAA SWPC 官方預報",
                        line=dict(dash="dash"))
    if not own.empty:
        s = own[own["param_code"] == "KP_3H"]
        fig.add_scatter(x=s["valid_time"], y=s["value"], name="SWX 預報",
                        line=dict(dash="dot"))
    fig.add_hline(y=5, line_dash="dash", line_color="#d84315", annotation_text="G1 門檻")
    fig.update_layout(height=380, margin=dict(l=0, r=0, t=10, b=0), yaxis_title="Kp")
    st.plotly_chart(fig, width='stretch')

    if own.empty:
        st.info("尚未產生本系統預報。執行 "
                "`python -m services.forecast.run --predict --write`")
    else:
        prob = own[own["param_code"] == "KP_STORM_PROB"]
        if not prob.empty:
            st.subheader("地磁暴機率 P(Kp≥5)")
            fig2 = px.bar(prob, x="valid_time", y="value")
            fig2.update_layout(height=240, margin=dict(l=0, r=0, t=10, b=0),
                               yaxis_tickformat=".0%")
            st.plotly_chart(fig2, width='stretch')

    st.divider()
    st.subheader("與官方預報的關係")
    st.markdown(
        "NOAA SWPC 官方 3 日預報已介接為來源 `swpc_geomag_forecast`，是本引擎的**對照基準**。"
        "但 SWPC 只發布當前一份、取不到歷史版本，因此無法回測比較，"
        "只能自累積之日起做前瞻比較。**在累積足夠樣本前，不應宣稱本引擎優於官方預報。**"
    )


# ── 6. 地磁基準場（議題二）─────────────────────────────────────────────
elif page == "地磁基準場":
    st.title("地磁參考場與區域擾動")
    st.caption("構想書議題二：全球地磁場基準模型與臺灣周邊地磁擾動評估")

    import datetime as _dt

    from geomag import (
        geomagnetic_latitude,
        regional_disturbance_proxy,
        station_fields,
        summary,
    )

    epoch = _dt.datetime.now(timezone.utc).replace(tzinfo=None)
    info = summary(epoch)
    ref = info["taiwan_reference_point"]

    st.success(
        "**基準場部分已完成**：IGRF-14 離線可算，不需任何外部資料。"
        "議題二尚待補齊的是區域擾動的**在地實測**（架構書 C3）。"
    )

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("總場強度 F", f"{ref['F_nT']:,.0f} nT")
    m2.metric("磁偏角 D", f"{ref['D_deg']:.2f}°")
    m3.metric("磁傾角 I", f"{ref['I_deg']:.2f}°")
    m4.metric("地磁緯度", f"{info['geomagnetic_latitude_deg']:.1f}°N", "地理 23.5°N")

    st.info(
        f"臺灣地理緯度 23.5°N，**地磁緯度僅約 {info['geomagnetic_latitude_deg']:.0f}°N**，"
        "正落在赤道異常駝峰區。這是臺灣 GNSS 閃爍風險偏高的物理原因，"
        "也是區域模型必須處理的重點——用地理緯度判斷電離層現象會系統性失準。"
    )

    st.subheader(f"測站參考場（IGRF-14，epoch {epoch:%Y-%m-%d}）")
    st.dataframe(station_fields(epoch), hide_index=True, width='stretch')
    st.caption("LNP = 崙坪，INTERMAGNET 觀測站，中央氣象署運作。"
               "取得其即時串流即可把下方推估換成實測。")

    st.divider()
    st.subheader("區域擾動 ΔH")
    store = get_store()
    end = datetime.now(timezone.utc)
    dst = store.series("DST", start=end - timedelta(days=lookback), end=end)

    if dst.empty:
        st.warning("無 Dst 資料，無法推估區域擾動。")
    else:
        proxy = regional_disturbance_proxy(dst=dst, epoch=epoch)
        st.error(
            "⚠ 以下為**推估值（is_proxy=True）**，非在地實測。"
            "依據為 ΔH(λm) ≈ Dst·cos(λm)，未涵蓋赤道電急流（EEJ）、DP2 電流系統"
            "與測站在地感應效應——這些只有實測能捕捉。"
        )
        fig = px.line(proxy, x="valid_time", y="dH_est_nT")
        fig.add_hline(y=-100, line_dash="dot", line_color="#f9a825",
                      annotation_text="中度擾動")
        fig.add_hline(y=-250, line_dash="dot", line_color="#b71c1c",
                      annotation_text="強烈擾動")
        fig.update_layout(height=340, margin=dict(l=0, r=0, t=10, b=0),
                          yaxis_title="ΔH 推估 (nT)")
        st.plotly_chart(fig, width='stretch')
        c1, c2 = st.columns(2)
        c1.metric("期間最大壓抑", f"{proxy['dH_est_nT'].min():.0f} nT")
        c2.metric("地磁緯度縮放因子", f"{proxy['scale_factor'].iloc[0]:.4f}")

    st.divider()
    st.subheader("高解析地磁指數")
    st.caption("Kp 為 3 小時值，暴起始時刻會被糊掉 1–2 小時；"
               "「提前量」是構想書明列的 KPI，需要 Hp30 這類更高解析度的指標。")
    hp = store.query(["HP30", "KP_3H"], start=end - timedelta(days=min(lookback, 14)), end=end)
    if hp.empty:
        st.info("無 Hp30 資料。執行 `python -m services.ingest.run --source gfz_hp30`")
    else:
        fig = px.line(hp, x="valid_time", y="value", color="param_code")
        fig.update_layout(height=300, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, width='stretch')

    aurora = store.query("AURORA_BOUNDARY_LAT", start=end - timedelta(days=lookback), end=end)
    if not aurora.empty:
        st.metric("極光橢圓赤道側邊界", f"{aurora.iloc[-1]['value']:.0f}°N",
                  help="邊界越往低緯推進，代表地磁擾動越深入")


# ── 7. 軌道與密度修正 ───────────────────────────────────────────────────
elif page == "軌道與密度修正":
    st.title("軌道預報風險與密度修正因子")
    st.caption("議題四產品：地磁擾動 → 熱氣層密度上升 → 大氣阻力增加 → 軌道預報誤差")

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback)
    c1, c2 = st.columns([1, 3])
    preset = c1.selectbox("期間", ["近期", "2024-05 Gannon G5", "2022-02 Starlink 再入"])
    if preset == "2024-05 Gannon G5":
        start, end = (pd.Timestamp("2024-05-08", tz="UTC").to_pydatetime(),
                      pd.Timestamp("2024-05-15", tz="UTC").to_pydatetime())
    elif preset == "2022-02 Starlink 再入":
        start, end = (pd.Timestamp("2022-02-01", tz="UTC").to_pydatetime(),
                      pd.Timestamp("2022-02-10", tz="UTC").to_pydatetime())

    with st.spinner("計算 MSIS 2.1 密度…"):
        try:
            dc = drag_correction.build(get_store(), start=start, end=end)
        except Exception as exc:  # noqa: BLE001
            st.error(f"無法計算：{exc}")
            dc = pd.DataFrame()

    if dc.empty:
        st.info("此期間無足夠資料。")
    else:
        peak = dc.loc[dc["storm_ratio"].idxmax()]
        m1, m2, m3 = st.columns(3)
        m1.metric("最大密度修正倍率", f"{peak['storm_ratio']:.2f}×",
                  f"± {peak['uncertainty']:.2f}")
        m2.metric("發生高度帶", f"{peak['alt_band_km']} km")
        m3.metric("當時 Ap", f"{peak['ap']:.0f} nT",
                  f"{peak['valid_time']:%m-%d %H:%M}Z")

        fig = px.line(dc, x="valid_time", y="storm_ratio", color="alt_band_km")
        fig.update_layout(height=340, margin=dict(l=0, r=0, t=10, b=0),
                          yaxis_title="密度倍率（相對同 F10.7 的地磁寧靜態）")
        st.plotly_chart(fig, width='stretch')

        st.info(
            "基準為**同一 F10.7、地磁寧靜（Ap=4）**，而非太陽極小期——"
            "後者會把太陽週期當成事件效應，在太陽極大期產生十倍以上的假修正。"
            "不確定度為經驗保守值，尚未由觀測反演校準。"
        )
        st.dataframe(dc.tail(30), hide_index=True, width='stretch')

    st.divider()
    st.subheader("STK / GMAT 驅動檔")
    if st.button("產生 CSSI 太空天氣檔"):
        with st.spinner("產生中…"):
            wide = stk_spaceweather.build_frame(get_store())
            info = stk_spaceweather.summary(wide)
            from swx_core import cssi

            text = cssi.write_text(wide)
        st.success(f"{info['date_min']} → {info['date_max']}，{info['rows']} 天，"
                   f"區段 {info['sections']}")
        st.download_button("下載 SpaceWeather-All-v1.2.txt", text,
                           file_name="SpaceWeather-All-v1.2.txt")
        st.caption("置入 STK 的 CSSI 太空天氣檔路徑，HPOP 選 MSIS 2.1／JB2008 即生效。")


# ── 8. 資料健康 ─────────────────────────────────────────────────────────
elif page == "資料健康":
    st.title("資料健康")
    health = load_health()
    if health.empty:
        st.info("尚無資料。")
    else:
        degraded = health[health["degraded"]]
        c1, c2, c3 = st.columns(3)
        c1.metric("參數通道數", len(health))
        c2.metric("逾越延遲預算", len(degraded), delta_color="inverse")
        c3.metric("平均良好率", f"{health['good_rate'].mean():.1%}")

        if not degraded.empty:
            st.error(f"{len(degraded)} 個通道資料過期：" +
                     "、".join(degraded["param_code"].tolist()))

        show = health.copy()
        show["資料齡期"] = show["age_s"].map(age_badge)
        st.dataframe(
            show[["source_id", "param_code", "latest_valid_time", "資料齡期",
                  "n_rows", "good_rate", "degraded"]],
            hide_index=True, width='stretch',
        )

    st.divider()
    st.subheader("資料源盤點")
    st.dataframe(
        pd.DataFrame(
            [
                {"來源": s.source_id, "名稱": s.name, "層級": s.tier,
                 "狀態": s.status, "提供參數": ", ".join(s.provides)}
                for s in catalog()
            ]
        ),
        hide_index=True, width='stretch',
    )


# ── 9. 門檻校準 ─────────────────────────────────────────────────────────
elif page == "門檻校準":
    st.title("L0–L4 門檻校準模擬")
    st.markdown(
        "構想書把「分級門檻須與需求單位共同校準，避免過度告警或漏報」列為風險。"
        "本頁的用途，是在校準會議上當場回答：**這組門檻，過去幾年會發出幾次告警？**"
    )

    rules = load_rules()
    rule_map = {f"{r.rule_id} — {r.name}（{r.domain}/{r.level}）": r for r in rules}
    picked = st.selectbox("規則", list(rule_map))
    rule = rule_map[picked]

    param = st.selectbox("調整參數", rule.params)
    base = next((c.value for c in rule.conditions if c.param == param), 5.0)
    lo, hi = st.slider("掃描範圍", 0.0, float(base * 4 or 20),
                       (max(0.0, base * 0.5), base * 1.8))
    steps = st.slider("掃描點數", 3, 12, 5)

    if st.button("執行掃描"):
        import numpy as np

        from tools.whatif_threshold import sweep_rule

        store = get_store()
        series = store.series(param, observed_only=True)
        if series.empty:
            st.warning(f"資料層無 {param} 觀測資料。")
        else:
            start, end = series.index.min(), series.index.max()
            with st.spinner("回放中…"):
                table = sweep_rule(
                    RiskEngine(store), rule,
                    list(np.linspace(lo, hi, steps).round(2)),
                    start=start, end=end, param=param,
                )
            st.caption(f"回放期間 {start:%Y-%m-%d} → {end:%Y-%m-%d}"
                       f"（{(end - start).days} 天）")
            st.dataframe(table, hide_index=True, width='stretch')
            if "per_year" in table.columns:
                fig = px.line(table, x="threshold", y="per_year", markers=True)
                fig.update_layout(height=300, margin=dict(l=0, r=0, t=10, b=0),
                                  yaxis_title="每年告警次數")
                st.plotly_chart(fig, width='stretch')
            st.info(
                "判讀：`per_year` 是與需求單位討論可接受告警頻率的主要依據；"
                "`duty_cycle_pct` 偏高代表門檻過鬆；`max_h` 過長代表遲滯設定需檢討。"
            )

    with st.expander("目前規則定義"):
        st.json(
            {
                "rule_id": rule.rule_id, "domain": rule.domain, "level": rule.level,
                "conditions": [
                    {"param": c.param, "op": c.op, "value": c.value, "dwell_h": c.dwell_h}
                    for c in rule.conditions
                ],
                "clear_below": rule.clear_below, "clear_dwell_h": rule.clear_dwell_h,
                "impact": rule.impact, "action": rule.action,
                "notify": list(rule.notify), "exclusions": list(rule.exclusions),
            }
        )


# ── 10. 名詞與判讀（教育推廣）────────────────────────────────────────────
elif page == "名詞與判讀":
    from swx_core.interpret import (
        BAND_ALERT, BAND_NOTABLE, BAND_QUIET, BAND_UNKNOWN, GUIDANCE,
    )

    st.title("名詞與判讀")
    st.caption("給第一次接觸太空天氣的讀者、教育推廣場合，以及需要看懂系統輸出的值勤人員")

    st.info(
        "**這頁的門檻是判讀教學用的一般性參考，不是系統的告警門檻。**　"
        "系統實際的 L0–L4 門檻在 `configs/rules/*.yaml`，尚未與需求單位校準。"
        "兩者刻意分開，避免科普素材上的數字被誤當成作業標準。"
    )

    st.subheader("先看懂這條因果鏈")
    st.markdown(
        """
```
太陽表面           行星際空間          地球磁層          電離層／熱氣層        我方系統
活動區、閃焰   →   太陽風、IMF   →   地磁擾動      →   電子密度、大氣密度  →  通信／定位／軌道
（F10.7、X 射線）  （Bz、速度）      （Kp、Dst）       （TEC、S4、ρ）         （L0–L4 判讀）
```

**三種擾動的抵達時間完全不同**——這是判讀時最關鍵的一件事：

| 擾動 | 抵達時間 | 有無預警空間 |
|---|---|---|
| 電磁輻射（X 射線、紫外線） | **8 分 20 秒** | **沒有**。看到閃焰時影響已同時發生 |
| 高能質子（SEP） | 數十分鐘～數小時 | 很短 |
| 日冕物質拋射（CME） | **1–3 天** | 有，這是地磁暴預警的主要來源 |
"""
    )

    st.divider()
    st.subheader("目前各參數落在哪一區")
    st.caption("以最近一筆觀測值對照判讀區間。灰色代表無資料或無判讀基準——**不代表安全**。")

    store = get_store()
    reg = registry()
    end = datetime.now(timezone.utc)
    badge = {BAND_QUIET: "🟢", BAND_NOTABLE: "🟡", BAND_ALERT: "🔴", BAND_UNKNOWN: "⚪"}

    rows = []
    for code, g in GUIDANCE.items():
        df = store.query(code, start=end - timedelta(days=lookback), end=end)
        latest = None if df.empty else df.sort_values("valid_time").iloc[-1]
        value = None if latest is None else float(latest["value"])
        band = g.band(value)
        rows.append(
            {
                "": badge[band],
                "參數": code,
                "名稱": g.name,
                "最近值": "—" if value is None else f"{value:g}",
                "單位": reg[code].unit if code in reg else "",
                "判讀": band,
                "平時": g.quiet,
                "時間": "—" if latest is None else f"{latest['valid_time']:%m-%d %H:%M}",
            }
        )
    st.dataframe(pd.DataFrame(rows), width='stretch', hide_index=True)

    st.divider()
    st.subheader("逐一了解")
    pick = st.selectbox(
        "選擇參數", list(GUIDANCE),
        format_func=lambda c: f"{c} — {GUIDANCE[c].name}",
    )
    g = GUIDANCE[pick]
    st.markdown(f"### {g.name}　`{pick}`")
    st.markdown(f"**這個數字在量什麼**　{g.reads}")

    c1, c2, c3 = st.columns(3)
    c1.metric("平時", g.quiet)
    c2.metric("值得注意", "—" if g.notable is None else f"{g.notable:g}")
    c3.metric("警戒", "—" if g.alert is None else f"{g.alert:g}")
    if not g.higher_is_worse:
        st.caption("此參數**數值越低（越負）代表越嚴重**，判讀方向與多數參數相反。")
    if g.note:
        st.warning(f"**判讀要點**　{g.note}")

    df = store.query(pick, start=end - timedelta(days=lookback), end=end)
    if not df.empty:
        fig = px.line(df.sort_values("valid_time"), x="valid_time", y="value")
        for thr, label, color in (
            (g.notable, "值得注意", "orange"), (g.alert, "警戒", "red"),
        ):
            if thr is not None:
                fig.add_hline(y=thr, line_dash="dash", line_color=color,
                              annotation_text=label)
        fig.update_layout(height=320, margin=dict(l=0, r=0, t=10, b=0),
                          yaxis_title=reg[pick].unit if pick in reg else "")
        st.plotly_chart(fig, width='stretch')
    else:
        st.info("此期間無資料。**這不代表數值正常**，只代表系統目前沒有這個通道的觀測。")

    st.divider()
    st.subheader("常見誤讀")
    for title, body in [
        ("「Kp 平均 4，所以還好」",
         "Kp 是準對數量，**不能取算術平均**。一天走 2-2-2-8-8-2-2-2 與全天平穩 4，"
         "物理意義完全不同。要平均請用 ap。"),
        ("「沒有告警，所以安全」",
         "系統刻意區分「沒事」與「沒資料」。規則回報 `unavailable` 代表**判定所需資料不存在**，"
         "不是風險為零。儀表板對此顯示灰色而非綠燈。"),
        ("「有 X 級閃焰，快發地磁暴警報」",
         "閃焰 X 射線 8 分鐘就到，影響的是**日側 HF**；地磁暴要等 CME 走 1–3 天，"
         "且**只有 Bz 南向才會發生**。大閃焰未必伴隨地磁暴。"),
        ("「臺灣在低緯，電離層風險低」",
         "**恰恰相反**。臺灣地磁緯度僅約 19°N（地理緯度 23.5°N），位於**赤道異常駝峰**，"
         "是全球 TEC 最高、閃爍最強的區域之一。"),
        ("「TEC 高就會失鎖」",
         "TEC 高造成的是**可修正的**測距偏差；讓接收機失鎖的是**閃爍**（S4／ROTI）。"
         "兩者機制不同，處置方式也不同。"),
        ("「模型算出來的就是實測」",
         "IGRF 基準場、MSIS 密度、`storm_ratio` **全部是模型輸出**，"
         "目前皆未與在地實測逐點比對。推估量一律標 `is_proxy=True`。"),
        ("「G5 就一定是最高等級警報」",
         "G/R/S 是**環境強度**，L0–L4 是**任務影響**。同一場 G5 對不同單位的影響差很多，"
         "所以系統不提供兩者的換算表。"),
    ]:
        with st.expander(title):
            st.markdown(body)

    st.caption("完整名詞說明見 `docs/glossary.md`。")


# ── 影像頁 ──────────────────────────────────────────────────────────────
elif page == "太陽與行星際影像":
    st.title("太陽與行星際影像")
    st.caption(
        "外部機構產製的公開影像，本系統僅嵌入呈現、不重製散布。"
        "**每張圖的來源與使用條款與影像同框顯示**——把出處摺疊起來等於實質未標註。"
    )
    st.info(
        "影像為**直接連結產製者網址**，看到的永遠是對方當下的版本，"
        "不會是我們快取的過期影像。代價是封閉網路環境無法顯示。"
    )

    GROUPS = [
        ("solar", "太陽", "黑子、日珥、閃焰與日冕。判斷未來數日風險的源頭。"),
        ("solarwind", "太陽風與行星際傳播", "CME 何時抵達地球——少數具 1–3 天提前量的資訊。"),
        ("ionosphere", "電離層", "直接影響 HF 通信與 GNSS 定位的一層。"),
        ("geospace", "地球空間", "太陽風與磁層的交互作用。"),
    ]
    try:
        items = imagery()
    except Exception as exc:
        st.error(f"影像設定載入失敗：{exc}")
        items = []

    for gid, gname, gdesc in GROUPS:
        group = [i for i in items if i.get("group") == gid]
        if not group:
            continue
        st.subheader(gname)
        st.caption(gdesc)
        for row_start in range(0, len(group), 2):
            cols = st.columns(2)
            for col, item in zip(cols, group[row_start:row_start + 2]):
                with col:
                    with st.container(border=True):
                        render_image_card(item)
        st.divider()


# ── 使用指南 ────────────────────────────────────────────────────────────
elif page == "使用指南":
    st.title("使用指南")
    st.caption("依「你想知道什麼」編排，不依現象分類編排。")

    tabs = st.tabs([
        "這系統在做什麼", "怎麼看懂畫面", "現象與影響",
        "分級標準", "資料從哪來", "系統邊界",
    ])

    with tabs[0]:
        st.markdown("""
### 一句話

把分散的國內外太空天氣觀測，轉成**任務單位可判讀、可通報、可介接、可計算**的產品。

### 它不是什麼

**不是國家級太空天氣預報中心。** 中央氣象署太空天氣作業辦公室（SWOO）
才是國內的作業級機構，本系統不取代它。

差別在於：SWOO 提供的是**環境有多強**（採 NOAA 的 G/R/S 尺度）；
本系統接著回答**對我方任務有多大影響**（L0–L4），
並附上處置建議、通報對象與資料可信度標示。

### 四類產品

| 產品 | 給誰 | 形式 |
|---|---|---|
| 分級判讀 | 值勤席 | L0–L4 燈號與事件卡 |
| 事件通報 | 相關單位 | 事件卡 JSON，含影響分項與建議處置 |
| 系統介接 | SDA 平臺 | REST API、圖層 |
| 軌道計算 | 軌道分析 | STK/GMAT CSSI 驅動檔、大氣密度修正因子 |
""")

    with tabs[1]:
        st.markdown("""
### 三個必記

1. **灰色 ≠ 綠色**——沒資料不是沒事。
2. **模型 ≠ 實測**——標 `proxy` 的結論不可對外表述為實測。
3. **24 小時以上的預報不能拿來做決策。**

### 顏色與狀態

| 顯示 | 意義 | 該做什麼 |
|---|---|---|
| 🟢 L0 | 規則未觸發，**且該網域有資料** | 正常值勤 |
| 🟡 L1–L2 | 環境轉變／影響可量測 | 依處置對照表通報 |
| 🔴 L3–L4 | 嚴重／重大 | 人工確認後發布，啟動通報 |
| ⚪ 無資料 | 判定所需資料不存在 | **查通道，不是放心** |

### 判定依據 `inference`

事件卡每個影響分項都帶此欄位，**永不為 `null`**：

- `observed`　直接觀測值判定，可作為判斷依據
- `modelled`　模型或預報輸出，須註明來源為模型
- `proxy`　　間接推估，**不可對外表述為實測**
- `unavailable`　資料不存在，**不是安全**

### 時間

**全系統 UTC，無例外。** 臺灣時間 = UTC + 8。交班紀錄請用 UTC。
""")

    with tabs[2]:
        st.markdown("""
### 一條因果鏈

```
太陽表面        行星際空間       地球磁層       電離層／熱氣層     我方系統
活動區、閃焰 →  太陽風、IMF  →  地磁擾動   →  電子密度、大氣密度 → 通信／定位／軌道
```

### 三種擾動的抵達時間差三個數量級

| 擾動 | 抵達時間 | 有無預警空間 |
|---|---|---|
| 電磁輻射（X 射線） | **8 分 20 秒** | **沒有**。看到就是已經發生 |
| 高能質子（SEP） | 數十分鐘～數小時 | 很短 |
| 日冕物質拋射（CME） | **1–3 天** | 有，地磁暴預警的主要來源 |

所以「閃焰預警」與「地磁暴預警」是兩件難度天差地遠的事。

### 各現象影響什麼

| 現象 | 主要衝擊 | 本系統對應網域 |
|---|---|---|
| 太陽閃焰（X 射線） | 日照側 HF 中斷（D 層吸收） | `HF_COMM` |
| 太陽質子事件 | 極區 HF 中斷、衛星單粒子翻轉 | `HF_COMM` |
| 地磁暴 | 熱氣層膨脹 → 低軌阻力增加 → 軌道預報誤差 | `ORBIT_PREDICTION` |
| 電離層擾動／閃爍 | GNSS 定位劣化、載波失鎖、授時中斷 | `GNSS_PNT` |

### 判讀順序

先看 **`IMF_BZ` 與 `SW_V`**（上游、有提前量），再看 **`KP_3H`／`DST`**（已發生的結果）。
只盯 Kp 等於只看後照鏡。

記一句：**南向 Bz 才會出事。**
""")

    with tabs[3]:
        st.markdown("""
### 兩套分級，不能互換

| 標記 | 是什麼 | 誰定的 |
|---|---|---|
| **L0–L4** | **任務風險等級** | 本系統自訂，門檻在 `configs/rules/*.yaml` |
| G/R/S | 環境事件強度 | NOAA Space Weather Scales |

**G/R/S 說「事件有多強」，L0–L4 說「對我方任務有多大影響」。**
同一場 G4 對地面單位可能只是 L1，對低軌操作單位可能是 L4，
所以系統**不提供換算表**。事件卡把兩者放在獨立欄位
（`international_scale` 與 `mission_level`）。

### NOAA 階梯

| 級 | G（Kp） | R（GOES 0.1–0.8 nm） | S（≥10 MeV 質子） |
|---|---|---|---|
| 1 | 5 | 1×10⁻⁵（M1） | 10 pfu |
| 2 | 6 | 5×10⁻⁵（M5） | 10² pfu |
| 3 | 7 | 1×10⁻⁴（X1） | 10³ pfu |
| 4 | 8 | 1×10⁻³（X10） | 10⁴ pfu |
| 5 | 9 | 2×10⁻³（X20） | 10⁵ pfu |

### 本系統的 L0–L4

| 等級 | 名稱 | 意義 |
|---|---|---|
| L0 | 正常 | 規則未觸發，且該網域有資料 |
| L1 | 注意 | 環境轉變，納入任務前提示 |
| L2 | 警戒 | 影響可量測，需通報相關席位 |
| L3 | 嚴重 | 建議調整任務規劃。**須人工確認後發布** |
| L4 | 重大 | 啟動重大事件通報與事件復盤 |

**門檻目前標記 `calibrated: false`**——尚未與需求單位校準，絕對意義仍待定版。
完整處置對照見 `docs/operations_manual.md`。
""")

    with tabs[4]:
        st.subheader("資料來源與標註")
        st.caption(
            "本系統整合的皆為外部機構產製的資料。"
            "**引用本系統的任何數字時，須一併標註原始產製者。**"
        )
        rows = []
        for spec in catalog():
            attr = spec.raw.get("attribution") or {}
            rows.append({
                "來源": spec.source_id,
                "狀態": spec.status,
                "產製者": attr.get("provider", "⚠ 未標註"),
                "內容": attr.get("product", ""),
                "使用條款": attr.get("terms", ""),
            })
        st.dataframe(pd.DataFrame(rows), width='stretch', hide_index=True)

        st.subheader("影像來源")
        try:
            img_rows = [{
                "影像": i["title"],
                "儀器／模式": i.get("instrument", ""),
                "產製者": i["attribution"].get("provider", ""),
                "使用條款": i["attribution"].get("terms", ""),
            } for i in imagery()]
            st.dataframe(pd.DataFrame(img_rows), width='stretch', hide_index=True)
        except Exception as exc:
            st.error(f"影像設定載入失敗：{exc}")

        st.info(
            "**特別注意**：CWA SWOO 的端點非公開 API，"
            "本案經成功大學合作管道取得中央氣象署授權後使用。"
            "**第三方不得逕行取用**，須另行取得授權。"
        )

    with tabs[5]:
        st.markdown("""
### 不可用於什麼

- **不得把 24 小時以上的預報用於作業決策。**
  測試折 BSS 於 24 小時起轉負，訓練折 POD 約 0.83 而測試折僅 0.02–0.38，
  過擬合落差存在於所有 horizon。
- **不得把推估值對外表述為實測。**
- **不得將 `unavailable` 解讀為 L0。**
- **未經人工確認的 L3 以上事件卡不得對外發送。**
- **不得依 DEMO 資料做任何作業判斷。**
- **不得用地理緯度判斷臺灣的電離層風險**——臺灣地磁緯度僅約 19°N，
  位於赤道異常駝峰，用地理緯度會系統性低估。

### 目前未校準的項目

| 項目 | 標記 |
|---|---|
| L0–L4 分級門檻 | `calibrated: false` |
| 密度修正因子 | `calibrated_by_observation: false` |
| D-RAP 與在地 HF 通聯的對應 | 未校準 |
| 多頻段影響矩陣 | 未校準 |

### 已知的模型限制

- **IGRF 基準場**僅通過值域合理性檢查，未與任一測站實測序列逐點比對。
- **密度修正因子**在固定參考點（lat 0°／lon 0°）算出，
  座標會使倍率變動達 14.9%（臺灣位置約 −8.45%）。
- **CSSI 格式**已對來源實檔逐行驗證，但 STK 端實際載入尚未交叉比對。

完整說明見 `docs/operations_manual.md` 與 `docs/research_review.md`。
""")
