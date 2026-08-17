"""services.ingest.run — 擷取排程進入點。

用法：
  python -m services.ingest.run --list                 列出來源與狀態
  python -m services.ingest.run --source all           跑所有 ready 的來源
  python -m services.ingest.run --source celestrak_sw_all
  python -m services.ingest.run --source all --offline 只用 local_fallback（封閉網路演練）

設計重點：**單一來源失敗不中斷整批**。每個來源各自回傳 FetchOutcome，
最後彙整成一張表；這對「外部來源異動／中斷」的風險（架構書 §15）是必要行為。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import pandas as pd

from swx_core import SourceSpec, SwxStore, catalog

from .base import Connector, FetchOutcome
from .celestrak_sw import CelestrakCsvConnector, CelestrakSpaceWeatherConnector
from .gfz_nowcast import GfzNowcastConnector
from .swpc_json import SwpcJsonConnector
from .swpc_solar import SwpcFlareConnector, SwpcSolarRegionsConnector
from .omni import OmniConnector
from .geomag_sources import GfzHp30Connector, SwpcOvationConnector
from .forecast_sources import (
    KyotoDstConnector,
    Swpc27DayOutlookConnector,
    SwpcDrapConnector,
    SwpcGeomagForecastConnector,
)

CONNECTORS: tuple[type[Connector], ...] = (
    CelestrakSpaceWeatherConnector,
    CelestrakCsvConnector,
    GfzNowcastConnector,
    SwpcJsonConnector,
    SwpcFlareConnector,
    SwpcSolarRegionsConnector,
    SwpcGeomagForecastConnector,
    Swpc27DayOutlookConnector,
    KyotoDstConnector,
    SwpcDrapConnector,
    OmniConnector,
    GfzHp30Connector,
    SwpcOvationConnector,
)


def build(spec: SourceSpec, store: SwxStore) -> Connector:
    """依 sources.yaml 的 format 欄挑選 connector 實作。"""
    cat = catalog()
    for cls in CONNECTORS:
        if spec.fmt in cls.formats:
            return cls(
                spec,
                store,
                timeout_s=cat.timeout_s,
                retry_attempts=cat.retry_attempts,
                retry_backoff_s=cat.retry_backoff_s,
            )
    raise ValueError(f"來源 {spec.source_id} 的 format={spec.fmt!r} 沒有對應的 connector")


def run_source(source_id: str, store: SwxStore, *, offline: bool = False,
               ingest_time: datetime | None = None,
               backfill: bool = False, year: int | None = None) -> FetchOutcome:
    spec = catalog()[source_id]
    if not spec.is_ready:
        return FetchOutcome(source_id, ok=False, mode="skipped",
                            error=f"status={spec.status}（來源尚未建置）")
    conn = build(spec, store)
    if year is not None and hasattr(conn, "year"):
        conn.year = year
    if offline:
        conn.spec = SourceSpec(**{**spec.__dict__, "endpoint": None})
    return conn.run(ingest_time=ingest_time, backfill=backfill)


def run_omni_years(store: SwxStore, *, backfill: bool = True,
                   years: int | None = None) -> list[FetchOutcome]:
    """OMNI2 每年一檔，逐年抓取。預設一律 backfill（它本來就是歷史資料）。"""
    spec = catalog()["omni2_hourly"]
    n = years or int(spec.raw.get("years", 8))
    this_year = datetime.now(timezone.utc).year
    out = []
    for y in range(this_year - n + 1, this_year + 1):
        out.append(run_source("omni2_hourly", store, backfill=True, year=y))
        out[-1].source_id = f"omni2_hourly[{y}]"
    return out


def run_all(store: SwxStore, *, offline: bool = False,
            backfill: bool = False) -> list[FetchOutcome]:
    stamp = datetime.now(timezone.utc)
    results = []
    for spec in catalog():
        if not spec.is_ready:
            continue
        if spec.source_id == "omni2_hourly":
            continue        # 逐年抓取，另以 --source omni2_hourly 執行
        results.append(run_source(spec.source_id, store, offline=offline,
                                  ingest_time=stamp, backfill=backfill))
    return results


def _list_sources() -> None:
    rows = []
    for s in catalog():
        rows.append(
            {
                "source_id": s.source_id,
                "tier": s.tier,
                "status": s.status,
                "connector": s.connector or "—",
                "provides": ",".join(s.provides) or "—",
                "cadence_s": s.cadence_s,
            }
        )
    print(pd.DataFrame(rows).to_string(index=False))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SWX-SDA 資料擷取")
    ap.add_argument("--source", default=None, help="來源代碼，或 all")
    ap.add_argument("--list", action="store_true", help="列出來源")
    ap.add_argument("--offline", action="store_true", help="只用 local_fallback")
    ap.add_argument("--years", type=int, default=None,
                    help="OMNI2 回填年數（預設取 sources.yaml 的 years）")
    ap.add_argument("--backfill", action="store_true",
                    help="回填模式：ingest_time 依來源發布延遲推算，使 as_of 回放可用")
    args = ap.parse_args(argv)

    if args.list or not args.source:
        _list_sources()
        return 0

    store = SwxStore()
    if args.source == "all":
        outcomes = run_all(store, offline=args.offline, backfill=args.backfill)
    elif args.source == "omni2_hourly":
        outcomes = run_omni_years(store, backfill=args.backfill, years=args.years)
    else:
        outcomes = [run_source(args.source, store, offline=args.offline,
                               backfill=args.backfill)]

    for o in outcomes:
        print(o)

    failed = [o for o in outcomes if not o.ok and o.mode != "skipped"]
    print(f"\n完成 {len(outcomes) - len(failed)}/{len(outcomes)} 個來源")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
