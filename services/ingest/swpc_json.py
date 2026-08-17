"""services.ingest.swpc_json — NOAA SWPC 即時通道（架構書 C1）。

這些是本案相對 Sat_TraingDataExtension 全新的通道（該案僅有 F10.7 與 Kp/Ap）。

SWPC 有三種 JSON 慣例，由 sources.yaml 的 format 欄決定：

  swpc_object_json    物件陣列，欄名即參數（RTSW 磁場／太陽風、行星際 Kp 屬此類）
  swpc_energy_json    物件陣列，另以 energy 欄區分能段（GOES X-ray、積分質子）
  swpc_array_json     二維陣列，第一列為欄名（SWPC 舊版 products/* 仍有此格式）

備註：SWPC 的端點會改版（本專案開發期間 `products/solar-wind/*-2-hour.json` 已 404，
改為 `json/rtsw/rtsw_*_1m.json`）。這正是架構書 P5「來源可替換」的實例——
端點寫在 sources.yaml，改版時只需改設定，不必動程式。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd

from swx_core import QUALITY_SUSPECT, empty_frame, normalize

from .base import Connector

# 能段字串 → param_code
_ENERGY_MAP = {
    # GOES XRS
    "0.1-0.8nm": ("XRAY_LONG", "W/m^2"),
    "0.05-0.4nm": ("XRAY_SHORT", "W/m^2"),
    # GOES 積分質子
    ">=10 MeV": ("PROT10", "pfu"),
    ">=100 MeV": ("PROT100", "pfu"),
}

# 物件欄名（小寫）→ (param_code, unit)
_FIELD_MAP = {
    "bz_gsm": ("IMF_BZ", "nT"),
    "bt": ("IMF_BT", "nT"),
    "proton_speed": ("SW_V", "km/s"),
    "proton_density": ("SW_N", "cm^-3"),
    "proton_temperature": ("SW_T", "K"),
    "speed": ("SW_V", "km/s"),
    "density": ("SW_N", "cm^-3"),
    "temperature": ("SW_T", "K"),
    "kp": ("KP_3H", "1"),
    "estimated_kp": ("KP_3H", "1"),
    "kp_index": ("KP_3H", "1"),
}

_TIME_KEYS = ("time_tag", "time-tag", "timestamp")


class SwpcJsonConnector(Connector):
    formats = ("swpc_object_json", "swpc_energy_json", "swpc_array_json")
    raw_ext = "json"

    def parse(self, payload: bytes) -> pd.DataFrame:
        data = json.loads(payload.decode("utf-8", errors="replace"))
        fmt = self.spec.fmt

        if fmt == "swpc_energy_json":
            df = self._parse_energy(data)
        elif fmt == "swpc_object_json":
            df = self._parse_objects(data)
        elif fmt == "swpc_array_json":
            df = self._parse_array(data)
        else:
            raise ValueError(f"未知的 SWPC 格式：{fmt}")

        if df.empty:
            return empty_frame()

        # 只保留本來源宣告提供的參數，避免夾帶未註冊欄位
        if self.spec.provides:
            df = df[df["param_code"].isin(self.spec.provides)]
        if df.empty:
            return empty_frame()

        # 視窗限制：RTSW 檔可達數 MB／數十萬列，只取近期可大幅縮短首次擷取
        window_h = self.spec.raw.get("window_h")
        if window_h:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=float(window_h))
            df = df[df["valid_time"] >= cutoff]

        df["source_id"] = self.spec.source_id
        df["source_tier"] = self.spec.tier
        df["data_type"] = "OBS"
        return normalize(df)

    # ── 物件陣列（欄名即參數）──────────────────────────────────────────
    @staticmethod
    def _parse_objects(data) -> pd.DataFrame:
        if not isinstance(data, list) or not data:
            return pd.DataFrame()
        raw = pd.DataFrame(data)
        raw.columns = [str(c).strip().lower() for c in raw.columns]
        time_col = next((c for c in _TIME_KEYS if c in raw.columns), None)
        if time_col is None:
            return pd.DataFrame()
        ts = pd.to_datetime(raw[time_col], utc=True, errors="coerce")

        # RTSW 自帶品質旗標：overall_quality 非 0 表示該筆資料有疑慮
        quality = raw["overall_quality"] if "overall_quality" in raw.columns else None

        frames: list[pd.DataFrame] = []
        for col, (code, unit) in _FIELD_MAP.items():
            if col not in raw.columns:
                continue
            piece = pd.DataFrame(
                {
                    "valid_time": ts,
                    "param_code": code,
                    "value": pd.to_numeric(raw[col], errors="coerce"),
                    "unit": unit,
                }
            )
            if quality is not None:
                bad = pd.to_numeric(quality, errors="coerce").fillna(0) != 0
                piece["quality_flag"] = bad.map({True: QUALITY_SUSPECT, False: "good"})
                piece["quality_reason"] = bad.map({True: "swpc_overall_quality", False: None})
            frames.append(piece.dropna(subset=["valid_time", "value"]))

        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True)
        return out.drop_duplicates(subset=["valid_time", "param_code"], keep="first")

    # ── 物件陣列（含 energy 欄）────────────────────────────────────────
    @staticmethod
    def _parse_energy(data) -> pd.DataFrame:
        if not isinstance(data, list) or not data:
            return pd.DataFrame()
        raw = pd.DataFrame(data)
        if "energy" not in raw.columns or "time_tag" not in raw.columns:
            return pd.DataFrame()
        mapped = raw["energy"].map(_ENERGY_MAP)
        keep = mapped.notna()
        if not keep.any():
            return pd.DataFrame()
        raw = raw[keep]
        mapped = mapped[keep]
        return pd.DataFrame(
            {
                "valid_time": pd.to_datetime(raw["time_tag"], utc=True, errors="coerce"),
                "param_code": [m[0] for m in mapped],
                "value": pd.to_numeric(raw["flux"], errors="coerce"),
                "unit": [m[1] for m in mapped],
            }
        ).dropna(subset=["valid_time"])

    # ── 二維陣列（首列為欄名）──────────────────────────────────────────
    @staticmethod
    def _parse_array(data) -> pd.DataFrame:
        if not isinstance(data, list) or len(data) < 2:
            return pd.DataFrame()
        header = [str(h).strip().lower() for h in data[0]]
        raw = pd.DataFrame(data[1:], columns=header)
        time_col = next((c for c in _TIME_KEYS if c in raw.columns), None)
        if time_col is None:
            return pd.DataFrame()
        ts = pd.to_datetime(raw[time_col], utc=True, errors="coerce")

        frames: list[pd.DataFrame] = []
        for col, (code, unit) in _FIELD_MAP.items():
            if col not in raw.columns:
                continue
            frames.append(
                pd.DataFrame(
                    {
                        "valid_time": ts,
                        "param_code": code,
                        "value": pd.to_numeric(raw[col], errors="coerce"),
                        "unit": unit,
                    }
                ).dropna(subset=["valid_time", "value"])
            )
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True)
        return out.drop_duplicates(subset=["valid_time", "param_code"], keep="first")
