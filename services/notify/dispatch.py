"""services.notify.dispatch — 通報投遞程序。

    python -m services.notify.dispatch            # 送出到期訊息一輪後結束（給排程用）
    python -m services.notify.dispatch --loop 30  # 每 30 秒一輪，常駐
    python -m services.notify.dispatch --status   # 列出待送／失敗訊息

部署位置：Zone B 作業區（與作業狀態庫同機）。`file` 通道寫入的檔案可由
單向傳輸搬出；`webhook` 通道只應指向內網通報閘道（架構書 §12）。
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass

import requests

import services  # noqa: F401  （主控台 UTF-8、packages 路徑）
from swx_core import data_dir

from services.risk_engine.eventcard import NOTIFY_FAILED, NOTIFY_SENT, EventStore

from .routing import UNROUTED, Channel, Routing, load_routing

USER_AGENT = "SWX-SDA-notifier/0.1"


class DeliveryError(Exception):
    """可重試的投遞失敗。"""


def _deliver_file(ch: Channel, msg: dict, body: dict) -> str:
    root = data_dir() / ch.options.get("dir", "exports/notifications")
    day = str(msg["created_utc"])[:10].replace("-", "")
    out = root / day
    out.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(msg["event_id"]))
    path = out / f"{int(msg['id']):08d}-{safe}-r{msg['revision']}.json"
    tmp = path.with_suffix(".json.tmp")
    # 先寫暫存再改名：搬運程序只會看到完整的檔
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return str(path.relative_to(data_dir()))


def _deliver_webhook(ch: Channel, body: dict, timeout_s: float) -> str:
    env = ch.options.get("url_env")
    url = os.getenv(env or "", "")
    if not url:
        raise DeliveryError(f"環境變數 {env} 未設定，webhook 無位址")
    try:
        r = requests.post(url, json=body, timeout=timeout_s,
                          headers={"User-Agent": USER_AGENT,
                                   "Idempotency-Key": f"{body['event_id']}@r{body['revision']}"
                                                      f"/{body['recipient']}"})
    except requests.RequestException as exc:
        raise DeliveryError(f"{type(exc).__name__}: {exc}") from exc
    if r.status_code >= 300:
        raise DeliveryError(f"HTTP {r.status_code}")
    return f"HTTP {r.status_code}"


def deliver(ch: Channel, msg: dict, routing: Routing) -> str:
    body = json.loads(msg["payload"])
    if ch.type == "file":
        return _deliver_file(ch, msg, body)
    if ch.type == "webhook":
        return _deliver_webhook(ch, body, routing.timeout_s)
    raise DeliveryError(f"未知通道型別 {ch.type}")


@dataclass
class RunResult:
    sent: int = 0
    retry: int = 0
    failed: int = 0

    def __str__(self) -> str:
        return f"送出 {self.sent}｜待重試 {self.retry}｜放棄 {self.failed}"


def run_once(store: EventStore | None = None, routing: Routing | None = None) -> RunResult:
    store = store or EventStore()
    routing = routing or load_routing()
    res = RunResult()
    for msg in store.claim_due():
        ch = routing.channels.get(msg["channel"])
        try:
            if ch is None:
                # 發布後路由表被改掉、或歷史訊息指向已刪除的通道：不重試，直接留痕
                raise LookupError(f"通道 {msg['channel']} 已不在 configs/notify.yaml")
            detail = deliver(ch, msg, routing)
        except LookupError as exc:
            store.mark_failed(msg["id"], str(exc), max_attempts=1, backoff_s=0)
            res.failed += 1
            continue
        except Exception as exc:  # noqa: BLE001 — 任何投遞失敗都要記錄並重試
            status = store.mark_failed(msg["id"], str(exc),
                                       max_attempts=routing.max_attempts,
                                       backoff_s=routing.backoff_s)
            if status == NOTIFY_FAILED:
                res.failed += 1
            else:
                res.retry += 1
            continue
        store.mark_sent(msg["id"], detail=detail)
        res.sent += 1
    return res


def _print_status(store: EventStore) -> None:
    rows = [r for r in store.notifications() if r["status"] != NOTIFY_SENT]
    if not rows:
        print("無待送或失敗的通報。")
        return
    for r in rows:
        tag = "未定義路由" if r["channel"] == UNROUTED else r["channel"]
        print(f"#{r['id']:<5} {r['status']:<8} {r['event_id']} r{r['revision']}  "
              f"{r['recipient']} → {tag}  嘗試 {r['attempts']}  {r.get('last_error') or ''}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="事件卡通報投遞")
    ap.add_argument("--loop", type=float, metavar="SECONDS", help="常駐，每 N 秒一輪")
    ap.add_argument("--status", action="store_true", help="列出待送與失敗訊息")
    args = ap.parse_args(argv)

    store = EventStore()
    if args.status:
        _print_status(store)
        return 0
    while True:
        res = run_once(store)
        if res.sent or res.retry or res.failed or not args.loop:
            print(res)
        if not args.loop:
            return 0 if not res.failed else 1
        time.sleep(args.loop)


if __name__ == "__main__":
    raise SystemExit(main())
