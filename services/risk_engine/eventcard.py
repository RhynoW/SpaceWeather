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
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from swx_core import SwxStore, data_dir, data_origin, registry
from swx_core.flare import flux_to_class, r_scale

from .engine import Episode, level_rank, max_level

SCHEMA_VERSION = "1.0"

STATUS_DRAFT = "draft"
STATUS_ISSUED = "issued"
STATUS_SUPERSEDED = "superseded"

NOTICE_SCHEMA = "swx.notification/1"
NOTIFY_PENDING = "pending"
NOTIFY_SENDING = "sending"
NOTIFY_SENT = "sent"
NOTIFY_FAILED = "failed"

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

-- 通報 outbox（services.notify）：發布時同交易寫入，投遞程序另外送出
CREATE TABLE IF NOT EXISTS notification_outbox (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id         TEXT NOT NULL,
    revision         INTEGER NOT NULL,
    recipient        TEXT NOT NULL,
    channel          TEXT NOT NULL,
    payload          TEXT NOT NULL,
    status           TEXT NOT NULL,      -- pending / sending / sent / failed
    attempts         INTEGER NOT NULL DEFAULT 0,
    created_utc      TEXT NOT NULL,      -- = 發布時刻
    next_attempt_utc TEXT NOT NULL,
    sent_utc         TEXT,
    last_error       TEXT,
    UNIQUE (event_id, revision, recipient, channel)
);
CREATE INDEX IF NOT EXISTS idx_outbox_due ON notification_outbox(status, next_attempt_utc);

-- append-only 由資料庫強制，而非只靠程式慣例：稽核表若能被 UPDATE／DELETE，
-- 它證明不了任何事。任何改寫或刪除都會被 SQLite 直接拒絕。
CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
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

    @contextmanager
    def _conn(self):
        """一次交易：成功提交、例外回滾，**結束時關閉連線**。

        sqlite3.Connection 本身的 context manager 只提交不關閉，連線會留到 GC。
        儀表板、API 與排程是三個行程同時開這個檔：WAL 讓讀者不擋寫者，
        busy_timeout 讓兩個寫者互相等待而不是立刻丟 `database is locked`。
        """
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA busy_timeout=10000")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

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

    def issue(self, event_id: str, *, actor: str, routing=None) -> bool:
        """人工確認後發布（L3 以上必經此步，架構書 §11）。

        只會發布**最新一版且仍為 draft** 者；單一 UPDATE 在 SQLite 內是原子的，
        兩位值勤員同時按發布，只有一人成功，另一人得到 False。

        發布成功時，**同一交易**依通報路由寫入待送訊息（transactional outbox，
        見 services.notify）。`routing` 預設讀 configs/notify.yaml。
        """
        if not actor or not str(actor).strip():
            raise ValueError("發布必須具名（actor 不可為空），否則稽核紀錄無從追溯")
        if routing is None:
            from services.notify.routing import load_routing

            routing = load_routing()
        with self._conn() as con:
            cur = con.execute(
                "UPDATE event_card SET status=? WHERE event_id=? AND status=?"
                " AND revision=(SELECT max(revision) FROM event_card WHERE event_id=?)",
                (STATUS_ISSUED, event_id, STATUS_DRAFT, event_id),
            )
            if not cur.rowcount:
                return False
            now = datetime.now(timezone.utc).isoformat()
            self._audit(con, actor, "issue_event_card", event_id, None)
            row = con.execute(
                "SELECT revision, payload FROM event_card WHERE event_id=?"
                " ORDER BY revision DESC LIMIT 1", (event_id,),
            ).fetchone()
            card = json.loads(row["payload"])
            card["status"] = STATUS_ISSUED
            self._enqueue(con, card, revision=row["revision"], actor=actor,
                          issued_at=now, routing=routing)
            return True

    # ── 通報 outbox ─────────────────────────────────────────────────────
    @staticmethod
    def _notice(card: dict, *, recipient: str, actor: str, issued_at: str,
                origin: dict) -> dict:
        """送給收件單位的訊息本體：判讀所需的最小集合，不是整張事件卡。"""
        t = card.get("timeline") or {}
        return {
            "schema": NOTICE_SCHEMA,
            "recipient": recipient,
            "event_id": card.get("event_id"),
            "revision": card.get("revision"),
            "type": card.get("type"),
            "mission_level": card.get("mission_level"),
            "international_scale": card.get("international_scale"),
            "onset_utc": t.get("onset_utc"),
            "expected_end_utc": t.get("expected_end_utc"),
            "affected_region": card.get("affected_region"),
            "impacts": [{"domain": i.get("domain"), "level": i.get("level"),
                         "statement": i.get("statement"), "inference": i.get("inference")}
                        for i in card.get("impacts") or []],
            "recommendations": card.get("recommendations") or [],
            "issued_by": actor,
            "issued_at_utc": issued_at,
            "detail_ref": f"/v1/events/{card.get('event_id')}",
            # 收件端不會看到儀表板的紅色橫幅。示範資料觸發的通報必須在訊息本身標明，
            # 否則一次演練就可能被當成真實事件處置。
            "data_origin": origin.get("data_origin"),
            "is_demo": bool(origin.get("is_demo")),
            "operational": bool(origin.get("operational")),
        }

    def _enqueue(self, con: sqlite3.Connection, card: dict, *, revision: int,
                 actor: str, issued_at: str, routing) -> int:
        from services.notify.routing import UNROUTED

        origin = data_origin()
        n = 0
        for recipient in card.get("notify") or []:
            for channel in routing.channels_for(recipient):
                body = self._notice(card, recipient=recipient, actor=actor,
                                    issued_at=issued_at, origin=origin)
                unrouted = channel == UNROUTED
                con.execute(
                    "INSERT OR IGNORE INTO notification_outbox (event_id, revision,"
                    " recipient, channel, payload, status, attempts, created_utc,"
                    " next_attempt_utc, last_error) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (card["event_id"], revision, recipient, channel,
                     json.dumps(body, ensure_ascii=False),
                     NOTIFY_FAILED if unrouted else NOTIFY_PENDING, 0, issued_at, issued_at,
                     f"角色「{recipient}」未在 configs/notify.yaml 定義通道" if unrouted
                     else None),
                )
                n += 1
        self._audit(con, actor, "enqueue_notifications", card["event_id"],
                    f"revision={revision} messages={n}")
        return n

    def claim_due(self, *, now: datetime | None = None, limit: int = 50,
                  lease_s: float = 300.0) -> list[dict]:
        """取出到期的待送訊息並標為 sending。

        逐筆條件式更新，多個投遞程序不會搶到同一筆。標 sending 時同時給一個
        租約（next_attempt_utc = now + lease_s）：投遞程序若在送出途中當掉，
        租約到期後該筆會再被取出，不會永遠卡在 sending。代價是極少數情況下
        重送一次——通報寧可重複，不可遺漏；收件端以 (event_id, revision) 去重。
        """
        from datetime import timedelta

        now = now or datetime.now(timezone.utc)
        now_s = now.isoformat()
        lease = (now + timedelta(seconds=lease_s)).isoformat()
        out = []
        with self._conn() as con:
            rows = con.execute(
                "SELECT * FROM notification_outbox WHERE status IN (?,?)"
                " AND next_attempt_utc<=? ORDER BY id LIMIT ?",
                (NOTIFY_PENDING, NOTIFY_SENDING, now_s, limit),
            ).fetchall()
            for r in rows:
                cur = con.execute(
                    "UPDATE notification_outbox SET status=?, next_attempt_utc=?"
                    " WHERE id=? AND status=? AND next_attempt_utc=?",
                    (NOTIFY_SENDING, lease, r["id"], r["status"], r["next_attempt_utc"]),
                )
                if cur.rowcount:
                    out.append(dict(r))
        return out

    def mark_sent(self, msg_id: int, *, detail: str | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as con:
            r = con.execute("SELECT event_id, recipient, channel, attempts"
                            " FROM notification_outbox WHERE id=?", (msg_id,)).fetchone()
            con.execute(
                "UPDATE notification_outbox SET status=?, sent_utc=?, attempts=attempts+1,"
                " last_error=NULL WHERE id=?", (NOTIFY_SENT, now, msg_id),
            )
            self._audit(con, "notifier", "notification_sent", r["event_id"],
                        f"id={msg_id} {r['recipient']}→{r['channel']}"
                        + (f" {detail}" if detail else ""))

    def mark_failed(self, msg_id: int, error: str, *, max_attempts: int,
                    backoff_s: float) -> str:
        """記錄一次失敗；未達上限則排定下次重試，否則標 failed。回傳新狀態。"""
        from datetime import timedelta

        with self._conn() as con:
            r = con.execute("SELECT event_id, recipient, channel, attempts"
                            " FROM notification_outbox WHERE id=?", (msg_id,)).fetchone()
            attempts = int(r["attempts"]) + 1
            final = attempts >= max_attempts
            status = NOTIFY_FAILED if final else NOTIFY_PENDING
            nxt = datetime.now(timezone.utc) + timedelta(
                seconds=backoff_s * (2 ** (attempts - 1)))
            con.execute(
                "UPDATE notification_outbox SET status=?, attempts=?, last_error=?,"
                " next_attempt_utc=? WHERE id=?",
                (status, attempts, error[:500], nxt.isoformat(), msg_id),
            )
            self._audit(con, "notifier",
                        "notification_failed" if final else "notification_retry",
                        r["event_id"],
                        f"id={msg_id} {r['recipient']}→{r['channel']} "
                        f"attempt={attempts}: {error[:200]}")
            return status

    def notifications(self, event_id: str | None = None, *, limit: int = 500) -> list[dict]:
        sql = "SELECT * FROM notification_outbox"
        args: tuple = ()
        if event_id:
            sql += " WHERE event_id=?"
            args = (event_id,)
        sql += " ORDER BY id DESC LIMIT ?"
        with self._conn() as con:
            rows = con.execute(sql, (*args, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.pop("payload", None)
            if d.get("sent_utc"):
                d["delivery_s"] = round((pd.Timestamp(d["sent_utc"])
                                         - pd.Timestamp(d["created_utc"])).total_seconds(), 1)
            out.append(d)
        return out

    @staticmethod
    def _card_from_row(row) -> dict:
        """把 DB 列還原成事件卡 dict，並以 **status 欄位**覆蓋 payload 內的值。

        payload 是寫入當下的 JSON 快照，其 `status` 停在寫入時的狀態；
        後續的 `issue()` 與 supersede 只更新 status 欄位。若直接回傳 payload，
        人工確認後 API 仍會回報 `draft`——狀態機等於沒有傳到呼叫端。
        欄位才是狀態的唯一真相來源。
        """
        card = json.loads(row["payload"])
        card["status"] = row["status"]
        return card

    def latest(self, event_id: str) -> dict | None:
        with self._conn() as con:
            row = con.execute(
                "SELECT payload, status FROM event_card WHERE event_id=?"
                " ORDER BY revision DESC LIMIT 1",
                (event_id,),
            ).fetchone()
        return self._card_from_row(row) if row else None

    def list_events(self, *, min_level: str | None = None, limit: int = 100) -> list[dict]:
        sql = (
            "SELECT ec.payload, ec.status FROM event_card ec JOIN ("
            "  SELECT event_id, max(revision) AS r FROM event_card GROUP BY event_id"
            ") m ON ec.event_id=m.event_id AND ec.revision=m.r"
            " ORDER BY ec.onset_utc DESC LIMIT ?"
        )
        with self._conn() as con:
            rows = con.execute(sql, (limit,)).fetchall()
        cards = [self._card_from_row(r) for r in rows]
        if min_level:
            cards = [c for c in cards
                     if level_rank(c["mission_level"]) >= level_rank(min_level)]
        return cards

    def history(self, event_id: str) -> list[dict]:
        with self._conn() as con:
            rows = con.execute(
                "SELECT payload, status FROM event_card WHERE event_id=? ORDER BY revision",
                (event_id,),
            ).fetchall()
        return [self._card_from_row(r) for r in rows]

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
