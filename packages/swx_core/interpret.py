"""swx_core.interpret — 參數判讀指引（教育推廣與值勤輔助）。

這裡放的是「這個數字代表什麼、多少算大、容易怎麼誤讀」，
與 `configs/rules/*.yaml` 的**告警門檻是兩回事**：

  · 告警門檻（rules）  決定系統要不要發 L1–L4，須與需求單位校準，會改。
  · 判讀指引（本模組）  教學與科普用的一般性參考，取自公開文獻與 NOAA 尺度。

刻意分開，是為了避免推廣素材上的數字被誤當成作業門檻反過來影響規則設定。
兩者若混為一談，一次科普簡報就可能污染作業標準。

資料以 `docs/glossary.md` 為對照，兩處須一起維護（有測試檢查參數代碼有效）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 判讀等級（僅供教學說明，非系統告警等級 L0–L4）
BAND_QUIET = "平時"
BAND_NOTABLE = "值得注意"
BAND_ALERT = "警戒"
BAND_UNKNOWN = "無判讀基準"


@dataclass(frozen=True)
class Guidance:
    """單一參數的判讀指引。

    notable/alert 為門檻值；`higher_is_worse=False` 時比較方向反轉
    （如 Dst 越負越嚴重、極光邊界緯度越低越嚴重）。
    """

    code: str
    name: str
    reads: str                      # 這個數字在量什麼
    quiet: str                      # 平時的樣子（文字描述，含單位脈絡）
    notable: float | None = None
    alert: float | None = None
    higher_is_worse: bool = True
    note: str = ""                  # 常見誤讀或判讀要點
    aliases: tuple[str, ...] = field(default_factory=tuple)

    def band(self, value: float | None) -> str:
        if value is None or self.notable is None or self.alert is None:
            return BAND_UNKNOWN
        v = float(value)
        if self.higher_is_worse:
            if v >= self.alert:
                return BAND_ALERT
            return BAND_NOTABLE if v >= self.notable else BAND_QUIET
        if v <= self.alert:
            return BAND_ALERT
        return BAND_NOTABLE if v <= self.notable else BAND_QUIET


GUIDANCE: dict[str, Guidance] = {
    g.code: g
    for g in [
        # ── 太陽活動 ────────────────────────────────────────────────
        Guidance(
            "F107_OBS", "太陽 10.7 cm 電波流量",
            "太陽整體活動的代理量，驅動高層大氣的膨脹程度",
            "70–100 sfu（太陽極小期約 65）",
            notable=150, alert=250,
            note="Obs 是實測值，Adj 是校正到日地距離 1 AU 的值；"
                 "餵給大氣模型時餵錯會有約 ±3.5% 的系統性偏差。",
        ),
        Guidance(
            "XRAY_LONG", "GOES X 射線通量（0.1–0.8 nm）",
            "太陽閃焰的即時強度",
            "<1×10⁻⁶ W/m²（B 級以下）",
            notable=1e-5, alert=1e-4,
            note="1×10⁻⁵ = M1（R1），1×10⁻⁴ = X1（R3）。"
                 "以光速抵達，看到時影響已同時發生，沒有預警空間。",
        ),
        Guidance(
            "PROT10", "≥10 MeV 質子通量",
            "太陽高能質子事件強度，影響極區通信與衛星硬體",
            "<1 pfu",
            notable=10, alert=1000,
            note="10 pfu = S1，每級 ×10。質子數十分鐘至數小時抵達，預警空間很短。",
        ),
        Guidance(
            "X_FLARE_PROB", "X 級閃焰機率（24 小時）",
            "活動區產生 X 級閃焰的機率",
            "<10%",
            notable=0.3, alert=0.5,
            note="這是本系統少數具備提前量的閃焰資訊，但僅供提示，不升級為事件等級。",
        ),
        # ── 地磁 ────────────────────────────────────────────────────
        Guidance(
            "KP_3H", "Kp 指數",
            "全球地磁擾動程度，每 3 小時一個值",
            "0–3",
            notable=5, alert=7,
            note="準對數尺度，不可取算術平均——要平均請用 ap。"
                 "Kp5=G1、6=G2、7=G3、8=G4、9=G5。",
        ),
        Guidance(
            "AP_3H", "ap 指數",
            "Kp 的線性版本，可做算術運算",
            "<15 nT",
            notable=48, alert=154,
            note="48≈Kp5、154≈Kp7。大氣模型吃的是 ap／Ap 而非 Kp。",
        ),
        Guidance(
            "HP30", "Hp30 指數",
            "30 分鐘解析度的地磁擾動指數",
            "0–3",
            notable=5, alert=7,
            note="可超過 9（達約 12），因較短時窗會抓到被 3 小時平均削平的尖峰，非資料錯誤。",
        ),
        Guidance(
            "DST", "Dst 指數",
            "赤道環電流強度，反映注入磁層的能量",
            ">−20 nT",
            notable=-50, alert=-100, higher_is_worse=False,
            note="越負越嚴重。−50 中等暴、−100 強暴、−250 以下為超級暴"
                 "（2024-05 Gannon 事件達 −406 nT）。",
        ),
        Guidance(
            "IMF_BZ", "行星際磁場 Bz",
            "太陽風磁場的南北分量——整條因果鏈上最關鍵的單一參數",
            "±5 nT 震盪",
            notable=-10, alert=-20, higher_is_worse=False,
            note="**南向（負）才會出事**。持續南向 Bz 才有效率地把能量灌進磁層；"
                 "速度再高但 Bz 向北，通常不會有大地磁暴。",
        ),
        Guidance(
            "SW_V", "太陽風速度",
            "太陽風抵達地球時的速度",
            "300–500 km/s",
            notable=600, alert=800,
            note="須與 Bz 一起看。高速但北向 Bz 的組合威脅有限。",
        ),
        Guidance(
            "AURORA_BOUNDARY_LAT", "極光橢圓赤道側邊界",
            "極光環擴張到的最低緯度，代表擾動深入程度",
            ">60°",
            notable=55, alert=45, higher_is_worse=False,
            note="邊界壓得越低，代表越低緯度的 HF 與 GNSS 開始受影響。",
        ),
        # ── 電離層與通信 ────────────────────────────────────────────
        Guidance(
            "DRAP_TW_MHZ", "D 層吸收影響頻率（臺灣）",
            "因 D 層吸收而不可用的最高頻率",
            "0 MHz",
            notable=5, alert=15,
            note="此頻率以下的 HF 都被吸收掉了，數字越高代表中斷越嚴重。僅發生於日側。",
        ),
        Guidance(
            "TEC", "總電子含量",
            "訊號路徑上的電子總數，直接造成 GNSS 測距誤差",
            "中緯日間 10–50 TECU",
            notable=80, alert=120,
            note="GPS L1 上 1 TECU ≈ 0.16 公尺延遲。"
                 "TEC 高是**可修正的**偏差，讓接收機失鎖的是閃爍而非 TEC。",
        ),
        Guidance(
            "S4", "振幅閃爍指數",
            "訊號強度快速起伏的程度",
            "<0.2",
            notable=0.3, alert=0.5,
            note="與 TEC 是兩件事：閃爍造成失鎖且無法修正，只能等它過去。"
                 "赤道異常區（含臺灣）好發於日落後數小時。",
        ),
        Guidance(
            "ROTI", "TEC 變化率指數",
            "電離層不規則體造成的 TEC 起伏速率",
            "<0.5 TECU/min",
            notable=1.0, alert=3.0,
            note="與 S4 同屬閃爍指標，可在缺 S4 時作為替代判據。",
        ),
        Guidance(
            "FOF2", "F2 層臨界頻率",
            "垂直入射時 F2 層還能反射回來的最高頻率",
            "日間 8–14 MHz",
            note="斜向可用的最高頻率（MUF）約為 foF2 的 3 倍。"
                 "判讀重點是與該地該時的氣候值相比降低多少，而非絕對值。",
        ),
        # ── 軌道與大氣 ──────────────────────────────────────────────
        Guidance(
            "RHO_RATIO", "密度暴時倍率",
            "相對地磁寧靜基準的大氣密度倍率",
            "≈1.0×",
            notable=1.5, alert=2.5,
            note="**模型內部的相對比較量**，不是由觀測反演的校正係數。"
                 "同時刻、同地點、同高度、同 F10.7 下，僅把地磁輸入換成寧靜值後的比值。",
        ),
    ]
}


def guidance_for(code: str) -> Guidance | None:
    return GUIDANCE.get(code)


def assess(code: str, value: float | None) -> str:
    """回傳判讀等級。無指引或無值時回 BAND_UNKNOWN，不猜。"""
    g = GUIDANCE.get(code)
    return g.band(value) if g else BAND_UNKNOWN
