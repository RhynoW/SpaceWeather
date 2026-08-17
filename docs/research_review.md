# 依公開學術研究檢視強化方向

> 檢視日期：2026-08-18。對照本專案現況（`README.md`、`docs/architecture.md`、
> `docs/forecast_verification.md`、`docs/density_model_validation.md`）。
>
> 每項建議都標示**證據等級**：
> 〔文獻〕有同儕審查或 arXiv 出處；〔實測〕本次檢視實際發出請求確認；
> 〔推論〕由前兩者推得，未直接驗證。

---

## 一、最重要的三個結論

**1. 48 小時預報未勝過基線，與文獻一致，不是本專案的實作缺陷。**
Frontiers 2024 的地磁暴預報綜述明確指出：超過約 3 小時的中期預報準確度急遽衰退，
多數模型「almost useless」，而 NOAA 的 24 小時機率預報也僅達約 50% 水準。
本專案 3/6/12h 勝過持續性、24h 微幅勝出、48h 未勝出的形狀，**正是文獻描述的形狀**。
→ 這改變的是**計畫管理**而非程式：構想書的「48 小時 POD ≥ 0.7」以目前的
國際水準衡量並不現實，應在期中檢討時以文獻為據重新協商 KPI，
而不是繼續調參試圖達標。〔文獻〕

**2. 電離層網域可以擺脫「需機關協調」的死結——已實測確認資料可直接下載。**
本專案 `GNSS_PNT` 網域的規則全部回報 `unavailable`，卡在沒有在地實測。
本次實測確認 CDAAC 的 COSMIC-2（福衛七號）**掩星產品可直接匿名下載**：

| 產品 | 內容 | 實測路徑 | 可對應的參數 |
|---|---|---|---|
| `ionPrf` | 電離層電子密度剖面 | `…/cosmic2/provisional/spaceWeather/level2/{年}/{日}/` | **`FOF2`**、hmF2 |
| `podTc2` | 絕對總電子含量 | `…/cosmic2/provisional/spaceWeather/level1b/{年}/{日}/` | **`TEC`** |

→ 這讓 `FOF2` 與 `TEC` 兩個參數**不必等外部協調就能有實測值**，
是本次檢視中效益最高的單一動作。〔實測〕

**3. 但 S4 沒有這麼順利，別把話說滿。**
文獻確實記載 CDAAC 有 `scnLv1` 產品（含 S4 振幅閃爍指數），
COSMIC-2 更在**星上**直接算出 GPS/GLONASS 於 L1/L2 的 S4 與 σφ。
但本次逐層瀏覽 `cosmic1/repro2021` 與 `cosmic2/provisional` 的公開目錄，
**均未出現 `scnLv1`**（只有 `podTec`／`podTc2`／`ionPrf`／`ionPhs`）。
→ 建議先向 CDAAC Data Users Forum 確認 `scnLv1` 的取得管道，
**在確認之前，不可在交付文件中宣稱 S4 已有資料來源**。〔實測＋文獻〕

---

## 二、預報層

### 2.1 加入 TSS，這是目前缺的標準指標　〔文獻〕｜成本：小

文獻共識是 **True Skill Statistic（TSS = POD − POFD）對類別不平衡不敏感**，
因而特別適合稀有事件評估；SWAN-SF 方法學論文專章比較 TSS 與 HSS 的優劣。
本專案已有 POD／FAR／CSI／HSS／BSS，**獨缺 TSS**。

本專案事件基率約 3%，正落在「HSS 會因大量正確否定而失真」的區間。
建議在 `services/forecast/verify.py` 的 `ContingencyTable` 補上 TSS 與
balanced accuracy，兩者都只是既有四格數字的算術，不需重跑模型。

### 2.2 有效樣本數被高估，信賴區間因此偏窄　〔文獻〕｜成本：中

SWAN-SF 論文的核心警告是**時間相關樣本**：同一活動區的連續時刻高度相關，
天真的切分會讓技巧虛高。對應到本專案，是**同一場地磁暴內的連續 3 小時樣本**。

本專案已做對兩件事（滾動起報、訓練恆早於測試並留 7 天 gap），
但 MAE 的 bootstrap 95% CI 目前應是對**逐筆樣本**重抽。
若 13,440 筆中有大量來自同一場暴，**有效樣本數遠小於名目值**，
CI 會系統性偏窄。

→ 建議改用 **block bootstrap**（以事件或以連續區塊為重抽單位），
並在驗證報告中同時列出名目樣本數與估計的有效樣本數。
這不會改善模型，但會讓「3h 改善 +7.6%」這類宣稱的不確定度誠實。

### 2.3 重採樣的負面結果已與文獻一致，值得寫進報告　〔文獻〕｜成本：極小

本專案實測 `class_weight="balanced"` 讓 BSS 從 +0.117 掉到 −0.81，因而移除。
SWAN-SF 論文正是系統性檢驗過採樣／欠採樣／加權對績效的實際影響。
→ 這是**本專案的實測與文獻互相印證**的一個點，
建議在 `forecast_verification.md` 明確引用，
把「我們試過而且失敗」升級為「我們的失敗方式與文獻一致」。

### 2.4 延長提前量最有文獻支持的方向：前驅訊號　〔文獻〕｜成本：中

Frontiers 綜述的結論是：中期預報的突破不在換模型，
而在**改用暴發生前的前驅量**，而非只用當下的太陽風條件。點名的包括：

| 前驅量 | 本專案現況 | 建議 |
|---|---|---|
| X 射線通量 | ✅ 已有 `XRAY_LONG` | 已具備，納入特徵 |
| 高能粒子 | ✅ 已有 `PROT10` | 已具備 |
| **宇宙線強度（Forbush decrease）** | ❌ **完全沒有** | **建議新增**：CME 抵達前宇宙線強度先下降，是少數具真實提前量的訊號。已有以宇宙線約束的 LSTM 暴預測研究。資料源 NMDB（中子監測站資料庫）公開免費 |
| ICME vs SIR/CIR 驅動源分類 | ❌ 沒有 | 兩種驅動的暴時演化不同，混訓會互相稀釋 |
| 平滑黑子數 SSn | ⚠️ 有 `ISN` 但未平滑 | 低成本 |

**優先做宇宙線**：它是清單中唯一能實質延長提前量、且資料公開免費的項目。

### 2.5 用耦合函數取代原始 Bz　〔文獻〕｜成本：小

Newell (2007) 的耦合函數
`dΦ/dt ∝ v^(4/3) · B⊥^(2/3) · sin^(8/3)(θc/2)`
與磁層狀態變數的相關性優於單獨的 Bz 或 v，是目前廣泛採用的驅動量。

本專案已有 `SW_V`、`IMF_BZ`、`IMF_BT`，**只差 By 就能算出 clock angle θc**，
而 OMNI2 本來就有 By 欄位（本專案的擷取器只是沒取）。
→ **加一個欄位、加一個衍生特徵**，是本節投報率最高的一項。

但須留意 Lockwood (2022) 對耦合函數的方法學提醒：
不同耦合函數的比較容易受資料處理與飽和效應影響，
不應只憑相關係數就宣稱某一式較優。

### 2.6 中等強度暴被低估——這會影響門檻校準　〔文獻〕｜成本：小

綜述引述的統計結果值得決策層注意：
**地磁感應電流（GIC）的峰值激發統計上對應 Kp 4–6，而非最大 Kp**；
中等強度暴因發生頻繁，累積造成的電網與衛星損害反而大於極端事件。

→ 本專案目前的事件定義是 Kp ≥ 5，門檻掃描示範用 Kp ≥ 6。
建議在與需求單位的門檻校準工作坊中**明確納入這一點**，
避免把資源全押在 L4／G4 以上的極端情境。

---

## 三、密度層

### 3.1 不確定度應改用文獻方法，目前是手寫經驗值　〔文獻〕｜成本：中

`services/exporter/drag_correction.py` 的 `_uncertainty()` 目前是
`0.15 + 0.10·log10(ap) + 0.20·(ratio−1)` 這樣的經驗函數，
程式註解已誠實標明「非由實測校準」。文獻已有成熟得多的做法：

- **ML-HASDM with UQ**（Licata et al., 2022）：以 MC dropout 與直接機率預測
  建模，在 CHAMP 軌道上 20 個預測區間的實測累積機率與期望值
  **偏離從未超過 1%**（GRACE-A 為 1.15%）。
- **Calibrated and Enhanced NRLMSIS 2.0 with UQ**：直接針對本專案所用的
  MSIS 系列做校準與不確定度量化。

→ 建議至少引用後者的校準曲線，取代自訂經驗式；
資源允許時再往 MC dropout 的方向做。

### 3.2 取得一個外部誤差基準　〔文獻〕｜成本：極小

本專案宣稱「平靜期 MSIS 典型偏差約 15%」但**無出處**。
文獻給出可引用的數字：**HASDM 在 200–800 km 的誤差經確認為 6–8%**。
→ 這提供了一個外部參照點：可據以說明本專案 15% 的保守程度，
或直接改引文獻值並註明來源。無論選哪個，都比目前的無出處數字好。

### 3.3 用 CHAMP/GRACE 公開密度校準 storm_ratio　〔文獻〕｜成本：中

本專案原規劃接上游 `Sat_TraingDataExtension` 的密度反演（A9）來校準。
但 CHAMP 與 GRACE 的加速度計反演密度**本身就是文獻的標準真值來源**，
且已公開發布多年。

→ 直接取用公開資料庫比自建反演更快，也更容易被外部稽核者接受
（因為用的是社群公認的基準）。這能把 `calibrated_by_observation`
從 `false` 翻成 `true`，是密度產品成熟度的關鍵一步。

---

## 四、電離層層

### 4.1 先接 ionPrf 與 podTc2　〔實測〕｜成本：中

如第一節所述，兩個產品已確認可匿名下載。建議依序：

1. **`ionPrf` → `FOF2`**：取臺灣周邊（例如 20–27°N、118–124°E）的掩星剖面，
   反演 F2 層臨界頻率。這是構想書「福衛相關產品」的實質內容，
   也讓 HF 網域從只靠 D-RAP 擴充到 F 層。
2. **`podTc2` → `TEC`**：絕對 TEC，讓 `GNSS_PNT` 網域至少有一個可判定的參數。

兩者都屬**事後產品**（provisional，非即時），
入庫時 `publication_lag_s` 必須反映真實延遲，否則回放會產生前視偏差。

### 4.2 在地判讀有現成的氣候態可用　〔文獻〕｜成本：小

基於 COSMIC-1 掩星資料的東亞區域研究
（10–40°N／100–140°E，**涵蓋臺灣**，2007–2018）給出可直接使用的統計特徵：

- **日變化**：17:00 起增強，**峰值集中在 22:00–01:00**
- **季節**：春秋最高，夏季次之，**冬季最低**
- 與地磁活動強相關

→ 兩個立即可用之處：
(a) 寫進 `docs/glossary.md` 的判讀指引，讓值勤人員知道該盯哪個時段；
(b) 在缺實測時作為**氣候態基準**——這正是本專案驗證擂台要求新模型必須
贏過的那種基線。

### 4.3 閃爍預報的現實期待　〔文獻〕｜成本：—

目前文獻的閃爍預報水準是**未來 1 小時的 S4 數值預報**（LSTM 類方法）。
→ 不要在構想書中承諾更長的閃爍提前量。這與第 2 節的地磁暴結論同理：
先確認國際水準，再訂 KPI。

---

## 五、驗證方法

### 5.1 對齊 COSPAR ISWAT 的驗證框架　〔文獻〕｜成本：中

COSPAR 的國際太空天氣行動小組（ISWAT）已提出社群層級的驗證規範，
其太陽風驗證團隊的做法值得本專案借鏡：

- **七元件的中繼資料架構**，支撐連續、透明、可重現的驗證
- **三層指標**：point-to-point 比對、binary（二分類）、event-based（事件層級）
- **開放線上平臺**，用統一指標追蹤模型隨時間的進展

本專案的驗證擂台在精神上已相當接近（基線門檻、滾動起報、列聯表），
差在**中繼資料的規範化**——這正好與外部審查意見要求的
「commit／資料快照／config hash」相通。

→ 建議讓驗證輸出帶上 `run_id`／`data_snapshot`／`config_hash`／`model_version`，
並在文件中聲明對齊 ISWAT 的三層指標。這對「國際可比性」這種交付論述特別有用。

### 5.2 作業級驗證有可借鏡的先例　〔文獻〕｜成本：小

英國氣象局太空天氣作業中心（MOSWOC）已發表其地磁暴與閃焰預報的
**即時驗證系統**，使用排序機率技巧分數、ROC 曲線與可靠度圖。
→ **可靠度圖（reliability diagram）是本專案目前缺的一項**：
它直接檢查「報 30% 的那些場合，是否真的約 30% 發生」，
正對應本專案 24h 起 BSS 轉負所反映的機率校準問題。
加一張可靠度圖，比再調一次模型更能說明問題出在哪。

---

## 六、不建議做的事

同樣重要，避免把資源投到文獻已顯示報酬有限的方向：

1. **不要再靠換模型架構搶 48h 的技巧分數。** 綜述已指出瓶頸在
   物理可預報性與前驅資訊，不在模型容量。換 LSTM／Transformer
   而不加前驅特徵，預期不會改變結論。
2. **不要用重採樣去衝 POD。** 本專案實測與文獻皆顯示它會破壞機率校準。
   目前 `--objective pod` 主動印出訓練／測試落差的做法是對的，應保留。
3. **不要在 S4 確認取得管道前，把它寫進交付的資料來源清單。**（見 1-3）
4. **不要用地理緯度做電離層的區域判斷。** 臺灣地磁緯度約 19°N，
   位於赤道異常駝峰，這已寫進 `glossary.md`，實作時須貫徹。

---

## 七、建議順序

以「效益 ÷ 成本」排序，前三項可在不增加外部相依的前提下完成：

| 順位 | 動作 | 成本 | 為何排這裡 |
|---|---|---|---|
| 1 | 補 **TSS** 與 balanced accuracy | 小 | 純算術，補上文獻標準指標 |
| 2 | OMNI 加取 **By**，衍生 **Newell 耦合函數** | 小 | 一個欄位換一個文獻公認的驅動量 |
| 3 | 加**可靠度圖** | 小 | 直接診斷 BSS 轉負的成因 |
| 4 | 接 **`ionPrf`／`podTc2`** | 中 | 讓 `FOF2`／`TEC` 脫離 `unavailable`〔已實測可下載〕 |
| 5 | 改用 **block bootstrap** | 中 | 讓既有的 CI 誠實 |
| 6 | 接 **NMDB 宇宙線** | 中 | 唯一能實質延長提前量的公開資料 |
| 7 | 以 **CHAMP/GRACE** 校準密度 | 中 | 把 `calibrated_by_observation` 翻成 true |
| 8 | 向 CDAAC 確認 **`scnLv1`** 管道 | 小（行政） | 確認後才談 S4 |
| 9 | 驗證輸出對齊 **ISWAT** 中繼資料 | 中 | 國際可比性論述 |

---

## 參考文獻

**地磁暴預報**
- [Importance and challenges of geomagnetic storm forecasting](https://www.frontiersin.org/journals/astronomy-and-space-sciences/articles/10.3389/fspas.2024.1493917/full)（Frontiers in Astronomy and Space Sciences, 2024）
- [How to Train Your Flare Prediction Model: Revisiting Robust Sampling of Rare Events](https://arxiv.org/abs/2103.07542)（SWAN-SF 方法學）
- [Cosmic-Ray-Constrained LSTM Model for Geomagnetic Storm Prediction](https://arxiv.org/pdf/2512.22003)
- [Physics-informed feature engineering for LSTM-based short-term forecasting of the geomagnetic Kp index](https://www.sciencedirect.com/science/article/abs/pii/S1364682626001215)
- [Forecasting Geoffective Events from Solar Wind Data](https://arxiv.org/pdf/2403.09847)

**耦合函數**
- [A nearly universal solar wind-magnetosphere coupling function](https://agupubs.onlinelibrary.wiley.com/doi/10.1029/2006JA012015)（Newell et al., 2007）
- [Solar Wind–Magnetosphere Coupling Functions: Pitfalls, Limitations, and Applications](https://www.personal.reading.ac.uk/~ym901336/pdfs/403_Lockwood_SpaceWeather_2022.pdf)（Lockwood, 2022）

**熱氣層密度**
- [Machine-Learned HASDM Thermospheric Mass Density Model With Uncertainty Quantification](https://agupubs.onlinelibrary.wiley.com/doi/10.1029/2021SW002915)（Licata et al., 2022）
- [Calibrated and Enhanced NRLMSIS 2.0 Model with Uncertainty Quantification](https://arxiv.org/pdf/2208.11619)
- [Uncertainty quantification techniques for data-driven space weather modeling: thermospheric density application](https://www.nature.com/articles/s41598-022-11049-3)
- [Calibration of GRACE on-board accelerometers for thermosphere density derivation](https://www.tandfonline.com/doi/full/10.1080/10095020.2021.2010506)

**電離層**
- [Research on Ionospheric Scintillation Effects and Prediction Model in East Asia Based on COSMIC-1 Occultation Dataset](https://doi.org/10.3390/universe12030086)
- [Short-time forecast of ionospheric irregularities using LSTM over equatorial and low-latitude regions](https://www.sciencedirect.com/science/article/abs/pii/S1364682625000501)
- [Ionospheric S4 Scintillations from GNSS Radio Occultation at Slant Path](https://www.mdpi.com/2072-4292/12/15/2373)
- [CDAAC GNSS Radio Occultation Datasets](https://www.cosmic.ucar.edu/what-we-do/data-processing-center/data)

**驗證方法**
- [Unifying the validation of ambient solar wind models](https://arxiv.org/pdf/2201.13447)（COSPAR ISWAT）
- [COSPAR ISWAT Assessment](https://www.iswat-cospar.org/assessment)
- [Verification of Space Weather Forecasts issued by the Met Office Space Weather Operations Centre](https://arxiv.org/pdf/1804.02985)
