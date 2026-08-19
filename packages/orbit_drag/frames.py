"""orbit_drag.frames — TEME ↔ ITRF 座標轉換（含地球定向參數）。

SGP4 的輸出在 **TEME**（真赤道平春分，當日），而精密定軌產品（SP3、leoOrb）
在 **ITRF** 地固框架。兩者比對前必須先轉到同一框架，否則差異會被地球自轉角主導。

## 與 astropy 的關係

本模組最初是因為本機 `astropy 7.1.1` 與 `numpy 2.5.0` 不相容而寫
（`np.in1d` 已移除、astropy 又直接引用 numpy 私有符號
`_check_interpolation_as_method`，`astropy.time` 與 `astropy.coordinates` 皆無法 import）。
該環境問題已於 2026-08-19 由升級 `astropy 8.0.1` 解決。

保留本模組的理由改為：**不新增重量級相依**，且已對 astropy 完整驗證。
以福衛七號 leoOrb 七天、五顆、2,636 個實際軌道點比對
`astropy 8.0.1` 的 `ITRS→TEME`：

    平均 0.081 m ／ 中位 0.058 m ／ P95 0.228 m ／ 最大 0.275 m

遠低於 leoOrb 自身的弧段重疊一致性（0.25 m），對本專案用途與 astropy 等價。
需要完整 IAU 2006/2000A 鏈或其他框架時，仍應直接用 astropy。

## 為何一定要帶 EOP

省略地球定向參數不會報錯，只會靜默產生偏差。實測（LEO 550 km）：

  極移（x_p, y_p）     約 9.2 m，以法向為主
  ΔUT1                本週僅 3.8 m，但 2021–2027 的 ΔUT1 範圍是
                      −0.1856 → +0.0948 s，最壞達 94 m

ΔUT1 的影響隨時期變動極大，**做歷史回放時不可假設可忽略**。
在 GNSS MEO 高度（29,600 km）同樣的 ΔUT1 誤差會放大到約 401 m。

EOP 取自 CelesTrak（公開、免帳號）：
  https://celestrak.org/SpaceData/EOP-Last5Years.csv
"""

from __future__ import annotations

import numpy as np
import pandas as pd

OMEGA = 7.292115e-5              # rad/s，IERS 地球自轉率
ASEC2RAD = np.pi / 648000.0
EOP_URL = "https://celestrak.org/SpaceData/EOP-Last5Years.csv"


def load_eop(path_or_url: str = EOP_URL) -> pd.DataFrame:
    """讀 CelesTrak EOP CSV，回傳以 MJD 為索引的 X／Y／UT1-UTC。

    X、Y 單位為角秒，UT1-UTC 為秒。預報段（DATA_TYPE='P'）一併保留——
    精度略低但仍遠優於完全省略。
    """
    d = pd.read_csv(path_or_url).dropna(subset=["UT1-UTC"])
    return d.set_index("MJD")[["X", "Y", "UT1-UTC"]]


def _interp(eop: pd.DataFrame, mjd: np.ndarray) -> dict[str, np.ndarray]:
    idx = eop.index.values.astype(float)
    return {c: np.interp(mjd, idx, eop[c].values.astype(float)) for c in eop.columns}


def gmst82(jd_ut1: np.ndarray) -> np.ndarray:
    """IAU-1982 格林威治平恆星時（弧度）。

    TEME 的定義即以此角度對齊 PEF（虛地固框架），故此處必須用 1982 式，
    不可換成 IAU-2000/2006 的地球自轉角 ERA——兩者差約 0.1 角秒以上，
    且對應的是不同的中間框架。
    """
    T = (jd_ut1 - 2451545.0) / 36525.0
    s = (67310.54841
         + (876600.0 * 3600.0 + 8640184.812866) * T
         + 0.093104 * T ** 2
         - 6.2e-6 * T ** 3)
    return np.deg2rad((s % 86400.0) / 240.0) % (2 * np.pi)


def _r3(th: np.ndarray) -> np.ndarray:
    c, s = np.cos(th), np.sin(th)
    m = np.zeros(th.shape + (3, 3))
    m[..., 0, 0] = c;  m[..., 0, 1] = s
    m[..., 1, 0] = -s; m[..., 1, 1] = c
    m[..., 2, 2] = 1.0
    return m


def _polar(xp: np.ndarray, yp: np.ndarray) -> np.ndarray:
    """PEF → ITRF 的極移矩陣。小角近似，殘差 < 1 mm（xp、yp 恆 < 1 角秒）。"""
    m = np.zeros(xp.shape + (3, 3))
    m[..., 0, 0] = 1.0; m[..., 0, 2] = xp
    m[..., 1, 1] = 1.0; m[..., 1, 2] = -yp
    m[..., 2, 0] = -xp; m[..., 2, 1] = yp; m[..., 2, 2] = 1.0
    return m


def _mjd(times) -> np.ndarray:
    t = pd.DatetimeIndex(times)
    return np.asarray(t.view("int64"), dtype=float) / 86400e9 + 40587.0


def itrf_to_teme(r_itrf, v_itrf, times, eop: pd.DataFrame):
    """ITRF 位置(km)／速度(km/s) → TEME。`times` 為 UTC。

    **注意輸入的速度必須是地固速度**（SP3 的 V 行即是）。
    若手上是慣性速度，不要用這個函式的速度輸出。
    """
    mjd = _mjd(times)
    e = _interp(eop, mjd)
    th = gmst82(mjd + 2400000.5 + e["UT1-UTC"] / 86400.0)
    W = _polar(e["X"] * ASEC2RAD, e["Y"] * ASEC2RAD)     # PEF → ITRF
    r_pef = np.einsum("nji,nj->ni", W, np.asarray(r_itrf, dtype=float))
    v_pef = np.einsum("nji,nj->ni", W, np.asarray(v_itrf, dtype=float))
    Rz = _r3(th)                                          # TEME → PEF
    w = np.array([0.0, 0.0, OMEGA])
    r_teme = np.einsum("nji,nj->ni", Rz, r_pef)
    # 地固速度轉慣性須加上 ω×r —— 漏掉會讓 vis-viva 反算的半長軸偏低數百公里
    v_teme = np.einsum("nji,nj->ni", Rz, v_pef + np.cross(w, r_pef))
    return r_teme, v_teme


def teme_to_itrf(r_teme, v_teme, times, eop: pd.DataFrame):
    """TEME 位置(km)／速度(km/s) → ITRF。輸出的速度為**地固**速度。"""
    mjd = _mjd(times)
    e = _interp(eop, mjd)
    th = gmst82(mjd + 2400000.5 + e["UT1-UTC"] / 86400.0)
    Rz = _r3(th)
    W = _polar(e["X"] * ASEC2RAD, e["Y"] * ASEC2RAD)
    w = np.array([0.0, 0.0, OMEGA])
    r_pef = np.einsum("nij,nj->ni", Rz, np.asarray(r_teme, dtype=float))
    v_pef = np.einsum("nij,nj->ni", Rz, np.asarray(v_teme, dtype=float)) - np.cross(w, r_pef)
    return (np.einsum("nij,nj->ni", W, r_pef),
            np.einsum("nij,nj->ni", W, v_pef))


def ric(r_ref, v_ref, r_test) -> np.ndarray:
    """`r_test − r_ref` 在 RIC（徑向／沿跡／法向）的分量，km。

    **三個輸入必須在同一慣性框架**，且 `v_ref` 必須是慣性速度。
    用地固速度會讓法向軸偏轉約 1.36°（實測，LEO 550 km），把沿跡誤差洩漏到法向。
    """
    r_ref = np.asarray(r_ref, dtype=float)
    v_ref = np.asarray(v_ref, dtype=float)
    d = np.asarray(r_test, dtype=float) - r_ref
    R = r_ref / np.linalg.norm(r_ref, axis=1, keepdims=True)
    C = np.cross(r_ref, v_ref)
    C /= np.linalg.norm(C, axis=1, keepdims=True)
    I = np.cross(C, R)
    return np.stack([(d * R).sum(1), (d * I).sum(1), (d * C).sum(1)], axis=1)
