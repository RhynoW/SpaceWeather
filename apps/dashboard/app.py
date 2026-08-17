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
from swx_core import SwxStore, catalog, data_origin, quality_summary, registry  # noqa: E402
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
    ["值勤模式", "太空環境總覽", "參數時序", "事件卡", "太陽閃焰", "48 小時預報",
     "地磁基準場", "軌道與密度修正", "資料健康", "門檻校準", "名詞與判讀"],
)
lookback = st.sidebar.slider("回顧天數", 1, 60, 7)
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
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
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
            st.plotly_chart(fig, use_container_width=True)

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
        st.plotly_chart(fig, use_container_width=True)
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
                    hide_index=True, use_container_width=True,
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
    st.dataframe(status, hide_index=True, use_container_width=True)


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
        st.plotly_chart(fig, use_container_width=True)

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
        st.dataframe(table, hide_index=True, use_container_width=True)

    drap = store.query(["DRAP_TW_MHZ", "DRAP_MAX_MHZ"],
                       start=end - timedelta(days=lookback), end=end)
    if not drap.empty:
        st.subheader("D 層吸收（SWPC D-RAP）")
        st.caption("最高受影響頻率：值越高代表越多 HF 頻段因 D 層吸收而不可用。")
        fig = px.line(drap, x="valid_time", y="value", color="param_code")
        fig.update_layout(height=260, margin=dict(l=0, r=0, t=10, b=0),
                          yaxis_title="MHz")
        st.plotly_chart(fig, use_container_width=True)


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
    st.plotly_chart(fig, use_container_width=True)

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
            st.plotly_chart(fig2, use_container_width=True)

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
    st.dataframe(station_fields(epoch), hide_index=True, use_container_width=True)
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
        st.plotly_chart(fig, use_container_width=True)
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
        st.plotly_chart(fig, use_container_width=True)

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
        st.plotly_chart(fig, use_container_width=True)

        st.info(
            "基準為**同一 F10.7、地磁寧靜（Ap=4）**，而非太陽極小期——"
            "後者會把太陽週期當成事件效應，在太陽極大期產生十倍以上的假修正。"
            "不確定度為經驗保守值，尚未由觀測反演校準。"
        )
        st.dataframe(dc.tail(30), hide_index=True, use_container_width=True)

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
            hide_index=True, use_container_width=True,
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
        hide_index=True, use_container_width=True,
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
            st.dataframe(table, hide_index=True, use_container_width=True)
            if "per_year" in table.columns:
                fig = px.line(table, x="threshold", y="per_year", markers=True)
                fig.update_layout(height=300, margin=dict(l=0, r=0, t=10, b=0),
                                  yaxis_title="每年告警次數")
                st.plotly_chart(fig, use_container_width=True)
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
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

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
        st.plotly_chart(fig, use_container_width=True)
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
