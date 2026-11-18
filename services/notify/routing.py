"""services.notify.routing — 角色 → 投遞通道的路由（configs/notify.yaml）。

刻意不 import 事件卡模組：EventStore.issue() 在交易內呼叫本模組決定要寫哪些
待送訊息，反向相依會形成循環。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from swx_core import config_dir

CHANNEL_TYPES = ("file", "webhook")

#: 角色在路由表裡找不到時使用的通道名。這類訊息直接標 failed——
#: 寧可在待送清單上留一筆看得見的失敗，也不要靜默少通報一個單位。
UNROUTED = "__unrouted__"


@dataclass(frozen=True)
class Channel:
    name: str
    type: str
    options: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Routing:
    channels: dict[str, Channel]
    recipients: dict[str, tuple[str, ...]]
    max_attempts: int = 5
    backoff_s: float = 60.0
    timeout_s: float = 10.0

    def channels_for(self, recipient: str) -> tuple[str, ...]:
        """該角色的通道名；未定義者回傳 (UNROUTED,)。"""
        return self.recipients.get(recipient) or (UNROUTED,)


def load_routing(path: Path | None = None) -> Routing:
    path = Path(path) if path else config_dir() / "notify.yaml"
    doc = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}
    d = doc.get("defaults") or {}

    channels: dict[str, Channel] = {}
    for name, spec in (doc.get("channels") or {}).items():
        spec = dict(spec or {})
        ctype = spec.pop("type", None)
        if ctype not in CHANNEL_TYPES:
            raise ValueError(f"notify.yaml 通道 {name} 的 type={ctype!r} 不在 {CHANNEL_TYPES}")
        if ctype == "webhook" and "url" in spec:
            # 位址常含權杖；寫進版控就等於公開
            raise ValueError(f"notify.yaml 通道 {name}：webhook 位址須用 url_env，不可寫死 url")
        channels[name] = Channel(name, ctype, spec)

    recipients: dict[str, tuple[str, ...]] = {}
    for who, names in (doc.get("recipients") or {}).items():
        names = tuple(names or ())
        missing = [n for n in names if n not in channels]
        if missing:
            raise ValueError(f"notify.yaml 角色 {who} 指向未定義的通道 {missing}")
        recipients[str(who)] = names

    return Routing(
        channels=channels,
        recipients=recipients,
        max_attempts=int(d.get("max_attempts", 5)),
        backoff_s=float(d.get("backoff_s", 60)),
        timeout_s=float(d.get("timeout_s", 10)),
    )
