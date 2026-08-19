"""services.ingest.tacc_leoorb — 由福衛七號精密定軌反演熱氣層阻力衰減率。

**這是 `ORBIT_PREDICTION` 網域第一個實測判據。** 在此之前該網域的四條規則
全部以 Kp/Ap 為門檻——那是地磁代理，不是熱氣層密度本身。

## 為什麼能繞開衛星參數

近圓軌道的長期衰減 `da/dt = -B * rho * sqrt(mu*a)`，其中 `B = Cd*A/m`。
福衛七號的投影面積、阻力係數與乾重**皆未公開**，故無法解出絕對密度。
但取同一顆衛星兩個時段的比值時 **B 完全消掉**：

    rho_storm / rho_quiet = (da/dt)_storm / (da/dt)_quiet

因此本模組交付的是 `DRAG_DECAY`（衰減率本身，m/日），
下游要的密度比值由它相除即得，不需要任何非公開的衛星參數，
也沒有「以 MSIS 擬合 B 再拿去反演 MSIS」的循環論證。

## 為什麼非用精密定軌不可

同樣的量測以 TLE 做過，失敗：550 km 每日衰減僅數十公尺，而 TLE 半長軸的
逐日雜訊達數百公尺，訊噪比約 0.07，六顆同型衛星擬合出的彈道係數相差 30%。
leoOrb 的弧段重疊一致性為 **0.25 m**，平滑後的估計雜訊約 5 m，
足以分辨 60 m/日 的平靜期基線與暴期的數百 m/日。

## 平均窗必須是軌道週期的整數倍

密切半長軸的短週期振幅達 **1.7 km**，遠大於每日數十公尺的衰減。
若以 6 小時或日曆日取平均，短週期會**混疊成假的長期趨勢**——
實測曾因此把 7.7 m/日 誤讀為 39 m/日，也曾把一次明確的機動誤判為「無機動」。
本模組以軌道週期（約 96.3 分鐘）為窗做中心移動平均，實測可把
逐點變異由 1118 m 壓到 13 m。

## 機動的處理

跨衛星取**中位數**而非平均：五、六顆同型同軌道面的衛星在同一時刻
承受同一組大氣，真實密度變化必為共模；單顆的機動則是離群值。
中位數對單顆離群穩健，而離散度超過門檻時標 `suspect`——
實測平靜期離散約 5–12%，Gannon 峰值仍僅 10%，故高離散代表污染而非真實差異。
"""

from __future__ import annotations

import io
import tarfile
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from swx_core import QUALITY_GOOD, QUALITY_SUSPECT, empty_frame, normalize

from .base import Connector

MU = 398600.4418
GPS_MINUS_UTC_S = 18.0        # 2026 年值（TAI-UTC=37, GPS=TAI-19）；新增閏秒須更新
BIN = "6h"                    # 交付節奏；短於此則衰減量接近估計雜訊
DISPERSION_SUSPECT = 0.25     # 跨衛星標準差／中位數的上限；超過即疑有機動或資料問題
MIN_SATS = 3                  # 少於此數不足以用中位數排除離群
MIN_COVERAGE = 0.8            # 分箱內平滑後樣本的最低覆蓋率

EOP_MEMBER = "eop.csv"
EOP_URL = "https://celestrak.org/SpaceData/EOP-Last5Years.csv"


def parse_sp3(text: str) -> tuple[str, pd.DataFrame] | None:
    """解析單一 SP3-c 檔。位置 km、速度 km/s、ITRF 地固框架。

    三個容易靜默出錯的慣例：
      · 時間是 **GPS 時**，與 UTC 差 18 秒（沿跡 128 km）
      · 速度單位是 **dm/s**，非 m/s
      · 速度是**地固**速度，轉慣性須加 omega x r（漏掉會讓半長軸偏低 736 km）
    """
    import re

    lines = text.splitlines()
    if len(lines) < 4 or not lines[0].startswith("#c"):
        return None
    m = re.search(r"\+\s+\d+\s+([A-Z]\d\d)", lines[2])
    if not m:
        return None

    rows: list[list] = []
    t: datetime | None = None
    for line in lines:
        if line.startswith("*  "):
            p = line[3:].split()
            if len(p) < 6:
                t = None
                continue
            t = datetime(int(p[0]), int(p[1]), int(p[2]), int(p[3]), int(p[4]),
                         tzinfo=timezone.utc) + timedelta(
                             seconds=float(p[5]) - GPS_MINUS_UTC_S)
        elif line.startswith("P") and t is not None:
            v = line[4:46].split()
            if len(v) >= 3:
                rows.append([t] + [float(x) for x in v[:3]] + [np.nan] * 3)
        elif line.startswith("V") and rows:
            v = line[4:46].split()
            if len(v) >= 3:
                rows[-1][4:7] = [float(x) / 10000.0 for x in v[:3]]   # dm/s → km/s
    if not rows:
        return None
    return m.group(1), pd.DataFrame(
        rows, columns=["t_utc", "x", "y", "z", "vx", "vy", "vz"])


def sma_from_sp3(frames: dict[str, list[pd.DataFrame]], eop: pd.DataFrame
                 ) -> dict[str, pd.Series]:
    """各衛星的密切半長軸時序（km），弧段重疊處取一筆。"""
    from orbit_drag.frames import itrf_to_teme

    out: dict[str, pd.Series] = {}
    for sat, arcs in frames.items():
        d = (pd.concat(arcs).dropna(subset=["vx"])
             .drop_duplicates("t_utc").sort_values("t_utc"))
        if len(d) < 10:
            continue
        r, v = itrf_to_teme(d[["x", "y", "z"]].values, d[["vx", "vy", "vz"]].values,
                            d["t_utc"], eop)
        a = 1.0 / (2.0 / np.linalg.norm(r, axis=1) - (v ** 2).sum(1) / MU)
        s = pd.Series(a, index=pd.DatetimeIndex(d["t_utc"])).sort_index()
        out[sat] = s[np.isfinite(s)]
    return out


def decay_rates(sma: dict[str, pd.Series]) -> pd.DataFrame:
    """由半長軸時序算逐 6 小時的衰減率（m/日，正值為衰減）。"""
    if not sma:
        return pd.DataFrame()
    a0 = float(np.mean([s.mean() for s in sma.values()]))
    period_s = 2.0 * np.pi * np.sqrt(a0 ** 3 / MU)
    win = max(2, int(round(period_s / 60.0)))          # 一個軌道週期的分鐘數

    smoothed = {}
    for sat, s in sma.items():
        u = s.resample("60s").mean().interpolate(limit=5)
        smoothed[sat] = u.rolling(win, center=True, min_periods=win).mean()
    sm = pd.DataFrame(smoothed)
    binned = sm.resample(BIN).mean()

    # 取樣不完整的分箱必須剔除，否則首尾會出現假影：移動平均在資料兩端各損失
    # 半個窗，該分箱的平均落在偏移的時刻上，差分出來的斜率因而失真
    # （實測首個分箱曾給出 32 m/日，而鄰接分箱是 52 m/日）。
    expected = pd.Timedelta(BIN).total_seconds() / 60.0
    coverage = sm.resample(BIN).count() / expected
    binned = binned.where(coverage >= MIN_COVERAGE)

    step_days = pd.Timedelta(BIN).total_seconds() / 86400.0
    return -(binned.diff() / step_days) * 1000.0


def rates_to_frame(rate: pd.DataFrame) -> pd.DataFrame:
    """跨衛星彙整為單一 DRAG_DECAY 序列。"""
    if rate.empty:
        return empty_frame()
    n = rate.notna().sum(axis=1)
    med = rate.median(axis=1)
    # **取值用中位數、偵測用標準差**，兩者的要求相反：
    # 交付值要對單顆機動穩健，而旗標要對單顆機動敏感。
    # 曾誤用中位數絕對偏差當離散度——n 只有五、六顆時，除非過半衛星偏離
    # 否則 MAD 恆為零，正好偵測不到「單顆離群」這個最需要偵測的情形。
    sd = rate.std(axis=1)
    keep = (n >= MIN_SATS) & med.notna() & (med > 0)
    med, sd, n = med[keep], sd[keep], n[keep]
    if med.empty:
        return empty_frame()

    disp = (sd / med).fillna(0.0)
    suspect = disp > DISPERSION_SUSPECT
    out = pd.DataFrame({
        "valid_time": med.index,
        "param_code": "DRAG_DECAY",
        "value": med.to_numpy(dtype=float),
        "unit": "m/day",
        "data_type": "OBS",
    })
    out["quality_flag"] = np.where(suspect, QUALITY_SUSPECT, QUALITY_GOOD)
    out["quality_reason"] = np.where(
        suspect,
        "cross_satellite_dispersion_" + disp.round(2).astype(str)
        + "（疑機動或資料缺口；平靜期實測約 0.05–0.12）",
        "",
    )
    return out


class TaccLeoOrbConnector(Connector):
    """TACC `leoOrb` 每日打包檔（SP3-c 精密定軌）。

    **需要連續多日**才能算出衰減率，故一次抓 `span_days` 天並打包成單一
    原始檔落地。重複覆蓋的 valid_time 由雙時間軸儲存處理——
    同一時刻以較新的 ingest_time 再寫一次正是該設計的用途。
    """

    formats = ("tacc_leoorb_tar",)
    raw_ext = "tar"

    def __init__(self, *args, date: str | None = None, span_days: int | None = None,
                 **kw) -> None:
        super().__init__(*args, **kw)
        self.date = date
        self._span = span_days
        self.resolved_dates: list[str] = []

    @property
    def span_days(self) -> int:
        return int(self._span or self.spec.raw.get("span_days", 4))

    def candidate_dates(self) -> list[str]:
        """要抓的日期，由新到舊。

        起點往前推一天：當日打包檔通常尚未產生。多抓一天是因為首尾各會
        因移動平均而損失半個窗，且第一個分箱沒有前值可差分。
        """
        if self.date is not None:
            base = datetime.strptime(self.date, "%Y.%j").replace(tzinfo=timezone.utc)
        else:
            base = datetime.now(timezone.utc) - timedelta(days=1)
        return [f"{d.year}.{d.timetuple().tm_yday:03d}"
                for d in (base - timedelta(days=k) for k in range(self.span_days))]

    def fetch_bytes(self) -> tuple[bytes, str]:
        """抓多日打包檔與 EOP，合併為單一 tar。

        EOP 一併納入原始檔：框架轉換沒有它就退化成數十公尺的偏差，
        把它和軌道資料放在一起，這份原始檔才足以獨立重現當次結果。
        """
        if not self.spec.endpoint:
            return super().fetch_bytes()

        original = self.spec.endpoint
        buf = io.BytesIO()
        got: list[str] = []
        errors: list[str] = []
        with tarfile.open(fileobj=buf, mode="w") as bundle:
            for date in self.candidate_dates():
                object.__setattr__(self.spec, "endpoint", original.format(date=date))
                try:
                    payload, _ = super().fetch_bytes()
                except Exception as exc:      # noqa: BLE001 — 逐日容錯
                    errors.append(f"{date}: {type(exc).__name__}")
                    continue
                finally:
                    object.__setattr__(self.spec, "endpoint", original)
                info = tarfile.TarInfo(f"leoOrb_{date}.tar.gz")
                info.size = len(payload)
                bundle.addfile(info, io.BytesIO(payload))
                got.append(date)

            if len(got) < 2:
                raise RuntimeError(
                    f"{self.spec.source_id} 取得 {len(got)} 天，不足以計算衰減率："
                    + "; ".join(errors))

            eop = self._fetch_eop()
            info = tarfile.TarInfo(EOP_MEMBER)
            info.size = len(eop)
            bundle.addfile(info, io.BytesIO(eop))

        self.resolved_dates = sorted(got)
        return buf.getvalue(), "remote"

    def _fetch_eop(self) -> bytes:
        import requests

        resp = requests.get(EOP_URL, timeout=self.timeout_s)
        resp.raise_for_status()
        return resp.content

    def parse(self, payload: bytes) -> pd.DataFrame:
        try:
            bundle = tarfile.open(fileobj=io.BytesIO(payload), mode="r")
        except tarfile.TarError:
            return empty_frame()

        frames: dict[str, list[pd.DataFrame]] = {}
        eop: pd.DataFrame | None = None
        with bundle:
            for member in bundle:
                if not member.isfile():
                    continue
                handle = bundle.extractfile(member)
                if handle is None:
                    continue
                raw = handle.read()
                if member.name == EOP_MEMBER:
                    d = pd.read_csv(io.BytesIO(raw)).dropna(subset=["UT1-UTC"])
                    eop = d.set_index("MJD")[["X", "Y", "UT1-UTC"]]
                    continue
                try:
                    inner = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
                except tarfile.TarError:
                    continue
                with inner:
                    for f in inner:
                        if not f.isfile():
                            continue
                        h = inner.extractfile(f)
                        if h is None:
                            continue
                        got = parse_sp3(h.read().decode("ascii", "replace"))
                        if got:
                            frames.setdefault(got[0], []).append(got[1])

        if eop is None or not frames:
            return empty_frame()

        df = rates_to_frame(decay_rates(sma_from_sp3(frames, eop)))
        if df.empty:
            return df
        df["source_id"] = self.spec.source_id
        df["source_tier"] = self.spec.tier
        return normalize(df)
