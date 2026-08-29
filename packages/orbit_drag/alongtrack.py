"""orbit_drag.alongtrack — 驅動量的選擇會造成多少沿跡誤差。

**這個模組回答的問題**：預報擂台量出「45 天期的 Ap 預報等於氣候值、
F10.7 的最佳模型是持續性」之後，接下來該問的是——**那又怎樣？**
差幾個 sfu、幾個 nT，在軌道上是差幾公里？如果差得比密度模型自己的偏差還小，
那麼爭論長期驅動量預報就是搞錯了對象。

作法刻意**不用軌道傳播器**。近圓軌道的沿跡誤差有封閉形式，物理透明得多，
也不必為此引入 SGP4 與 TLE 序列：

    半長軸衰減   da/dt = -ρ · B · sqrt(μ·a)          （能量法，近圓軌道）
    平均運動     n(a)  = sqrt(μ / a³)
    沿跡相位     θ(t)  = ∫ n(a(τ)) dτ
    兩情境之差   Δs(t) = a · (θ_A(t) − θ_B(t))

Δa 隨時間近似線性、Δθ 隨之近似二次，所以**沿跡誤差以 t² 成長**。
這正是為什麼「小小的密度偏差，兩週後就是幾十公里」。

單位一律走 SI（公尺、秒、kg/m³），只在輸出時換成公里——
km 與 m 混用是這類推導最常見的錯誤來源，而它不會報錯，只會給出
差 10³ 倍的答案。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .atmospheric import MU, RE, density

#: SI 版的重力常數與地球半徑（本模組內部一律用公尺）
MU_SI = MU * 1e9        # km³/s² → m³/s²
RE_SI = RE * 1e3

#: 彈道係數 B = Cd·A/m（m²/kg）的代表值。**這是輸入不是常數**：
#: 同一場太空天氣事件，B 差三倍，沿跡誤差就差三倍。
BC_REFERENCE = {
    "compact": 0.005,      # 密實本體、面質比低（大型平臺）
    "typical": 0.012,      # 一般遙測衛星
    "high_area": 0.022,    # 立方衛星／大型太陽帆板
}
DEFAULT_BC = BC_REFERENCE["typical"]


@dataclass
class Scenario:
    """一組驅動量假設。

    `rho_scale` 用來表達**密度模型自身的偏差**（MSIS 平靜期典型 ±15%），
    與驅動量的選擇是兩件不同的事——把它們畫在同一張圖上，才看得出
    誰才是沿跡誤差的主導項。
    """

    name: str
    label: str
    sw: pd.DataFrame
    #: 純量，或與 epochs 等長的逐時刻倍率。逐時刻是為了讓不確定度帶
    #: 隨地磁活動變寬——暴時的模式誤差比平靜期大，用單一常數會在
    #: 平靜段高估、暴時段低估。
    rho_scale: float | np.ndarray = 1.0
    note: str = ""


def constant_drivers(dates, *, f107: float, ap: float,
                     f107a: float | None = None) -> pd.DataFrame:
    """定值驅動表（持續性／氣候值情境用）。"""
    idx = pd.DatetimeIndex(pd.to_datetime(dates, utc=True)).floor("D").unique()
    return pd.DataFrame(
        {"f107": float(f107), "f107a": float(f107a if f107a is not None else f107),
         "ap": float(ap)},
        index=idx,
    )


def propagate(
    epochs,
    alt0_km: float,
    sw: pd.DataFrame,
    *,
    bc: float = DEFAULT_BC,
    rho_scale: float | np.ndarray = 1.0,
    lat: float = 0.0,
    lon: float = 0.0,
) -> pd.DataFrame:
    """由驅動量積出半長軸與沿跡相位。

    回傳逐 epoch 的 rho / alt_km / sma_km / n_rad_s / theta_rad。

    `lat`／`lon` 是密度取樣的參考點，不是全球代表值——同一組驅動量在不同
    座標上的暴時比值可差約 15%（見 atmospheric.density 的說明）。
    比較兩個情境時**兩邊必須用同一組座標**，否則差值裡混進了座標效應。
    """
    epochs = pd.DatetimeIndex(pd.to_datetime(epochs, utc=True))
    if len(epochs) < 2:
        raise ValueError("至少需要兩個 epoch 才能積分")

    a = (RE_SI + alt0_km * 1e3)                      # 公尺
    # **不要用 `epochs.view("int64") / 1e9` 取秒數。**
    # pandas 2.x 的 DatetimeIndex 可能是 ns 也可能是 us 解析度：
    # date_range() 給 ns，但由 pivot_table 或 floor 得來的索引常是 us。
    # 除以 1e9 在 us 索引上會得到 1/1000 的步長——不會報錯，
    # 只會讓 45 天的軌道衰減算成 1 小時的量（實測：沿跡差從數百 km 變成 10 公分）。
    dt = (epochs[1:] - epochs[:-1]).total_seconds().to_numpy()
    if not np.all(dt > 0):
        raise ValueError("epochs 必須嚴格遞增")

    scale = np.asarray(rho_scale, dtype=float)
    if scale.ndim == 0:
        scale = np.full(len(epochs), float(scale))
    elif len(scale) != len(epochs):
        raise ValueError(f"rho_scale 長度 {len(scale)} 與 epochs {len(epochs)} 不符")

    rho_all = np.empty(len(epochs))
    sma = np.empty(len(epochs))
    theta = np.zeros(len(epochs))

    for i, epoch in enumerate(epochs):
        alt_km = (a - RE_SI) / 1e3
        rho = float(density([epoch], np.array([alt_km]), sw, lat=lat, lon=lon)[0])
        rho *= scale[i]
        rho_all[i] = rho
        sma[i] = a

        n = np.sqrt(MU_SI / a ** 3)
        if i + 1 < len(epochs):
            step = dt[i]
            # 前向積分即可：一步之內 a 的變化是 10⁻⁷ 量級，
            # 高階積分器改善的是捨入誤差，不是物理。
            theta[i + 1] = theta[i] + n * step
            a = a - rho * bc * np.sqrt(MU_SI * a) * step

    return pd.DataFrame(
        {"rho": rho_all, "sma_km": sma / 1e3, "alt_km": (sma - RE_SI) / 1e3,
         "n_rad_s": np.sqrt(MU_SI / sma ** 3), "theta_rad": theta},
        index=epochs,
    )


def alongtrack_km(ref: pd.DataFrame, test: pd.DataFrame) -> pd.Series:
    """兩情境的沿跡位置差（km，正值代表 test 落後於 ref）。

    以參考情境的半長軸換算弧長。兩者的 a 相差不到千分之一，
    用哪一邊當半徑對結果的影響遠小於驅動量本身的不確定度。
    """
    dtheta = ref["theta_rad"] - test["theta_rad"]
    return (dtheta * ref["sma_km"]).rename("alongtrack_km")


def compare(scenarios: list[Scenario], epochs, alt0_km: float, *,
            bc: float = DEFAULT_BC, lat: float = 0.0, lon: float = 0.0,
            reference: str | None = None) -> pd.DataFrame:
    """把所有情境放在同一條時間軸上，回傳相對參考情境的沿跡差。

    第一個情境預設為參考。回傳長表：epoch / scenario / alongtrack_km /
    alt_km / rho，便於直接餵給繪圖。
    """
    if not scenarios:
        raise ValueError("至少需要一個情境")
    runs = {s.name: propagate(epochs, alt0_km, s.sw, bc=bc,
                              rho_scale=s.rho_scale, lat=lat, lon=lon)
            for s in scenarios}
    ref_name = reference or scenarios[0].name
    ref = runs[ref_name]

    rows = []
    for s in scenarios:
        r = runs[s.name]
        ds = alongtrack_km(ref, r)
        for epoch in r.index:
            rows.append({
                "epoch": epoch,
                "scenario": s.name,
                "label": s.label,
                "alongtrack_km": float(ds.loc[epoch]),
                "alt_km": float(r.loc[epoch, "alt_km"]),
                "rho": float(r.loc[epoch, "rho"]),
            })
    out = pd.DataFrame(rows)
    out.attrs["reference"] = ref_name
    out.attrs["bc"] = bc
    out.attrs["alt0_km"] = alt0_km
    return out
