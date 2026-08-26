"""services.ingest.refresh — 依資料齡期自動重新擷取（展示層用）。

**用途界線**：這是給展示層（儀表板／雲端站台）用的「畫面上的數字別太舊」機制，
不是資料倉儲的排程器。兩者差別很重要：

  展示層更新（本模組）  只抓近即時通道，寫入容器本地，重啟後重來。
                        目的是讓開啟頁面的人看到當下的太空天氣。
  作業級擷取（排程主機） 完整來源、含歷史回填、寫入持久化儲存。
                        本模組**不能取代**它——Streamlit Cloud 這類容器
                        檔案系統不持久、會因閒置休眠，不適合當資料倉儲。

**刻意排除的來源**：`omni2_hourly` 是六年份的歷史回填（數十 MB、數十秒），
拿來當即時更新只會拖垮頁面載入，且它本來就是事後重整資料、無即時價值。

**逐站台停用**：`SWX_DISABLE_SOURCES`（逗號分隔的 source_id）可在某個部署上
關掉個別來源，不必改 sources.yaml。用途是雲端展示站台因授權或連外限制
不宜抓取某來源時，只關那一站，排程主機與本機不受影響。

**冷啟動的提升行為**：雲端首次開啟時 `data/swx_parquet` 是空的，
`data_dir()` 會退回 `data/demo` 示範快照。本模組一律寫入**真實的 data/ 目錄**，
因此第一次成功更新後，`data_dir()` 就會改回 `data/`、DEMO 橫幅自動消失。
示範快照因而只是冷啟動的墊檔，不是永久狀態。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from swx_core import SwxStore, catalog, project_root
from swx_core.schema import OBSERVED_TYPES

# 自動更新一律排除的來源。
#   omni2_hourly  六年份歷史回填（數十秒～分鐘級），且本身是事後重整資料，無即時價值
#   gfz_hp30      120 天完整序列檔，**單一來源就要 46 秒**，佔全部擷取時間的六成
#   tacc_scn1c2   單日打包檔 **約 72 MB**，且需解壓近萬個 netCDF 再逐一解析
#   tacc_leoorb   需連續多日方能算出衰減率，單次要解析上千個 SP3 檔
# 三者都改由手動「完整更新」或排程主機處理，不放進頁面載入路徑。
EXCLUDE_FROM_REFRESH = frozenset({"omni2_hourly", "gfz_hp30", "tacc_scn1c2", "tacc_leoorb"})

# 手動完整更新才納入的重量級來源（實測：hp30 約 46s、tacc_scn1c2 約 72 MB）
HEAVY_SOURCES = frozenset({"gfz_hp30", "tacc_scn1c2", "tacc_leoorb"})

DEFAULT_MAX_AGE_S = 3600.0     # 60 分鐘


def live_data_root() -> Path:
    """真實資料目錄（非示範快照）。更新一律寫這裡。"""
    return project_root() / "data"


def disabled_sources() -> frozenset[str]:
    """由環境變數停用的來源（逗號分隔的 source_id）。

    設這個開關的理由是**部署層與程式碼要能分開**：雲端展示站台若因授權、
    法遵或連外限制而不該抓某個來源，應該在該站台的環境設定裡關掉，
    而不是改 sources.yaml——後者會同時關掉排程主機與本機開發環境。
    """
    raw = os.getenv("SWX_DISABLE_SOURCES", "")
    return frozenset(x.strip() for x in raw.split(",") if x.strip())


def live_sources(*, include_heavy: bool = False) -> list[str]:
    """自動更新納入的來源。

    實測各來源耗時（見 docs/operations_manual.md）：快速通道 16 個合計約 25–33 秒
    （其中 nlsc_egnss_i95 約 3–4 秒：一頁 HTML 加三張圖表），
    加上 gfz_hp30 則增為約 74 秒——後者不適合放在頁面載入路徑上。
    """
    skip = (EXCLUDE_FROM_REFRESH - (HEAVY_SOURCES if include_heavy else frozenset())
            ) | disabled_sources()
    return [s.source_id for s in catalog().ready() if s.source_id not in skip]


def last_ingest_time(store: SwxStore | None = None, *,
                     now: datetime | None = None) -> datetime | None:
    """資料層最近一次寫入的時間（不是觀測時間）。

    用 `ingest_time` 而非 `valid_time` 判斷「該不該重抓」：
    valid_time 舊可能只是那個通道本來就更新得慢，重抓也不會變新；
    ingest_time 舊才真的代表我們很久沒去拿資料了。

    兩個必要的過濾，少任何一個這個函式都會給出錯誤答案：

      只取觀測型別   CelesTrak 的月預測列 valid_time 遠到 2041 年，
                     回填模式據此推算的 ingest_time 也落在未來。
                     不濾掉的話 max() 會取到 2041，齡期變負值，
                     **自動更新永遠不會觸發**。
      排除未來時刻   同理，任何因回填近似而落在 now 之後的 ingest_time
                     都不代表「我們已經拿到資料」。
    """
    store = store or SwxStore(live_data_root())
    globs = store.globs()
    if not globs:
        return None
    cutoff = (now or datetime.now(timezone.utc)).isoformat()
    types = ",".join(repr(t) for t in OBSERVED_TYPES)
    sql = (
        f"SELECT max(ingest_time) AS t FROM read_parquet({globs!r}, union_by_name=true) "
        f"WHERE data_type IN ({types}) AND ingest_time <= TIMESTAMPTZ '{cutoff}'"
    )
    try:
        with store.connect() as con:
            value = con.execute(sql).fetchone()[0]
    except Exception:
        return None
    if value is None:
        return None
    ts = pd.Timestamp(value)
    return (ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")).to_pydatetime()


def data_age_s(store: SwxStore | None = None, *, now: datetime | None = None) -> float | None:
    now = now or datetime.now(timezone.utc)
    last = last_ingest_time(store, now=now)
    if last is None:
        return None
    return (now - last).total_seconds()


@dataclass
class RefreshResult:
    ran: bool
    reason: str
    age_before_s: float | None = None
    succeeded: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    rows_written: int = 0
    elapsed_s: float = 0.0
    #: 逐來源寫入列數。成功但寫 0 列與根本沒跑是兩件事，畫面上必須分得出來。
    rows: dict[str, int] = field(default_factory=dict)
    #: 逐來源的部分成功說明（如 e-GNSS 三個網只抓到兩個）。
    warnings: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: 本次納入的來源清單。不在其中即代表**根本沒去抓**，
    #: 與「抓了但失敗」要用不同的話說——前者去看設定，後者去看網路。
    attempted: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.ran and bool(self.succeeded)

    def status_of(self, source_id: str) -> tuple[str, str]:
        """單一來源在本次更新中的下場，回傳 (狀態碼, 人話)。

        狀態碼：ok／empty／failed／skipped／not_run。
        """
        if not self.ran:
            return "not_run", f"未執行更新（{self.reason}）"
        for sid, err in self.failed:
            if sid == source_id:
                return "failed", err
        if source_id in self.succeeded:
            note = "；".join(self.warnings.get(source_id, ()))
            rows = self.rows.get(source_id, 0)
            if rows == 0:
                return "empty", note or "擷取成功但沒有新資料寫入"
            return "ok", note or f"寫入 {rows} 列"
        if source_id not in self.attempted:
            return "skipped", "未納入本次自動更新"
        return "not_run", "本次更新未涵蓋"

    def summary(self) -> str:
        if not self.ran:
            return f"未更新（{self.reason}）"
        parts = [f"更新 {len(self.succeeded)}/{len(self.succeeded) + len(self.failed)} 個來源",
                 f"寫入 {self.rows_written} 列", f"{self.elapsed_s:.1f}s"]
        if self.failed:
            parts.append(f"失敗：{', '.join(sid for sid, _ in self.failed)}")
        return "，".join(parts)


def refresh_if_stale(
    *,
    max_age_s: float = DEFAULT_MAX_AGE_S,
    force: bool = False,
    sources: list[str] | None = None,
    include_heavy: bool = False,
    now: datetime | None = None,
    on_progress=None,
) -> RefreshResult:
    """資料齡期超過 max_age_s 時重新擷取近即時通道。

    單一來源失敗不影響其他來源——部分更新遠比整批放棄有用，
    失敗清單會回傳給呼叫端顯示，不靜默吞掉。
    """
    import time

    from .run import run_source

    store = SwxStore(live_data_root())
    age = data_age_s(store, now=now)

    if not force and age is not None and age <= max_age_s:
        return RefreshResult(ran=False, age_before_s=age,
                             reason=f"資料齡期 {age / 60:.0f} 分鐘，未超過 {max_age_s / 60:.0f} 分鐘")

    ids = sources if sources is not None else live_sources(include_heavy=include_heavy)
    started = time.monotonic()
    result = RefreshResult(
        ran=True, age_before_s=age,
        reason="強制更新" if force else ("尚無資料" if age is None
                                        else f"資料齡期 {age / 60:.0f} 分鐘，已逾時"),
    )

    result.attempted = list(ids)

    for i, source_id in enumerate(ids, 1):
        if on_progress is not None:
            on_progress(i, len(ids), source_id)
        try:
            outcome = run_source(source_id, store)
            if outcome.warnings:
                result.warnings[source_id] = tuple(outcome.warnings)
            if outcome.ok:
                result.succeeded.append(source_id)
                result.rows_written += outcome.written
                result.rows[source_id] = outcome.written
            else:
                result.failed.append((source_id, outcome.error or "未知錯誤"))
        except Exception as exc:      # noqa: BLE001 — 單一來源失敗不得中斷整體
            result.failed.append((source_id, f"{type(exc).__name__}: {exc}"))

    result.elapsed_s = time.monotonic() - started
    return result


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="依資料齡期自動重新擷取近即時通道")
    ap.add_argument("--max-age-min", type=float, default=60.0, help="超過幾分鐘就更新")
    ap.add_argument("--force", action="store_true", help="不論齡期一律更新")
    ap.add_argument("--status", action="store_true", help="只顯示齡期，不更新")
    ap.add_argument("--full", action="store_true", help="納入重量級來源（gfz_hp30）")
    args = ap.parse_args(argv)

    if args.status:
        age = data_age_s()
        last = last_ingest_time()
        print(f"最近入庫　{last or '（無資料）'}")
        print(f"資料齡期　{'—' if age is None else f'{age / 60:.1f} 分鐘'}")
        print(f"納入更新的來源（{len(live_sources())}）：{', '.join(live_sources())}")
        off = disabled_sources()
        if off:
            print(f"由 SWX_DISABLE_SOURCES 停用：{', '.join(sorted(off))}")
        return 0

    result = refresh_if_stale(max_age_s=args.max_age_min * 60.0, force=args.force,
                              include_heavy=args.full,
                              on_progress=lambda i, n, sid: print(f'  [{i}/{n}] {sid}'))
    print(result.summary())
    for source_id, err in result.failed:
        print(f"  ✗ {source_id}: {err[:120]}")
    return 0 if (not result.ran or result.ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
