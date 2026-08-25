"""services.ingest.base — 介接器統一契約（架構書 §5.1、§5.3）。

每個資料源實作一個 Connector，行為由 configs/sources.yaml 驅動而非寫死。
共通行為集中在此：

  原始落地（raw landing）  先把抓到的位元組原封存檔，再解析入庫。
                           解析錯誤可重跑，不需要重新向外抓取。
  退避重試                 網路瞬斷不應該讓整個排程失敗。
  降級（degraded）         主來源不可用時回退本地檔並明確標示，
                           而不是安靜地顯示舊值假裝正常（架構書 P5）。
"""

from __future__ import annotations

import ssl
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import certifi
import requests

from swx_core import SourceSpec, SwxStore, apply_quality, empty_frame


class _RelaxedStrictAdapter(requests.adapters.HTTPAdapter):
    """關閉 X.509 STRICT 一致性檢查的 HTTPS 轉接器。

    **這不是關閉憑證驗證。** 憑證鏈仍會對 CA 信任庫完整驗證，主機名仍會比對；
    唯一放寬的是 RFC 5280 對憑證擴充欄位的嚴格一致性檢查。

    為何需要：Python 3.12 起預設開啟 `VERIFY_X509_STRICT`，會拒絕中介憑證
    缺少 Subject Key Identifier 的憑證鏈。部分政府機關的 CA（實測 TAIWAN-CA）
    屬此情形——curl 與瀏覽器可通，Python 不通。這是對方憑證的一致性問題，
    不是信任問題。

    刻意做成**逐來源明示啟用**（sources.yaml 的 `tls_relaxed_strict: true`），
    不做成全域預設：全域關閉會讓未來任何一個真正有問題的憑證安靜通過。
    """

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context(cafile=certifi.where())
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def _session_for(spec: SourceSpec) -> requests.Session:
    sess = requests.Session()
    if bool(spec.raw.get("tls_relaxed_strict", False)):
        sess.mount("https://", _RelaxedStrictAdapter())
    return sess


@dataclass
class FetchOutcome:
    """一次擷取的結果。即使失敗也回傳物件，讓排程器能記錄而非拋例外中斷。"""

    source_id: str
    ok: bool
    rows: int = 0
    written: int = 0
    skipped: int = 0
    mode: str = "remote"           # remote / local_fallback / skipped
    raw_path: Path | None = None
    error: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    elapsed_s: float = 0.0

    def __str__(self) -> str:
        if not self.ok:
            return f"✗ {self.source_id}: {self.error}"
        return (
            f"✓ {self.source_id} [{self.mode}] 解析 {self.rows} 列，"
            f"寫入 {self.written}（未變更 {self.skipped}），{self.elapsed_s:.1f}s"
        )


class Connector(ABC):
    """資料源介接器基底。"""

    #: 對應 sources.yaml 的 format 欄位，供 registry 選擇實作
    formats: tuple[str, ...] = ()

    def __init__(self, spec: SourceSpec, store: SwxStore, *, timeout_s: int = 30,
                 retry_attempts: int = 3, retry_backoff_s: float = 5.0) -> None:
        self.spec = spec
        self.store = store
        self.timeout_s = timeout_s
        self.retry_attempts = retry_attempts
        self.retry_backoff_s = retry_backoff_s

    # ── 子類別必須實作 ──────────────────────────────────────────────────
    @abstractmethod
    def parse(self, payload: bytes) -> pd.DataFrame:
        """把原始位元組解析為 swx_observation 契約的 DataFrame。"""

    #: 原始檔副檔名，用於落地路徑
    raw_ext: str = "txt"

    # ── 共通流程 ────────────────────────────────────────────────────────
    def fetch_bytes(self) -> tuple[bytes, str]:
        """取得原始位元組，回傳 (payload, mode)。

        遠端失敗時退回 local_fallback；兩者皆無則拋例外。
        """
        errors: list[str] = []
        if self.spec.endpoint:
            for attempt in range(1, self.retry_attempts + 1):
                try:
                    with _session_for(self.spec) as sess:
                        resp = sess.get(self.spec.endpoint, timeout=self.timeout_s)
                    resp.raise_for_status()
                    return resp.content, "remote"
                except Exception as exc:  # noqa: BLE001 - 需容忍任何網路層例外
                    errors.append(f"attempt{attempt}: {type(exc).__name__}: {exc}")
                    if attempt < self.retry_attempts:
                        time.sleep(self.retry_backoff_s * attempt)

        local = self.spec.local_path()
        if local and local.exists():
            return local.read_bytes(), "local_fallback"

        raise RuntimeError(
            f"{self.spec.source_id} 無法取得資料：{'; '.join(errors) or '未設定 endpoint'}"
            + ("（且無 local_fallback）" if not local else f"（local_fallback 不存在：{local}）")
        )

    def latest_raw(self) -> Path | None:
        """本來源最新一份原始落地檔。找不到時回 None。"""
        root = self.store.raw_root / self.spec.source_id
        if not root.exists():
            return None
        files = sorted(root.rglob(f"*.{self.raw_ext.lstrip('.')}"))
        return files[-1] if files else None

    def run(
        self,
        *,
        ingest_time: datetime | None = None,
        dedupe: bool = True,
        backfill: bool = False,
        reparse: bool = False,
    ) -> FetchOutcome:
        """完整擷取流程：抓取 → 原始落地 → 解析 → 品質標記 → 寫入。

        backfill=True 時，每列的 ingest_time 改推算為
        `valid_time + publication_lag_s`，而不是「現在」。

        reparse=True 時不向外抓取，改解析最新一份原始落地檔。原始落地的用意
        本來就是「解析錯誤可重跑，不需重抓」，但在此之前沒有任何入口能做到
        這件事——解析規則改了（例如放寬 window_days 以回填歷史）只能重新下載
        整份檔案。此模式讓那句話成立，也讓回填不必再打擾來源站台。

        為什麼需要這個模式：回填的歷史資料若一律標成今天入庫，as_of 回放到
        2024 年會查不到任何東西——這在語意上是對的（我們當年確實沒有這筆資料），
        但議題七的歷史事件回放就無從進行。backfill 模式以來源的典型發布延遲
        重建「當時可取得性」，讓回放具有意義。此為**近似**，須在驗證報告中載明。
        """
        started = datetime.now(timezone.utc)
        outcome = FetchOutcome(source_id=self.spec.source_id, ok=False, started_at=started)
        try:
            if reparse:
                raw_path = self.latest_raw()
                if raw_path is None:
                    raise RuntimeError(
                        f"{self.spec.source_id} 沒有原始落地檔可重新解析"
                        f"（{self.store.raw_root / self.spec.source_id}）"
                    )
                payload, mode = raw_path.read_bytes(), f"reparse:{raw_path.name}"
            else:
                payload, mode = self.fetch_bytes()
                raw_path = self.store.raw_path(self.spec.source_id, started, self.raw_ext)
                raw_path.write_bytes(payload)
            outcome.mode = mode
            outcome.raw_path = raw_path

            df = self.parse(payload)
            df = apply_quality(df) if not df.empty else empty_frame()
            outcome.rows = len(df)

            if backfill and not df.empty:
                lag = pd.Timedelta(seconds=self.spec.publication_lag_s)
                df = df.copy()
                df["ingest_time"] = df["valid_time"] + lag
                outcome.mode += "+backfill"
                stamp = None
            else:
                stamp = ingest_time or started

            result = self.store.write(
                df, source_id=self.spec.source_id, ingest_time=stamp, dedupe=dedupe,
            )
            outcome.written = result.rows_written
            outcome.skipped = result.rows_skipped
            outcome.ok = True
        except Exception as exc:  # noqa: BLE001 - 單一來源失敗不應中斷整批排程
            outcome.error = f"{type(exc).__name__}: {exc}"
        finally:
            outcome.elapsed_s = (datetime.now(timezone.utc) - started).total_seconds()
        return outcome

    # ── 工具 ────────────────────────────────────────────────────────────
    def tag(self, df: pd.DataFrame) -> pd.DataFrame:
        """補上來源相關欄位。"""
        if df.empty:
            return df
        out = df.copy()
        out["source_id"] = self.spec.source_id
        out["source_tier"] = self.spec.tier
        return out
