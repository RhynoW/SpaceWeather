"""swx_core.cssi — CSSI Space Weather 檔案格式（讀＋寫）。

本模組是 CSSI 格式的**唯一**權威實作：擷取端（services/ingest/celestrak_sw.py）
與匯出端（services/exporter/stk_spaceweather.py）共用同一組欄位定義，
確保「讀進來的」與「寫出去給 STK 的」永遠是同一種格式。

格式來源（已對實際檔案驗證）：
  · CelesTrak SW-All.txt 標頭自帶 `FORMAT(I4,I3,I3,I5,I3,8I3,I4,8I4,I4,F4.1,I2,I4,F6.1,I2,5F6.1)`
  · GMAT R2025a `src/base/solarsys/SolarFluxReader.cpp` 以 `theLine.substr(92)` 起讀 F10.7
    區塊，與本模組 F107_ADJ 起始位置 92 一致；Kp 欄位為 ×10 之整數（GMAT 讀入後除以 10）。

欄位順序注意：**文字檔是 Adj 在前、Obs 在後**（與 CelesTrak CSV 版相反），
中間夾一個整數品質旗標 Q。這是誤植高風險處，故以固定位置表達而非依賴分隔。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

SECTION_OBSERVED = "OBSERVED"
SECTION_DAILY_PREDICTED = "DAILY_PREDICTED"
SECTION_MONTHLY_PREDICTED = "MONTHLY_PREDICTED"
SECTIONS = (SECTION_OBSERVED, SECTION_DAILY_PREDICTED, SECTION_MONTHLY_PREDICTED)

# CelesTrak 的資料型別語彙（本專案 schema.data_type 沿用之）
SECTION_DATA_TYPE = {
    SECTION_OBSERVED: "OBS",
    SECTION_DAILY_PREDICTED: "PRD",
    SECTION_MONTHLY_PREDICTED: "PRM",
}

LINE_WIDTH = 130

# (欄名, 起始索引, 結束索引, 型別)  ── 0-based，[start, end)
FIELDS: list[tuple[str, int, int, str]] = (
    [
        ("year", 0, 4, "int"),
        ("month", 4, 7, "int"),
        ("day", 7, 10, "int"),
        ("bsrn", 10, 15, "int"),
        ("nd", 15, 18, "int"),
    ]
    + [(f"kp{i + 1}", 18 + 3 * i, 21 + 3 * i, "int") for i in range(8)]
    + [("kp_sum", 42, 46, "int")]
    + [(f"ap{i + 1}", 46 + 4 * i, 50 + 4 * i, "int") for i in range(8)]
    + [
        ("ap_avg", 78, 82, "int"),
        ("cp", 82, 86, "float"),
        ("c9", 86, 88, "int"),
        ("isn", 88, 92, "int"),
        ("f107_adj", 92, 98, "float"),
        ("q", 98, 100, "int"),
        ("f107_adj_c81", 100, 106, "float"),
        ("f107_adj_l81", 106, 112, "float"),
        ("f107_obs", 112, 118, "float"),
        ("f107_obs_c81", 118, 124, "float"),
        ("f107_obs_l81", 124, 130, "float"),
    ]
)

KP_COLS = [f"kp{i}" for i in range(1, 9)]
AP_COLS = [f"ap{i}" for i in range(1, 9)]


# ── 讀 ──────────────────────────────────────────────────────────────────
def parse_line(line: str) -> dict:
    """解析單行；空白欄位回傳 None（月預測段的 Kp/Ap 即為空白）。"""
    padded = line.ljust(LINE_WIDTH)
    row: dict[str, object] = {}
    for name, start, end, kind in FIELDS:
        raw = padded[start:end].strip()
        if not raw:
            row[name] = None
        elif kind == "int":
            row[name] = int(float(raw))
        else:
            row[name] = float(raw)
    return row


def parse_text(text: str) -> pd.DataFrame:
    """解析整份 CSSI 檔，回傳寬表（每日一列，含 section 欄）。"""
    rows: list[dict] = []
    section: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("BEGIN "):
            section = stripped[len("BEGIN "):].strip()
            continue
        if stripped.startswith("END "):
            section = None
            continue
        if section is None or not stripped or stripped.startswith("#"):
            continue
        if len(stripped) < 10 or not stripped[:4].isdigit():
            continue
        row = parse_line(line)
        row["section"] = section
        rows.append(row)

    if not rows:
        return pd.DataFrame(columns=[f[0] for f in FIELDS] + ["section", "date"])

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(
        dict(year=df["year"], month=df["month"], day=df["day"]), utc=True
    )
    return df.sort_values("date").reset_index(drop=True)


def parse_file(path: str | Path) -> pd.DataFrame:
    return parse_text(Path(path).read_text(encoding="utf-8", errors="replace"))


def parse_header(text: str) -> dict:
    """取出 DATATYPE / VERSION / UPDATED / NUM_*_POINTS 等標頭資訊。"""
    info: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("BEGIN "):
            break
        parts = s.split(None, 1)
        if len(parts) == 2:
            info[parts[0]] = parts[1].strip()
    return info


# ── 寫 ──────────────────────────────────────────────────────────────────
def _fmt(value, start: int, end: int, kind: str, name: str) -> str:
    width = end - start
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return " " * width
    if name in ("month", "day"):           # I3 但實際為前導空白 + 兩位零填
        return f" {int(value):02d}"
    if kind == "int":
        return f"{int(round(float(value))):{width}d}"
    if name == "cp":                        # F4.1
        return f"{float(value):{width}.1f}"
    return f"{float(value):{width}.1f}"     # F6.1


def format_line(row: dict | pd.Series) -> str:
    """把一列寬表資料格式化為 130 字元的 CSSI 資料行。"""
    get = row.get if isinstance(row, dict) else (lambda k, d=None: row[k] if k in row else d)
    out = []
    for name, start, end, kind in FIELDS:
        out.append(_fmt(get(name, None), start, end, kind, name))
    line = "".join(out)
    assert len(line) == LINE_WIDTH, f"CSSI 行長度應為 {LINE_WIDTH}，實得 {len(line)}"
    return line


def write_text(df: pd.DataFrame, *, updated: datetime | None = None) -> str:
    """由寬表產生完整 CSSI 檔內容（含標頭與三個區段）。

    df 需含 FIELDS 各欄與 `section` 欄；缺少的欄位以空白輸出。
    """
    updated = updated or datetime.now(timezone.utc)
    counts = {s: int((df["section"] == s).sum()) for s in SECTIONS}

    lines = [
        "DATATYPE CssiSpaceWeather",
        "VERSION 1.2",
        f"UPDATED {updated:%Y %b %d %H:%M:%S} UTC",
        "#" + "-" * 132,
        "#                              SPACE WEATHER DATA",
        "#" + "-" * 132,
        "#",
        "# 由 SWX-SDA 產生（swx_core.cssi）。格式與 CelesTrak CssiSpaceWeather v1.2 相同，",
        "# 可直接供 STK / GMAT 之 CSSI 太空天氣檔讀取。",
        "#",
        "# FORMAT(I4,I3,I3,I5,I3,8I3,I4,8I4,I4,F4.1,I2,I4,F6.1,I2,5F6.1)",
        "#" + "-" * 132,
    ]

    for section in SECTIONS:
        sub = df[df["section"] == section].sort_values("date")
        key = {
            SECTION_OBSERVED: "NUM_OBSERVED_POINTS",
            SECTION_DAILY_PREDICTED: "NUM_DAILY_PREDICTED_POINTS",
            SECTION_MONTHLY_PREDICTED: "NUM_MONTHLY_PREDICTED_POINTS",
        }[section]
        lines.append(f"{key} {counts[section]}")
        lines.append(f"BEGIN {section}")
        lines.extend(format_line(r) for _, r in sub.iterrows())
        lines.append(f"END {section}")

    return "\n".join(lines) + "\n"


def write_file(df: pd.DataFrame, path: str | Path, *, updated: datetime | None = None) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(write_text(df, updated=updated), encoding="utf-8", newline="\n")
    return p


# ── 轉為 swx_observation 長表 ──────────────────────────────────────────
#   Kp 在檔中為 ×10 之整數，轉存時還原為真實 Kp（0–9）。
PARAM_MAP_DAILY = {
    "f107_obs": ("F107_OBS", "sfu", 1.0),
    "f107_adj": ("F107_ADJ", "sfu", 1.0),
    "f107_obs_c81": ("F107_OBS_C81", "sfu", 1.0),
    "f107_adj_c81": ("F107_ADJ_C81", "sfu", 1.0),
    "f107_obs_l81": ("F107_OBS_L81", "sfu", 1.0),
    "f107_adj_l81": ("F107_ADJ_L81", "sfu", 1.0),
    "ap_avg": ("AP_AVG", "nT", 1.0),
    "isn": ("ISN", "1", 1.0),
    "cp": ("CP", "1", 1.0),
    "c9": ("C9", "1", 1.0),
    "q": ("F107_Q", "1", 1.0),
}


def kp_from_file(value: float) -> float:
    """檔內 Kp×10 整數 → 真實 Kp。

    Kp 的定義域是三分位（0, 1/3, 2/3, 1, 4/3 …），觀測值存的是四捨五入到整數的
    Kp×10（1/3 → 3、2/3 → 7）。若天真地除以 10 會得到 0.3 而非 0.333，
    再乘回去就還原不了原值，且 KpSum 會系統性偏低——CelesTrak 的 KpSum 是
    **未量化真值**的總和。故此處還原到最近的三分位。

    注意：僅適用於**觀測值**。預測段的 Kp 不在三分位格點上（實檔可見 22、24），
    強行套用三分位會把 24 變成 23，故預測值一律走 raw/10（見 to_observations）。
    """
    return round(float(value) * 3.0 / 10.0) / 3.0


def kp_to_file(kp: float) -> int:
    """真實 Kp → 檔內 Kp×10 整數。"""
    return int(round(float(kp) * 10.0))


def to_observations(df: pd.DataFrame, *, source_id: str, source_tier: int = 1) -> pd.DataFrame:
    """把 CSSI 寬表展開為 swx_observation 長表。

    · 日尺度參數（F10.7 系列、Ap 日均、ISN）取當日 00:00 UTC 為 valid_time
    · Kp/ap 逐 3 小時展開為 8 筆，valid_time 為該時段起始
    """
    from .schema import normalize

    recs: list[dict] = []
    for _, row in df.iterrows():
        date = row["date"]
        data_type = SECTION_DATA_TYPE.get(row.get("section", ""), "OBS")

        for col, (code, unit, scale) in PARAM_MAP_DAILY.items():
            val = row.get(col)
            if val is None or pd.isna(val):
                continue
            recs.append(
                {
                    "valid_time": date,
                    "param_code": code,
                    "value": float(val) * scale,
                    "unit": unit,
                    "source_id": source_id,
                    "source_tier": source_tier,
                    "data_type": data_type,
                }
            )

        for i in range(8):
            offset = pd.Timedelta(hours=3 * i)
            kp = row.get(f"kp{i + 1}")
            if kp is not None and not pd.isna(kp):
                # 觀測值還原至三分位；預測值不在三分位格點上，直接除以 10
                kp_value = (
                    kp_from_file(kp) if data_type in ("OBS", "INT") else float(kp) / 10.0
                )
                recs.append(
                    {
                        "valid_time": date + offset,
                        "param_code": "KP_3H",
                        "value": kp_value,
                        "unit": "1",
                        "source_id": source_id,
                        "source_tier": source_tier,
                        "data_type": data_type,
                    }
                )
            ap = row.get(f"ap{i + 1}")
            if ap is not None and not pd.isna(ap):
                recs.append(
                    {
                        "valid_time": date + offset,
                        "param_code": "AP_3H",
                        "value": float(ap),
                        "unit": "nT",
                        "source_id": source_id,
                        "source_tier": source_tier,
                        "data_type": data_type,
                    }
                )

    return normalize(pd.DataFrame(recs))


# swx_observation.data_type → CSSI 區段（SECTION_DATA_TYPE 的反向表）
DATA_TYPE_SECTION = {
    "OBS": SECTION_OBSERVED,
    "INT": SECTION_OBSERVED,
    "PRD": SECTION_DAILY_PREDICTED,
    "PRM": SECTION_MONTHLY_PREDICTED,
    "FCS": SECTION_DAILY_PREDICTED,   # 本系統自產預報併入日預測段
}


def from_observations(
    obs: pd.DataFrame,
    *,
    observed_until: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """由 swx_observation 長表回組 CSSI 寬表（匯出用）。

    區段依各列的 data_type 判定（PRM 必須落在 MONTHLY_PREDICTED——STK 對月預測段
    的內插方式與日預測段不同，混放會改變軌道傳播結果）。
    僅在資料無 data_type 可依據時，才退回以 observed_until 切分。
    """
    if obs.empty:
        return pd.DataFrame(columns=[f[0] for f in FIELDS] + ["section", "date"])

    df = obs.copy()
    df["date"] = df["valid_time"].dt.floor("D")
    df["slot"] = (df["valid_time"] - df["date"]).dt.total_seconds() // 10800

    # 日尺度欄位只採當日 00:00 的那一筆。若不設限，任何把日值逐時重複的來源
    # （例如 OMNI）都會在同一天產生多列，而後續的逐列寫入是「最後一筆勝出」，
    # 權威來源的值會被較低階來源覆蓋——且不會報錯，只是匯出檔悄悄變錯。
    daily_codes = {code for code, _unit, _scale in PARAM_MAP_DAILY.values()}
    is_daily = df["param_code"].isin(daily_codes)
    df = df[~is_daily | (df["valid_time"] == df["date"])]

    # 同一 (參數, 時間) 仍有多來源時，讓 tier 最小者最後寫入而勝出
    if "source_tier" in df.columns:
        df = df.sort_values(["date", "param_code", "source_tier"], ascending=[True, True, False])

    wide: dict[pd.Timestamp, dict] = {}

    def cell(day: pd.Timestamp) -> dict:
        if day not in wide:
            wide[day] = {
                "year": day.year, "month": day.month, "day": day.day,
                "bsrn": None, "nd": None, "kp_sum": None, "cp": None, "c9": None,
                "isn": None, "q": None, "date": day, "_types": set(),
            }
        return wide[day]

    inv_daily = {code: col for col, (code, _u, _s) in PARAM_MAP_DAILY.items()}
    for _, r in df.iterrows():
        code = str(r["param_code"])
        day = r["date"]
        cell(day)["_types"].add(str(r.get("data_type") or "OBS"))
        if code in inv_daily:
            cell(day)[inv_daily[code]] = r["value"]
        elif code == "KP_3H":
            cell(day)[f"kp{int(r['slot']) + 1}"] = kp_to_file(r["value"])
            cell(day).setdefault("_kp_true", {})[int(r["slot"])] = float(r["value"])
        elif code == "AP_3H":
            cell(day)[f"ap{int(r['slot']) + 1}"] = float(r["value"])

    out = pd.DataFrame(list(wide.values())).sort_values("date").reset_index(drop=True)

    # KpSum 的算法在觀測段與預測段不同（已對 CelesTrak 實檔驗證）：
    #   觀測段：由**未量化**的 Kp 真值加總再取整。例 0.333×2 → 7 而非 3+3=6。
    #   預測段：預測值本身就是量化後的整數，直接加總。例 27×8 = 216。
    # 兩者混用會在其中一段產生 ±3 的系統性偏差。
    if "_kp_true" in out.columns:
        out["_kp_sum_true"] = out["_kp_true"].apply(
            lambda d: int(round(sum(d.values()) * 10)) if isinstance(d, dict) and d else None
        )
        out["_kp_sum_int"] = out["_kp_true"].apply(
            lambda d: int(sum(round(v * 10) for v in d.values()))
            if isinstance(d, dict) and d else None
        )
        out = out.drop(columns=["_kp_true"])
    ap_present = [c for c in AP_COLS if c in out.columns]
    if ap_present and "ap_avg" not in out.columns:
        out["ap_avg"] = out[ap_present].mean(axis=1)

    if "q" not in out.columns:
        out["q"] = None

    def _section(types: set[str]) -> str:
        # 一天內若混有多種型別，取最「不確定」者（OBS < PRD < PRM）
        for t in ("PRM", "FCS", "PRD", "INT", "OBS"):
            if t in types:
                return DATA_TYPE_SECTION[t]
        return SECTION_OBSERVED

    if out["_types"].map(bool).any():
        out["section"] = out["_types"].map(_section)
    else:
        cutoff = observed_until or out["date"].max()
        out["section"] = out["date"].apply(
            lambda d: SECTION_OBSERVED if d <= cutoff else SECTION_DAILY_PREDICTED
        )
    out = out.drop(columns=["_types"])
    # 觀測段若無來源旗標則補 0（CelesTrak 慣例）；預測段留空白
    mask = (out["section"] == SECTION_OBSERVED) & out["q"].isna()
    out.loc[mask, "q"] = 0
    out.loc[out["section"] != SECTION_OBSERVED, "q"] = None

    if "_kp_sum_true" in out.columns:
        observed = out["section"] == SECTION_OBSERVED
        out["kp_sum"] = out["_kp_sum_int"].where(~observed, out["_kp_sum_true"])
        out = out.drop(columns=["_kp_sum_true", "_kp_sum_int"])
    for name, *_ in FIELDS:
        if name not in out.columns:
            out[name] = None
    return out
