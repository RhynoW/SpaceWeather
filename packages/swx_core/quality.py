"""swx_core.quality — 品質控管管線（架構書 §6.4）。

設計沿用 Sat_TraingDataExtension/data_quality_audit.py 的三級制與「規則＋成因字串」：
  rejected  物理不可能（超出值域、NaN）——不可用
  suspect   可用但存疑（突波、缺口內插、跨源不一致）——需人工複核
  good      以上皆未觸發

關鍵原則：**補值不覆蓋原值**。內插結果以新列寫入並標 suspect + interpolated，
原始列保留，儀表板預設顯示原始值。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .params import ParamRegistry, registry
from .schema import (
    DATA_TYPE_INT,
    QUALITY_GOOD,
    QUALITY_REJECTED,
    QUALITY_SUSPECT,
    normalize,
)


def apply_quality(
    df: pd.DataFrame,
    *,
    reg: ParamRegistry | None = None,
    check_spike: bool = True,
) -> pd.DataFrame:
    """對觀測 frame 逐列賦予 quality_flag 與 quality_reason。

    已被 connector 標為 rejected/suspect 的列不會被降級，只會補上更嚴重的判定。
    """
    reg = reg or registry()
    out = normalize(df)
    if out.empty:
        return out

    flags = out["quality_flag"].fillna(QUALITY_GOOD).to_numpy(dtype=object)
    reasons = out["quality_reason"].fillna("").to_numpy(dtype=object)

    def escalate(mask: np.ndarray, level: str, reason: str) -> None:
        rank = {QUALITY_GOOD: 0, QUALITY_SUSPECT: 1, QUALITY_REJECTED: 2}
        for i in np.flatnonzero(mask):
            if rank[level] >= rank.get(flags[i], 0):
                if rank[level] > rank.get(flags[i], 0):
                    flags[i] = level
                reasons[i] = f"{reasons[i]};{reason}".strip(";")

    # 1) 缺值
    escalate(out["value"].isna().to_numpy(), QUALITY_REJECTED, "missing")

    # 2) 值域檢核（來自 param_registry）
    for code, grp in out.groupby("param_code", sort=False):
        spec = reg.get(str(code))
        if spec is None:
            escalate(out.index.isin(grp.index).__and__(np.ones(len(out), bool)),
                     QUALITY_REJECTED, "unregistered_param")
            continue
        idx = out.index.isin(grp.index)
        vals = out["value"].to_numpy()
        if spec.valid_min is not None:
            escalate(idx & (vals < spec.valid_min), QUALITY_REJECTED, "below_valid_min")
        if spec.valid_max is not None:
            escalate(idx & (vals > spec.valid_max), QUALITY_REJECTED, "above_valid_max")

        # 3) 突波／梯度檢核
        #    門檻以「每個名目更新週期的變化量」表示，而非固定每小時——否則同一個
        #    數字對 1 分鐘級與 1 日級參數的意義會差三個數量級，日尺度參數等於沒檢查。
        #    取樣間隔大於名目週期時（資料有缺口）按實際間隔換算，避免缺口後誤報。
        if check_spike and spec.spike_limit is not None and len(grp) >= 2:
            g = grp.sort_values("valid_time")
            cadence_s = float(spec.cadence_s or 3600)
            dt_s = g["valid_time"].diff().dt.total_seconds()
            steps = (dt_s / cadence_s).clip(lower=1.0)
            rate = g["value"].diff().abs() / steps
            spike_idx = g.index[(rate > spec.spike_limit).fillna(False)]
            escalate(out.index.isin(spike_idx), QUALITY_SUSPECT, "spike")

    out["quality_flag"] = pd.Series(flags, index=out.index, dtype="string")
    out["quality_reason"] = (
        pd.Series(reasons, index=out.index, dtype="string").replace("", pd.NA)
    )
    return out


def cross_source_check(
    df: pd.DataFrame,
    *,
    rel_tol: float = 0.2,
    abs_tol: float = 1.0,
) -> pd.DataFrame:
    """跨源一致性檢核：同一 (param, valid_time) 有多來源時比對主來源。

    偏離主來源（tier 最小者）超過容差的列標 suspect + cross_source_mismatch。
    """
    out = normalize(df)
    if out.empty:
        return out

    key = ["param_code", "valid_time"]
    primary = (
        out.sort_values(["source_tier", "ingest_time"])
        .groupby(key, as_index=False)
        .first()[key + ["value"]]
        .rename(columns={"value": "_ref"})
    )
    merged = out.merge(primary, on=key, how="left")
    diff = (merged["value"] - merged["_ref"]).abs()
    tol = merged["_ref"].abs() * rel_tol + abs_tol
    bad = (diff > tol).fillna(False).to_numpy()

    flags = out["quality_flag"].to_numpy(dtype=object)
    reasons = out["quality_reason"].fillna("").to_numpy(dtype=object)
    for i in np.flatnonzero(bad):
        if flags[i] == QUALITY_GOOD:
            flags[i] = QUALITY_SUSPECT
        reasons[i] = f"{reasons[i]};cross_source_mismatch".strip(";")
    out["quality_flag"] = pd.Series(flags, index=out.index, dtype="string")
    out["quality_reason"] = (
        pd.Series(reasons, index=out.index, dtype="string").replace("", pd.NA)
    )
    return out


def interpolate_gaps(
    df: pd.DataFrame,
    param_code: str,
    *,
    freq: str,
    limit: int = 3,
) -> pd.DataFrame:
    """對單一參數補齊時間格點，產生**新列**（不覆蓋原值）。

    回傳僅含補值列的 frame，quality_flag=suspect、quality_reason=interpolated、
    data_type=INT。呼叫端負責與原資料一起寫入。
    """
    out = normalize(df)
    sub = out[out["param_code"] == param_code].sort_values("valid_time")
    if len(sub) < 2:
        return out.iloc[0:0]

    grid = pd.date_range(sub["valid_time"].min(), sub["valid_time"].max(), freq=freq, tz="UTC")
    s = sub.set_index("valid_time")["value"].reindex(grid)
    filled = s.interpolate(method="time", limit=limit, limit_direction="both")
    new_idx = s.isna() & filled.notna()
    if not new_idx.any():
        return out.iloc[0:0]

    template = sub.iloc[0]
    made = pd.DataFrame(
        {
            "valid_time": grid[new_idx],
            "param_code": param_code,
            "value": filled[new_idx].to_numpy(),
            "unit": template["unit"],
            "source_id": template["source_id"],
            "source_tier": template["source_tier"],
            "quality_flag": QUALITY_SUSPECT,
            "quality_reason": "interpolated",
            "confidence": 0.5,
            "data_type": DATA_TYPE_INT,
        }
    )
    return normalize(made)


def quality_summary(df: pd.DataFrame) -> pd.DataFrame:
    """品質旗標分布摘要（供儀表板「資料健康」頁與稽核報告）。"""
    out = normalize(df)
    if out.empty:
        return pd.DataFrame(columns=["param_code", "source_id", "n", "good", "suspect",
                                     "rejected", "good_rate"])
    g = (
        out.assign(_one=1)
        .pivot_table(
            index=["param_code", "source_id"],
            columns="quality_flag",
            values="_one",
            aggfunc="sum",
            fill_value=0,
        )
        .reset_index()
    )
    for col in (QUALITY_GOOD, QUALITY_SUSPECT, QUALITY_REJECTED):
        if col not in g.columns:
            g[col] = 0
    g["n"] = g[[QUALITY_GOOD, QUALITY_SUSPECT, QUALITY_REJECTED]].sum(axis=1)
    g["good_rate"] = g[QUALITY_GOOD] / g["n"].replace(0, np.nan)
    return g[["param_code", "source_id", "n", QUALITY_GOOD, QUALITY_SUSPECT,
              QUALITY_REJECTED, "good_rate"]].rename(
        columns={QUALITY_GOOD: "good", QUALITY_SUSPECT: "suspect",
                 QUALITY_REJECTED: "rejected"}
    )
