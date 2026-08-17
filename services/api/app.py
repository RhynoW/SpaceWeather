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

from swx_core import SwxStore, catalog, registry
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


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return []
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[col]):
            out[col] = out[col].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return out.where(pd.notna(out), None).to_dict(orient="records")


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
        latest = df["valid_time"].max() if not df.empty else None
        age = _age_seconds(latest)
        body = {
            "param": param,
            "name": spec.name_zh,
            "unit": spec.unit,
            "count": len(df),
            "data_age_s": None if age is None else round(age),
            "degraded": bool(spec.cadence_s and age and age > 5 * spec.cadence_s),
            "records": _records(df[["valid_time", "value", "quality_flag",
                                    "data_type", "source_id"]] if not df.empty else df),
        }
        # 回應內含本系統預報列時，附上機器可讀的作業性告誡。
        # 只寫在文件裡不夠——呼叫端讀的是 JSON，不是 README。
        if not df.empty and (df["source_id"] == "swx_forecast").any():
            body["advisory"] = {
                "code": "RESEARCH_GRADE_FORECAST",
                "not_for_operational_use_beyond_h": 12,
                "message": (
                    "超過 12 小時的預報為研究階段產出，不建議用於作業決策："
                    "測試折 BSS 於 24h 起轉負，且訓練/測試折存在過擬合落差。"
                ),
                "reference": "docs/forecast_verification.md",
            }
        return jsonify(body)

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
