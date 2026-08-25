"""services.api.app — SWX API（架構書 §10.1）。

沿用 Sat_TraingDataExtension/backend_duckdb_v2.py 的底座模式：Flask + CORS +
`Settings.from_env` + 唯讀資料存取。原案的 `/api/orbit_czml` 等軌道端點可在
後續階段直接併入本服務，成為 SDA 圖層與太空天氣圖層疊加的基礎。

API 設計原則（架構書 §10.1）：唯讀為主、無狀態、可快取；所有回應帶
`data_age_s` 與 `degraded`，讓呼叫端能分辨「沒事」與「沒資料」。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from flask import Flask, jsonify, request

from swx_core import SwxStore, catalog, data_origin, registry
from swx_core.schema import OBSERVED_TYPES
from swx_core.flare import flux_to_class, r_scale

from services.exporter import drag_correction, stk_spaceweather
from services.risk_engine.engine import RiskEngine
from services.risk_engine.eventcard import EventStore, build_event_cards


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 5100
    debug: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.getenv("SWX_API_HOST", "127.0.0.1"),
            port=int(os.getenv("SWX_API_PORT", "5100")),
            debug=os.getenv("SWX_API_DEBUG", "false").lower() == "true",
        )


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return pd.Timestamp(value, tz="UTC").to_pydatetime()


def _now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def _age_seconds(latest) -> float | None:
    if latest is None or pd.isna(latest):
        return None
    return (_now() - pd.Timestamp(latest)).total_seconds()



def _iso_ts(value) -> str | None:
    """pandas 時間戳 → ISO 8601（UTC，Z 結尾）。"""
    if value is None or pd.isna(value):
        return None
    ts = pd.Timestamp(value)
    ts = ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")
    return ts.isoformat().replace("+00:00", "Z")


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return []
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[col]):
            out[col] = out[col].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return out.where(pd.notna(out), None).to_dict(orient="records")


#: 本系統預報一律附帶的作業性告誡。只寫在文件裡不夠——呼叫端讀的是 JSON。
FORECAST_ADVISORY = {
    "code": "RESEARCH_GRADE_FORECAST",
    "not_for_operational_use_beyond_h": 12,
    "message": (
        "超過 12 小時的預報為研究階段產出，不建議用於作業決策："
        "測試折 BSS 於 24h 起轉負，且訓練/測試折存在過擬合落差。"
    ),
    "reference": "docs/forecast_verification.md",
}


def _skill_block(skill: dict, hkey: str | None, picker) -> dict:
    """該 horizon 的實測技巧：上線模型與它贏過的基線，一起給。

    只給上線模型的分數，讀者無從判斷 0.6 的 POD 是好是壞；
    只給差值，又看不出絕對水準。兩者並列才可判讀。
    """
    entry = (skill.get("horizons", {}) or {}).get(hkey) if hkey else None
    best, baseline = picker(entry)
    keep = ("model", "MAE", "POD", "FAR", "CSI", "BSS",
            "episodes", "ep_recall", "lead_h_med", "lead_n")
    return {
        "skill": None if not best else {k: best.get(k) for k in keep},
        "skill_baseline": None if not baseline else {k: baseline.get(k) for k in keep},
    }


def create_app(store: SwxStore | None = None) -> Flask:
    app = Flask(__name__)
    try:
        from flask_cors import CORS

        CORS(app, resources={r"/v1/*": {"origins": "*"}})
    except ImportError:      # CORS 非必要相依；封閉環境未安裝時照常運作
        pass

    store = store or SwxStore()
    reg = registry()
    events = EventStore()

    # ── 健康與字典 ──────────────────────────────────────────────────────
    @app.get("/health")
    def health():
        h = store.health()
        return jsonify(
            {
                "status": "ok",
                # 呼叫端必須能分辨這台服務端的是示範快照還是實際擷取的資料。
                # 只給資料齡期不夠——快照內的資料在其自身時間軸上看起來是新的。
                "data": data_origin(),
                "params_in_store": store.available_params(),
                "sources_ready": [s.source_id for s in catalog().ready()],
                "degraded_sources": sorted(
                    h.loc[h["degraded"], "source_id"].unique().tolist()
                ) if not h.empty else [],
            }
        )

    @app.get("/v1/params")
    def params():
        return jsonify(_records(reg.to_frame()))

    @app.get("/v1/sources")
    def sources():
        return jsonify(
            [
                {
                    "source_id": s.source_id,
                    "name": s.name,
                    "tier": s.tier,
                    "status": s.status,
                    "provides": list(s.provides),
                    "cadence_s": s.cadence_s,
                    "notes": s.notes,
                }
                for s in catalog()
            ]
        )

    @app.get("/v1/health/data")
    def data_health():
        return jsonify(_records(store.health()))

    # ── 觀測與回放 ──────────────────────────────────────────────────────
    @app.get("/v1/obs")
    def obs():
        param = request.args.get("param")
        if not param:
            return jsonify({"error": "缺少 param 參數"}), 400
        if param not in reg:
            return jsonify({"error": f"參數 {param} 未在 param_registry 註冊"}), 404

        df = store.query(
            param,
            start=_ts(request.args.get("from")),
            end=_ts(request.args.get("to")),
            as_of=_ts(request.args.get("as_of")),     # 回放模式
            observed_only=request.args.get("observed_only", "false").lower() == "true",
        )
        spec = reg[param]
        # 齡期只能由**觀測列**計算。回應可能同時含預報列，其 valid_time 在未來；
        # 若一併取 max()，齡期會變成負值，`degraded` 的判斷式
        # （age > 5×cadence）就永遠為假——只要有任何預報列存在，
        # 過期的觀測通道就再也不會被標記為劣化，而那正是 degraded 的唯一用途。
        obs = df[df["data_type"].isin(OBSERVED_TYPES)] if not df.empty else df
        latest = obs["valid_time"].max() if not obs.empty else None
        age = _age_seconds(latest)
        fcs = df[~df["data_type"].isin(OBSERVED_TYPES)] if not df.empty else df
        body = {
            "param": param,
            "name": spec.name_zh,
            "unit": spec.unit,
            "count": len(df),
            "observed_count": int(len(obs)),
            # 觀測資料的齡期。**不含預報列**，見上方註解。
            "data_age_s": None if age is None else round(age),
            "latest_observed_utc": None if latest is None else _iso_ts(latest),
            # 預報涵蓋到哪個時刻（無預報列時為 null），與齡期是兩件事
            "forecast_to_utc": None if fcs.empty else _iso_ts(fcs["valid_time"].max()),
            "degraded": bool(spec.cadence_s and age and age > 5 * spec.cadence_s),
            "records": _records(df[["valid_time", "value", "quality_flag",
                                    "data_type", "source_id"]] if not df.empty else df),
        }
        # 回應內含本系統預報列時，附上機器可讀的作業性告誡。
        # 只寫在文件裡不夠——呼叫端讀的是 JSON，不是 README。
        if not df.empty and (df["source_id"] == "swx_forecast").any():
            body["advisory"] = dict(FORECAST_ADVISORY)
        return jsonify(body)

    @app.get("/v1/forecast")
    def forecast():
        """預報序列＋該 horizon 的實測技巧。

        **技巧與預報值必須同時回傳**。只給「Kp 3.2」而不給「這個 horizon 的
        中位提前量是 0 小時、誤警率 0.52」，呼叫端無從判斷該不該據以行動——
        這正是構想書把命中率／誤警率／提前量／可信度四項並列為 KPI 的用意。

        `horizon_h` 由 `valid_time − issued_utc` 還原，其中 `issued_utc` 取該
        目標參數的**最新觀測時刻**（預報引擎即以此為起報錨點）。資料落後時
        還原值會偏大，故一併回傳 `issued_basis` 讓呼叫端知道這是還原而非記錄。
        """
        from services.forecast.features import TARGETS
        from services.forecast.run import forecast_confidence
        from services.forecast.skill import (latest_forecast_batch, load_skill,
                                             skill_models)

        key = request.args.get("target", "kp")
        if key not in TARGETS:
            return jsonify({"error": f"未知的預報目標 {key}",
                            "available": sorted(TARGETS)}), 404
        spec = TARGETS[key]

        now = _now()
        # 視窗放寬到 30 天：資料落後時仍要看得到最後一批預報與它的錨點，
        # 空回應會被誤讀成「沒有風險」而非「資料沒更新」。
        df = store.query([spec.code, spec.prob_code],
                         start=now - timedelta(days=30), end=now + timedelta(days=3),
                         as_of=_ts(request.args.get("as_of")))
        fcs = latest_forecast_batch(
            df[df["source_id"] == "swx_forecast"] if not df.empty else df)
        obs = df[df["data_type"].isin(OBSERVED_TYPES)] if not df.empty else df
        latest_obs = obs.loc[obs["param_code"] == spec.code, "valid_time"].max() \
            if not obs.empty else None
        # 對齊到目標的格點：預報引擎以格點為起報錨點，而觀測的 valid_time
        # 可能落在格間（例如 SWPC 估計 Kp 標在 00:05）。不對齊的話 horizon
        # 會還原成 2.92 小時，技巧查表就查不到——差幾分鐘讓整個 KPI 消失。
        issued = None if latest_obs is None else pd.Timestamp(latest_obs).floor(spec.grid)

        prob = (fcs[fcs["param_code"] == spec.prob_code]
                .set_index("valid_time")["value"].to_dict()) if not fcs.empty else {}

        skill = load_skill().get("targets", {}).get(key, {})
        want = request.args.get("horizon")

        rows = []
        for _, r in (fcs[fcs["param_code"] == spec.code].sort_values("valid_time")
                     if not fcs.empty else fcs).iterrows():
            h = None if issued is None else round(
                (pd.Timestamp(r["valid_time"]) - pd.Timestamp(issued)).total_seconds() / 3600, 2)
            if want and str(h) != want and str(int(h or 0)) != want:
                continue
            hkey = None if h is None else str(int(h)) if float(h).is_integer() else str(h)
            rows.append({
                "valid_time": _iso_ts(r["valid_time"]),
                "horizon_h": h,
                "value": None if pd.isna(r["value"]) else float(r["value"]),
                "storm_probability": prob.get(r["valid_time"]),
                # confidence 依 horizon 分層，非逐筆機率的函數（機率分類器過擬合，
                # 用「這筆有多篤定」當可信度等於把過擬合當信心）
                "confidence": None if h is None else forecast_confidence(h),
                **_skill_block(skill, hkey, skill_models),
            })

        return jsonify({
            "target": key,
            "param": spec.code,
            "grid": spec.grid,
            "storm_threshold": spec.storm_threshold,
            "issued_utc": _iso_ts(issued),
            "issued_basis": f"latest_observation floored to {spec.grid}",
            "latest_observation_utc": _iso_ts(latest_obs),
            "data_age_s": None if latest_obs is None else round(_age_seconds(latest_obs)),
            "count": len(rows),
            "forecasts": rows,
            "skill_reference": {
                "generated_utc": skill.get("generated_utc"),
                "command": skill.get("command"),
                "splits": skill.get("splits"),
                "sample_span_utc": skill.get("sample_span_utc"),
                "note": ("skill 為滾動起報回測的實測值，非本次預報的信心；"
                         "lead_h_med 可為負，代表事件開始後才首次命中。"),
            },
            "advisory": dict(FORECAST_ADVISORY),
        })

    # ── 現況與事件 ──────────────────────────────────────────────────────
    @app.get("/v1/nowcast")
    def nowcast():
        engine = RiskEngine(store)
        return jsonify(
            {
                "issued_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "domains": _records(engine.nowcast()),
            }
        )

    @app.get("/v1/events")
    def list_events():
        start = _ts(request.args.get("from"))
        end = _ts(request.args.get("to"))
        if start or end:
            engine = RiskEngine(store)
            eps, _ = engine.evaluate(start=start, end=end, as_of=_ts(request.args.get("as_of")))
            cards = [c.to_dict() for c in build_event_cards(eps, store=store)]
        else:
            cards = events.list_events(min_level=request.args.get("level"))
        return jsonify({"count": len(cards), "events": cards})

    @app.get("/v1/events/<event_id>")
    def get_event(event_id: str):
        card = events.latest(event_id)
        if card is None:
            return jsonify({"error": f"查無事件 {event_id}"}), 404
        return jsonify(card)

    @app.get("/v1/events/<event_id>/history")
    def event_history(event_id: str):
        return jsonify(events.history(event_id))

    @app.get("/v1/rules")
    def rules():
        engine = RiskEngine(store)
        _, status = engine.evaluate(
            start=datetime.now(timezone.utc) - timedelta(days=2),
            end=datetime.now(timezone.utc),
        )
        return jsonify(_records(status))

    # ── 太陽閃焰 ────────────────────────────────────────────────────────
    @app.get("/v1/flares")
    def flares():
        df = store.query(
            "FLARE_PEAK",
            start=_ts(request.args.get("from")),
            end=_ts(request.args.get("to")),
        )
        if df.empty:
            return jsonify({"count": 0, "flares": []})
        out = [
            {
                "peak_utc": row["valid_time"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                "peak_flux_w_m2": row["value"],
                "flare_class": flux_to_class(row["value"]),
                "noaa_r_scale": r_scale(row["value"]),
                "source_id": row["source_id"],
            }
            for _, row in df.iterrows()
        ]
        out.sort(key=lambda d: d["peak_utc"], reverse=True)
        return jsonify({"count": len(out), "flares": out})

    # ── 匯出 ────────────────────────────────────────────────────────────
    @app.get("/v1/exports/stk/spaceweather.txt")
    def export_stk():
        from swx_core import cssi

        wide = stk_spaceweather.build_frame(
            store,
            start=_ts(request.args.get("from")),
            as_of=_ts(request.args.get("as_of")),
            mode=request.args.get("mode", stk_spaceweather.MODE_SOURCE),
        )
        text = cssi.write_text(wide, updated=datetime.now(timezone.utc))
        return app.response_class(text, mimetype="text/plain; charset=utf-8")

    @app.get("/v1/exports/drag-correction")
    def export_drag():
        df = drag_correction.build(
            store,
            start=_ts(request.args.get("from")),
            end=_ts(request.args.get("to")),
            as_of=_ts(request.args.get("as_of")),
        )
        return jsonify(
            {
                # 與 services.exporter.drag_correction.export() 的 metadata 同義，
                # 兩處若飄開，使用者會拿到互相矛盾的基準定義。
                **drag_correction.product_metadata(),
                "count": len(df),
                "records": _records(df),
            }
        )

    @app.errorhandler(Exception)
    def on_error(exc: Exception):
        """未預期例外一律回 500，且**不把內部例外訊息送出**。

        例外字串常含檔案路徑、SQL 片段與套件內部結構，對呼叫端無用，
        對外網部署則是資訊洩漏。詳情寫進 server log，只有 debug 模式才回傳。
        """
        app.logger.exception("API error")
        body = {"error": {"code": "INTERNAL_ERROR", "message": "服務暫時無法完成請求"}}
        if app.debug:
            body["error"]["detail"] = f"{type(exc).__name__}: {exc}"
        return jsonify(body), 500

    return app


def main() -> int:
    settings = Settings.from_env()
    app = create_app()
    print(f"SWX API 啟動：http://{settings.host}:{settings.port}")
    print("  /health  /v1/params  /v1/obs?param=KP_3H  /v1/nowcast  /v1/events")
    print("  /v1/flares  /v1/exports/stk/spaceweather.txt  /v1/exports/drag-correction")
    app.run(host=settings.host, port=settings.port, debug=settings.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
