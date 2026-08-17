"""services.exporter.stk_spaceweather — STK / GMAT CSSI 太空天氣驅動檔匯出（架構書 §10.3 階段 A）。

這是本案與軌道計算的第一個介接點，也是成本最低的一個：資料本體就是我們已經擷取的
CelesTrak 參數，匯出只是把它寫回 CSSI 文字格式（swx_core.cssi 為格式的唯一實作）。

產出的檔案可直接放進 STK 的 CSSI 太空天氣檔路徑，HPOP 選用 NRLMSISE-00 或 JB2008
即會讀取；GMAT 亦讀同一格式（其 SolarFluxReader 解析位置已與本模組對齊）。

三種輸出模式：
  observed_only   只輸出觀測值（回放與驗證用，避免拿預測值當真值）
  with_source     觀測 + 來源自帶的預測（CelesTrak PRD/PRM，預設）
  with_forecast   觀測 + 本系統 6 小時預報（data_type=FCS），供事件期間傳播使用
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from swx_core import DATA_TYPE_FCS, SwxStore, cssi

# CSSI 檔需要的參數（缺少的欄位會以空白輸出，STK 仍可讀）
EXPORT_PARAMS = [
    "F107_OBS", "F107_ADJ", "F107_OBS_C81", "F107_ADJ_C81",
    "F107_OBS_L81", "F107_ADJ_L81",
    "KP_3H", "AP_3H", "AP_AVG", "ISN", "CP", "C9", "F107_Q",
]

MODE_OBSERVED = "observed_only"
MODE_SOURCE = "with_source"
MODE_FORECAST = "with_forecast"


def build_frame(
    store: SwxStore,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    as_of: datetime | None = None,
    mode: str = MODE_SOURCE,
) -> pd.DataFrame:
    """由資料層組出 CSSI 寬表。"""
    obs = store.query(
        EXPORT_PARAMS,
        start=start,
        end=end,
        as_of=as_of,
        observed_only=(mode == MODE_OBSERVED),
    )
    if obs.empty:
        raise RuntimeError(
            "資料層沒有可匯出的太空天氣參數；請先執行 "
            "`python -m services.ingest.run --source celestrak_sw_all`"
        )

    if mode == MODE_FORECAST:
        keep = obs["data_type"].isin(["OBS", "INT", DATA_TYPE_FCS])
        obs = obs[keep]

    observed_until = obs.loc[obs["data_type"].isin(["OBS", "INT"]), "valid_time"].max()
    wide = cssi.from_observations(obs, observed_until=observed_until)

    # 補上 CSSI 慣例欄位：BSRN（Bartels 太陽自轉序號）與 ND（自轉內日序）。
    # STK/GMAT 不使用這兩欄（GMAT 明確跳過），但保留可與 CelesTrak 原檔比對。
    wide = _fill_bartels(wide)
    return wide


def _fill_bartels(wide: pd.DataFrame) -> pd.DataFrame:
    """依 Bartels 27 天自轉週期補 BSRN / ND（起算日 1832-02-08 為第 1 轉第 1 天）。"""
    if wide.empty:
        return wide
    epoch = pd.Timestamp("1832-02-08", tz="UTC")
    days = (wide["date"] - epoch).dt.days
    wide["bsrn"] = (days // 27) + 1
    wide["nd"] = (days % 27) + 1
    return wide


def export(
    store: SwxStore | None = None,
    *,
    path: str | Path | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    as_of: datetime | None = None,
    mode: str = MODE_SOURCE,
) -> Path:
    """產生 CSSI 檔並回傳路徑。"""
    store = store or SwxStore()
    wide = build_frame(store, start=start, end=end, as_of=as_of, mode=mode)
    out = Path(path) if path else store.root / "exports" / "SpaceWeather-All-v1.2.txt"
    cssi.write_file(wide, out, updated=datetime.now(timezone.utc))
    return out


def summary(wide: pd.DataFrame) -> dict:
    """匯出內容摘要（供 API 回應與匯出報告）。"""
    if wide.empty:
        return {"rows": 0}
    return {
        "rows": int(len(wide)),
        "date_min": wide["date"].min().date().isoformat(),
        "date_max": wide["date"].max().date().isoformat(),
        "sections": {k: int(v) for k, v in wide["section"].value_counts().items()},
        "f107_max": float(pd.to_numeric(wide["f107_obs"], errors="coerce").max()),
        "ap_max": float(pd.to_numeric(wide["ap_avg"], errors="coerce").max()),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="匯出 STK/GMAT CSSI 太空天氣驅動檔")
    ap.add_argument("--out", default=None, help="輸出路徑")
    ap.add_argument("--mode", default=MODE_SOURCE,
                    choices=[MODE_OBSERVED, MODE_SOURCE, MODE_FORECAST])
    ap.add_argument("--as-of", default=None, help="回放模式：只用該時刻前已知的資料")
    ap.add_argument("--days", type=int, default=None, help="只輸出近 N 天（含來源預測）")
    args = ap.parse_args(argv)

    store = SwxStore()
    start = None
    if args.days:
        start = datetime.now(timezone.utc) - timedelta(days=args.days)
    as_of = pd.Timestamp(args.as_of, tz="UTC").to_pydatetime() if args.as_of else None

    wide = build_frame(store, start=start, as_of=as_of, mode=args.mode)
    out = Path(args.out) if args.out else store.root / "exports" / "SpaceWeather-All-v1.2.txt"
    cssi.write_file(wide, out, updated=datetime.now(timezone.utc))

    info = summary(wide)
    print(f"已匯出 {out}")
    print(f"  期間 {info['date_min']} → {info['date_max']}，共 {info['rows']} 天")
    print(f"  區段 {info['sections']}")
    print(f"  F10.7 最大 {info['f107_max']:.1f} sfu、Ap 最大 {info['ap_max']:.0f} nT")
    print("\n用法：將此檔放入 STK 的 CSSI 太空天氣檔路徑，HPOP 選 NRLMSISE-00 / JB2008 即生效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
