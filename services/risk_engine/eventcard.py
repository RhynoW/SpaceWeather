"""services.risk_engine.eventcard — 事件卡建立與作業狀態庫（架構書 §10.2、§6.1）。

事件卡是本案與 SDA 平臺的主要介接物。兩個設計重點：

  修訂而非覆寫   事件在發展中會改判（G3 升 G4、結束時間延後）。以 revision +
                 supersedes 保留每一版，事後復盤才能回答「當時發布的是什麼」。

  作業狀態走 SQLite  事件卡有狀態機（draft → issued → superseded）與並發寫入，
                 屬 OLTP 性質，不適合放在 DuckDB／Parquet 分析層（架構書 §6.1）。

`exclusions_checked` 欄位刻意做成資料而非文字：讓「已排除哪些非太空天氣因素」
可被查詢與統計，而不是埋在敘述裡（架構書 §9.3）。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from swx_core import SwxStore, data_dir, registry
from swx_core.flare import flux_to_class, r_scale

from .engine import Episode, level_rank, max_level

SCHEMA_VERSION = "1.0"

STATUS_DRAFT = "draft"
STATUS_ISSUED = "issued"
STATUS_SUPERSEDED = "superseded"

# 事件型態（依主導參數推定）
EVENT_TYPES = {
    "KP_3H": "GEOMAGNETIC_STORM",
    "AP_3H": "GEOMAGNETIC_STORM",
    "AP_AVG": "GEOMAGNETIC_STORM",
    "DST": "GEOMAGNETIC_STORM",
    "XRAY_LONG": "SOLAR_FLARE",
    "XRAY_SHORT": "SOLAR_FLARE",
    "FLARE_PEAK": "SOLAR_FLARE",
    "PROT10": "SOLAR_RADIATION_STORM",
    "S4": "IONOSPHERIC_SCINTILLATION",
    "ROTI": "IONOSPHERIC_SCINTILLATION",
    "TEC": "IONOSPHERIC_DISTURBANCE",
    "X_FLARE_PROB": "SOLAR_ACTIVITY_OUTLOOK",
}

DDL = """
CREATE TABLE IF NOT EXISTS event_card (
    event_id     TEXT NOT NULL,
    revision     INTEGER NOT NULL,
    issued_utc   TEXT NOT NULL,
    status       TEXT NOT NULL,
    mission_level TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    onset_utc    TEXT,
    end_utc      TEXT,
    supersedes   TEXT,
    payload      TEXT NOT NULL,
    PRIMARY KEY (event_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_event_card_status ON event_card(status);
CREATE INDEX IF NOT EXISTS idx_event_card_onset  ON event_card(onset_utc);

-- append-only 稽核：所有發布、修訂、門檻異動皆留痕（架構書 §12）
CREATE TABLE IF NOT EXISTS audit_log (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc     TEXT NOT NULL,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    subject    TEXT,
    detail     TEXT
);
"""


def _iso(ts) -> str | None:
    if ts is None or (isinstance(ts, float) and pd.isna(ts)):
        return None
    return pd.Timestamp(ts).tz_convert("UTC").isoformat().replace("+00:00", "Z")


@dataclass
class EventCard:
    event_id: str
    event_type: str
    mission_level: str
    onset_utc: pd.Timestamp
    end_utc: pd.Timestamp | None
    peak_utc: pd.Timestamp | None
    confidence: float
    revision: int = 1
    supersedes: str | None = None
    issued_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    international_scale: str | None = None
    drivers: list[dict] = field(default_factory=list)
    impacts: list[dict] = field(default_factory=list)
    orbit_products: dict = field(default_factory=dict)
    recommendations: list[str] = field(default_factory=list)
    notify: list[str] = field(default_factory=list)
    affected_region: str | None = None
    sources: list[str] = field(default_factory=list)
    rule_ids: list[str] = field(default_factory=list)
    status: str = STATUS_DRAFT

    def to_dict(self) -> dict[str, Any]:
        duration = None
        if self.end_utc is not None and self.onset_utc is not None:
            duration = round((self.end_utc - self.onset_utc).total_seconds() / 3600.0, 2)
        return {
            "event_id": self.event_id,
            "schema_version": SCHEMA_VERSION,
            "issued_utc": _iso(self.issued_utc),
            "revision": self.revision,
            "supersedes": self.supersedes,
            "status": self.status,
            "type": self.event_type,
            "international_scale": self.international_scale,
            "mission_level": self.mission_level,
            "confidence": round(float(self.confidence), 3),
            "timeline": {
                "onset_utc": _iso(self.onset_utc),
                "peak_utc": _iso(self.peak_utc),
                "expected_end_utc": _iso(self.end_utc),
                "duration_h": duration,
            },
            "affected_region": self.affected_region,
            "drivers": self.drivers,
            "impacts": self.impacts,
            "orbit_products": self.orbit_products,
            "recommendations": self.recommendations,
            "notify": self.notify,
            "sda_hooks": {
                "record_in_sda": level_rank(self.mission_level) >= 2,
                "correlate_with": ["ANOMALOUS_MANEUVER", "CONJUNCTION"]
                if self.event_type == "GEOMAGNETIC_STORM"
                else [],
            },
            "rule_ids": self.rule_ids,
            "sources": self.sources,
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


def build_event_cards(
    episodes: list[Episode],
    *,
    store: SwxStore | None = None,
    merge_gap_h: float = 12.0,
) -> list[EventCard]:
    """把規則命中段合併成事件卡。

    合併原則：同一事件型態、時間相鄰（間隔 <= merge_gap_h）者視為同一事件，
    等級取其中最高，但每個網域的分項影響各自保留（架構書 §9.1「多網域彙整」）。
    """
    if not episodes:
        return []
    store = store or SwxStore()
    reg = registry()

    enriched = []
    for ep in episodes:
        enriched.append((EVENT_TYPES.get(ep.peak_param, "SPACE_WEATHER_EVENT"), ep))
    enriched.sort(key=lambda t: (t[0], t[1].start))

    clusters: list[tuple[str, list[Episode]]] = []
    for etype, ep in enriched:
        if clusters and clusters[-1][0] == etype and (
            ep.start - max(e.end for e in clusters[-1][1])
        ) <= pd.Timedelta(hours=merge_gap_h):
            clusters[-1][1].append(ep)
        else:
            clusters.append((etype, [ep]))

    cards: list[EventCard] = []
    for etype, eps in clusters:
        onset = min(e.start for e in eps)
        end = max(e.end for e in eps)
        level = max_level([e.level for e in eps])
        driving = max(eps, key=lambda e: level_rank(e.level))

        # 可信度：以間接推估為主者降級；資料為觀測值者較高
        proxy_only = all(e.inference == "proxy" for e in eps)
        confidence = 0.55 if proxy_only else 0.85

        # 驅動參數逐項只留最強的一筆：同一場事件會被多條規則各自命中，
        # 若不去重，事件卡會出現六筆重複的 AP_AVG，對 SDA 端是雜訊。
        best: dict[str, Episode] = {}
        for e in eps:
            cur = best.get(e.peak_param)
            if cur is None or (not pd.isna(e.peak_value) and e.peak_value > cur.peak_value):
                best[e.peak_param] = e
        drivers = []
        for param, e in sorted(best.items(), key=lambda kv: -level_rank(kv[1].level)):
            spec = reg.get(param)
            drivers.append(
                {
                    "param": param,
                    "name": spec.name_zh if spec else param,
                    "value": None if pd.isna(e.peak_value) else round(float(e.peak_value), 6),
                    "unit": spec.unit if spec else "1",
                    "peak_utc": _iso(e.peak_time),
                    "rule_id": e.rule_id,
                }
            )

        impacts = []
        for domain in sorted({e.domain for e in eps}):
            dom_eps = [e for e in eps if e.domain == domain]
            top = max(dom_eps, key=lambda e: level_rank(e.level))
            rule = top.rule
            impacts.append(
                {
                    "domain": domain,
                    "level": top.level,
                    "statement": " ".join((rule.impact or "").split()) if rule else "",
                    "metric": {top.peak_param: None if pd.isna(top.peak_value)
                               else round(float(top.peak_value), 6)},
                    "exclusions_checked": list(rule.exclusions) if rule else [],
                    "inference": top.inference,
                    "region": rule.region if rule else None,
                }
            )

        scale = driving.scale_hint
        if etype == "SOLAR_FLARE" and not pd.isna(driving.peak_value):
            scale = r_scale(float(driving.peak_value)) or scale

        recommendations, notify = [], []
        for e in eps:
            if not e.rule:
                continue
            for line in e.rule.action.splitlines():
                line = line.strip()
                if line and line not in recommendations:
                    recommendations.append(line)
            for who in e.rule.notify:
                if who not in notify:
                    notify.append(who)

        region = next((e.rule.region for e in eps if e.rule and e.rule.region), None)

        card = EventCard(
            event_id=_make_event_id(etype, onset),
            event_type=etype,
            mission_level=level,
            onset_utc=onset,
            end_utc=end,
            peak_utc=driving.peak_time,
            confidence=confidence,
            international_scale=scale,
            drivers=drivers,
            impacts=impacts,
            recommendations=recommendations,
            notify=notify,
            affected_region=region,
            rule_ids=sorted({e.rule_id for e in eps}),
            sources=_sources_for(store, {e.peak_param for e in eps}, onset, end),
        )
        if etype == "SOLAR_FLARE" and not pd.isna(driving.peak_value):
            card.orbit_products = {}
            card.drivers.insert(
                0,
                {
                    "param": "FLARE_CLASS",
                    "name": "太陽閃焰分級",
                    "value": flux_to_class(float(driving.peak_value)),
                    "unit": "1",
                    "peak_utc": _iso(driving.peak_time),
                    "rule_id": driving.rule_id,
                },
            )
        cards.append(card)

    cards.sort(key=lambda c: c.onset_utc)
    return cards


def _make_event_id(event_type: str, onset: pd.Timestamp) -> str:
    prefix = {
        "GEOMAGNETIC_STORM": "GS",
        "SOLAR_FLARE": "FL",
        "SOLAR_RADIATION_STORM": "SR",
        "IONOSPHERIC_SCINTILLATION": "IS",
        "IONOSPHERIC_DISTURBANCE": "ID",
        "SOLAR_ACTIVITY_OUTLOOK": "OL",
    }.get(event_type, "SW")
    return f"SWX-{pd.Timestamp(onset).strftime('%Y%m%dT%H%M')}-{prefix}"


def _sources_for(store: SwxStore, params: set[str], start, end) -> list[str]:
    try:
        df = store.query(sorted(params), start=start, end=end)
        return sorted(set(df["source_id"].dropna().astype(str))) if not df.empty else []
    except Exception:
        return []


class EventStore:
    """事件卡作業狀態庫（SQLite）。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else data_dir() / "swx_ops.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as con:
            con.executescript(DDL)

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def upsert(self, card: EventCard, *, actor: str = "system") -> EventCard:
        """寫入事件卡。同一 event_id 內容有變時建立新 revision 並標記前版 superseded。"""
        with self._conn() as con:
            rows = con.execute(
                "SELECT revision, payload FROM event_card WHERE event_id=? ORDER BY revision DESC",
                (card.event_id,),
            ).fetchall()

            if rows:
                latest = json.loads(rows[0]["payload"])
                candidate = card.to_dict()
                # 比較時忽略發布時間與版次，只看實質內容
                for key in ("issued_utc", "revision", "supersedes"):
                    latest.pop(key, None)
                    candidate.pop(key, None)
                if latest == candidate:
                    card.revision = rows[0]["revision"]
                    return card
                card.revision = rows[0]["revision"] + 1
                card.supersedes = f"{card.event_id}@r{rows[0]['revision']}"
                con.execute(
                    "UPDATE event_card SET status=? WHERE event_id=? AND revision=?",
                    (STATUS_SUPERSEDED, card.event_id, rows[0]["revision"]),
                )

            payload = card.to_dict()
            con.execute(
                "INSERT INTO event_card (event_id, revision, issued_utc, status, mission_level,"
                " event_type, onset_utc, end_utc, supersedes, payload)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    card.event_id, card.revision, _iso(card.issued_utc), card.status,
                    card.mission_level, card.event_type, _iso(card.onset_utc),
                    _iso(card.end_utc), card.supersedes, json.dumps(payload, ensure_ascii=False),
                ),
            )
            self._audit(con, actor, "upsert_event_card", card.event_id,
                        f"revision={card.revision} level={card.mission_level}")
        return card

    def issue(self, event_id: str, *, actor: str) -> bool:
        """人工確認後發布（L3 以上必經此步，架構書 §11）。"""
        with self._conn() as con:
            cur = con.execute(
                "UPDATE event_card SET status=? WHERE event_id=? AND status=?"
                " AND revision=(SELECT max(revision) FROM event_card WHERE event_id=?)",
                (STATUS_ISSUED, event_id, STATUS_DRAFT, event_id),
            )
            if cur.rowcount:
                self._audit(con, actor, "issue_event_card", event_id, None)
            return bool(cur.rowcount)

    def latest(self, event_id: str) -> dict | None:
        with self._conn() as con:
            row = con.execute(
                "SELECT payload FROM event_card WHERE event_id=? ORDER BY revision DESC LIMIT 1",
                (event_id,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_events(self, *, min_level: str | None = None, limit: int = 100) -> list[dict]:
        sql = (
            "SELECT ec.payload FROM event_card ec JOIN ("
            "  SELECT event_id, max(revision) AS r FROM event_card GROUP BY event_id"
            ") m ON ec.event_id=m.event_id AND ec.revision=m.r"
            " ORDER BY ec.onset_utc DESC LIMIT ?"
        )
        with self._conn() as con:
            rows = con.execute(sql, (limit,)).fetchall()
        cards = [json.loads(r["payload"]) for r in rows]
        if min_level:
            cards = [c for c in cards
                     if level_rank(c["mission_level"]) >= level_rank(min_level)]
        return cards

    def history(self, event_id: str) -> list[dict]:
        with self._conn() as con:
            rows = con.execute(
                "SELECT payload FROM event_card WHERE event_id=? ORDER BY revision",
                (event_id,),
            ).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def audit_trail(self, limit: int = 200) -> pd.DataFrame:
        with self._conn() as con:
            rows = con.execute(
                "SELECT * FROM audit_log ORDER BY seq DESC LIMIT ?", (limit,)
            ).fetchall()
        return pd.DataFrame([dict(r) for r in rows])

    @staticmethod
    def _audit(con: sqlite3.Connection, actor: str, action: str,
               subject: str | None, detail: str | None) -> None:
        con.execute(
            "INSERT INTO audit_log (ts_utc, actor, action, subject, detail) VALUES (?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), actor, action, subject, detail),
        )
