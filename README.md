# SWX-SDA　太空天氣整合資訊與 SDA 應用系統

> 對應計畫：**太空天氣整合資訊與 SDA 應用模型建構研究**（117–118 年）

把分散的國內外太空天氣觀測，轉成任務單位可判讀（L0–L4）、可通報（事件卡）、
可介接（API／圖層）、可計算（STK/HPOP 大氣阻力參數）的四類產品。

**定位**：不重建國家級預報中心，而是建立「任務化轉譯層」。

**線上版**：<https://spaceweather.streamlit.app/>
　　教學頁可直達：[繁中](https://spaceweather.streamlit.app/?page=stem&lang=zh)　
[日本語](https://spaceweather.streamlit.app/?page=stem&lang=ja)　
[English](https://spaceweather.streamlit.app/?page=stem&lang=en)　
[Bahasa Melayu](https://spaceweather.streamlit.app/?page=stem&lang=ms)

> 線上版使用示範快照，**不是即時作業資料**（app 內有紅色橫幅標示）。

| 文件 | 內容 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | 系統架構、設計原則、與構想書七大議題的對應 |
| [docs/data_sources_c2c3.md](docs/data_sources_c2c3.md) | 地磁與電離層資料源盤查（C2/C3 風險分級之修正） |
| [docs/forecast_verification.md](docs/forecast_verification.md) | 預報引擎驗證報告（切分明細、列聯表、未達 KPI 之落差） |
| [docs/rtk_ionosphere.md](docs/rtk_ionosphere.md) | **RTK 能不能收斂**——I95 判據、兩種模式的差異，與「平靜日照樣超標」的實測 |
| [docs/forecast_skill.json](docs/forecast_skill.json) | **上表的機器可讀版**——API 與儀表板的技巧數字由此取得，不手抄 |
| [docs/density_model_validation.md](docs/density_model_validation.md) | 大氣密度模型配置、修正因子定義與驗證邊界 |
| [docs/operations_manual.md](docs/operations_manual.md) | **值勤手冊**（燈號判讀、24 條規則處置對照、維運指令、使用邊界） |
| [docs/glossary.md](docs/glossary.md) | **名詞說明與參數判讀**（教育推廣、值勤判讀、常見誤讀） |
| [docs/research_review.md](docs/research_review.md) | **依公開學術研究的強化檢視**（文獻對照、建議順序） |
| [docs/cwa_swoo_analysis.md](docs/cwa_swoo_analysis.md) | 中央氣象署 SWOO 架構分析與介接記錄（授權依據、待確認事項） |
| [docs/formosat7_tacc_analysis.md](docs/formosat7_tacc_analysis.md) | 福衛七號 TACC 資料分析（TDPC／TROPS）——**S4 閃爍資料源的確認** |

> **關於本文長度**：本文刻意維持單一文件，不拆成「概覽／作業／技術」三份。
> 理由是這套系統最容易被誤用的地方，正是**宣稱與限制之間的距離**——
> 把「48 小時預報」寫在概覽、把「未達 KPI」藏進技術文件，
> 讀者會只讀到前半。限制與數字必須跟功能敘述在同一份文件裡才不會被略過。
> 深入細節（值勤處置、驗證明細、資料源盤查）仍在 `docs/`，本文只保留判讀所需的部分。

### 分級術語

系統中有兩套獨立的分級，**不可互相換算**：

| 標記 | 定義 | 由誰決定 |
|---|---|---|
| **L0–L4** | 本系統的**任務風險等級**（正常／注意／警戒／嚴重／重大） | 本案自訂，門檻在 `configs/rules/*.yaml`，須與需求單位校準 |
| G1–G5 | NOAA 地磁暴強度分級 | 依 **Kp** |
| R1–R5 | NOAA 無線電衰減分級 | 依 **GOES 0.1–0.8 nm X 射線峰值通量** |
| S1–S5 | NOAA 太陽輻射風暴分級 | 依 **≥10 MeV 質子通量** |

G/R/S 階梯依 **[NOAA Space Weather Scales](https://www.swpc.noaa.gov/noaa-scales-explanation)**
定義，本系統不自訂、不修改。三套尺度**各自獨立**，同一列不代表等價：

| G　地磁暴<br>（Kp，無單位） | R　無線電衰減<br>（GOES 0.1–0.8 nm 峰值，**W/m²**） | S　輻射風暴<br>（≥10 MeV 積分質子，**pfu**） |
|---|---|---|
| **G1** ＝ Kp 5 | **R1** ＝ 1×10⁻⁵（M1） | **S1** ＝ 10 |
| **G2** ＝ Kp 6 | **R2** ＝ 5×10⁻⁵（M5） | **S2** ＝ 10² |
| **G3** ＝ Kp 7 | **R3** ＝ 1×10⁻⁴（X1） | **S3** ＝ 10³ |
| **G4** ＝ Kp 8 | **R4** ＝ 1×10⁻³（X10） | **S4** ＝ 10⁴ |
| **G5** ＝ Kp 9 | **R5** ＝ 2×10⁻³（X20） | **S5** ＝ 10⁵ |

pfu = particle flux unit（粒子·cm⁻²·s⁻¹·sr⁻¹）。
實作階梯在 `swx_core/flare.py`，並由 `tests/test_contracts.py` 對照上表驗證。

實作階梯與網域對應見 [docs/glossary.md](docs/glossary.md)，程式在 `swx_core/flare.py`。

這兩套分級在事件卡 JSON 中是**兩個獨立欄位**：

| 欄位 | 內容 |
|---|---|
| `mission_level` | 本系統自訂的任務風險等級，值域 `L0`–`L4` |
| `international_scale` | NOAA 尺度，例如 `G4`、`R3`、`S1`；未達門檻時為 `null` |
| `impacts[].inference` | **永不為 null**，四選一：`observed`（直接觀測）／`modelled`（模型或預報輸出）／`proxy`（間接推估）／`unavailable`（判定所需資料不存在，**不代表風險為零**） |
| `status` | 事件卡狀態機：`draft`（待人工確認）→ `issued`（已發布）→ `superseded`（已被新修訂取代）。狀態以 DB 欄位為準並覆蓋 payload 快照；此行為由
`tests/test_event_lifecycle.py` 驗證（發布後 `latest()` 必須回報 `issued`）。
單一行程內的讀寫已驗證，**多行程並行寫入的競態尚未測試** |

**目前實際行為與尚未實作的部分**（避免把設計誤讀為現況）：
`status`、`revision`、`supersedes`、`issued_utc` **已由 API 回傳**；
發布動作經 `EventStore.issue(actor=...)` 並寫入 `audit_log`。
但 **`reviewed_by`／`reviewed_at` 尚未成為事件卡欄位**，
簽核者目前只存在於稽核軌跡，未隨事件卡 JSON 一起交付；
亦尚未接上正式的人工簽核流程（無 UI 簽核動作、無權限控管）。

文中出現的「L3／G3」是**兩個欄位並列**，不代表等價——
`mission_level` 是任務影響判斷，`international_scale` 是 NOAA 定義的環境事件
強度尺度，不等同於任務影響。同一場事件的兩者可以不同步。

架構分層另以名稱表示（擷取層、資料層、模型層、預報層、風險層、產品層、展示層），
不使用 L 編號，以免與任務風險等級混淆。

---

## 快速開始

```bash
pip install -r requirements.txt

# 1. 資料源盤點與首次擷取
python -m services.ingest.run --list                    # 先看有哪些來源
python -m services.ingest.run --source all --backfill   # 首次：回填歷史（約 1 分鐘）
python -m services.ingest.run --source omni2_hourly --years 6
# Hp30 例行只解析近 120 天；1 小時預報要訓練資料，需回填歷史（不重抓，改解析既有原始檔）
python -m services.ingest.run --source gfz_hp30 --reparse --window-days 2100 --backfill
# e-GNSS I95 連線與版面檢查（已納入自動更新；此工具用於外部端點改版時定位問題）
python tools/i95_smoke.py   # 預報引擎的訓練資料

# 2. 端到端演練：資料 → 分級 → 事件卡 → STK 檔 → 密度修正因子
python tools/e2e_demo.py                        # 預設 2024-05 Gannon G5 事件

# 3. 儀表板
streamlit run apps/dashboard/app.py             # http://localhost:8501

# 4. API 與測試
python -m services.api.app                      # http://127.0.0.1:5100
python -m pytest tests -q
```

### 前提條件

上述快速開始流程（安裝、資料擷取、端到端演練、儀表板、API 與測試）
可在**不設定環境變數、不使用外部帳號、不具備 STK 授權**的情況下完整執行，
但**需要對外 HTTPS 連線**（CelesTrak、SWPC、GFZ、Kyoto、NASA SPDF）。

封閉網路環境加 `--offline` 改讀 `data/seed/` 的本地檔，此時只有種子資料涵蓋的
參數可用，其餘來源會回報失敗而非靜默略過。例行擷取不加 `--backfill`
（理由見下方「雙時間軸儲存」）。

### 執行環境

| 項目 | 值 |
|---|---|
| Python | 3.11+（開發與驗證於 3.13.9） |
| 作業系統 | Windows／Linux／WSL2（開發於 Windows 11） |
| 時間基準 | 全系統一律 UTC，無例外 |
| 外部授權 | 無。STK 僅在後續階段的實際介接驗證時需要 |
| 主要相依 | duckdb 1.5、pandas 2.2、pyarrow 22、pymsis 0.12（MSIS 2.1）、ppigrf 2.1、scikit-learn 1.8、Flask 3.1、Streamlit 1.54 |

上表為**開發與驗證所用的環境版本，不代表已測試過的相容範圍**——
其他版本可能可用，但未經驗證。`requirements.txt` 只給下限；
`requirements.lock` 提供本案相依樹的實測版本（57 個套件），
以 `python tools/make_lock.py` 重新產生（直接寫檔，不可用 shell 重導向——Windows 主控台為 cp950，重導向會把檔頭註解寫成亂碼）。

```bash
pip install -r requirements.lock   # 重現本文數字時使用
```

`requirements.lock` **不是跨平臺求解的結果**（不同於 uv／pip-tools 的 lock），
只是產生環境的實測版本快照；亦尚未在 CI 上固定版本。

---

## 目前狀態

| 層 | 模組 | 狀態 |
|---|---|---|
| 擷取層 | `services/ingest` | ✅ 24 個來源（21 個可運作）：CelesTrak、GFZ ×2、SWPC ×9、Kyoto、NASA OMNI2、**中央氣象署 SWOO**、**福衛七號 TACC ×2**（閃爍 `scn1c2`、精密定軌 `leoOrb`）、**國土測繪中心 e-GNSS I95**。其中 17 個納入背景自動更新 |
| 資料層 | `packages/swx_core` | ✅ 雙時間軸 Parquet + DuckDB、品質三級制、48 個註冊參數 |
| 模型層 | `packages/orbit_drag`、`packages/geomag` | ✅ 熱氣層密度／阻力（MSIS 2.1，暴時 ap 模式）＋地磁基準場（IGRF-14）＋TEME↔ITRF 框架轉換（含 EOP，對 astropy 8.0.1 驗證至 0.08 m）；電離層 D 層吸收已接 |
| 預報層 | `services/forecast` | ⚠️ **功能覆蓋** 四組目標：Kp（3 小時格點，3–48 h）、**Hp30（30 分鐘格點，1／3／6 h）**（構想書要求的 1 小時產品）、以及 **F10.7 與 Ap（日格點，1–45 天）**——後兩者是軌道預測實際依賴的驅動量，只有連續型指標；驗證擂台含命中率、誤警率、**提前量**與可信度四項 KPI。**任何 horizon 皆非正式作業產品**；Hp30 1 h 與 Kp 3–12 h 可作研究參考，**>12 h 為非作業性研究預報**（與 API 的 `not_for_operational_use_beyond_h: 12` 一致） |
| 風險層 | `services/risk_engine` | ✅ 4 網域 24 條規則、事件卡、作業狀態庫。新增 **GNSS_RTK**：門檻引用國土測繪中心 I95 公告值（8／20／30），是本案唯一有作業單位背書的判據 |
| 產品層 | `services/exporter` | ✅ STK/GMAT CSSI 驅動檔、密度修正因子表 |
| 展示層 | `services/api`、`apps/dashboard` | ✅ REST API＋Streamlit 儀表板（含值勤模式、影像頁、使用指南與四語 STEM 教學頁），端點與頁面集合由契約測試守住，且每頁以 AppTest 實跑驗證可渲染 |

### 實作狀態 ≠ 驗證狀態

「已實作」與「已在目標工具上驗證」是兩件事，分開列：

| 模組 | 成熟度 | 已完成的驗證 | 主要限制 | 證據 |
|---|---|---|---|---|
| CSSI 驅動檔匯出 | 格式已驗證 | 對 CelesTrak 實檔逐行比對，排除當日更新列後 2,054/2,054 一致 | STK 實際載入未驗證 | `tests/test_contracts.py` |
| 密度修正因子 | 原型 | 單元測試、前視洩漏檢查、基準污染檢查；**已與福衛七號精密定軌反演的密度交叉比對**（2024-04-29–05-20，83 個 6 小時分箱） | 比對顯示**模式的暴時響應被壓縮**（觀測增強 <1.5 時比值 0.85、≥3.5 時 1.70）；僅一個事件窗、單一高度，且兩條基線間可能有常數偏移，故**只有單調趨勢可引用**，個別絕對值不可 | `tools/density_obs_vs_model.py` |
| 實測密度判據 `DRAG_ENHANCEMENT` | 原型 | 由 leoOrb 精密定軌反演，彈道係數在比值中消掉；合成資料驗證可抵抗短週期混疊與單顆機動 | 門檻僅以一個 21 天窗（65 個寧靜樣本）標定，寧靜期最大值 1.98 幾乎貼著 L1 門檻 2.0；本參數為**尾隨量**（標在 t 的值描述 t−6h 到 t），不適合當暴起始指標 | `tests/test_tacc_leoorb.py` |
| D-RAP 介接 | 已介接 | 解析與臺灣取樣 | 未與在地 HF 通聯實測校準 | `services/ingest/forecast_sources.py` |
| L0–L4 分級 | 原型 | 駐留、遲滯、可用性行為測試 | 門檻未與需求單位校準（`calibrated: false`） | `tests/test_risk_engine.py` |
| Kp 預報（3–48 h） | 研究階段 | 4 折滾動起報回測、基線比較、事件段提前量 | 未達 KPI；3–12 h 中位提前量為 0；未與 NOAA 官方預報同場比較 | `docs/forecast_verification.md` |
| Hp30 預報（1／3／6 h） | 研究階段 | 同一擂台、5 折、含提前量 | 1 h 的 BSS 0.475、FAR 0.337 為全部組合最佳，但持續性基線 POD 更高（0.729 對 0.601）；6 h 中位提前量為負，屬事後偵測 | `docs/forecast_verification.md` |
| **F10.7 預報（1–45 天）** | **不發布產品** | 4 折滾動起報、日格點、2021–2026 | 每個提前量都由 **Tier 0 持續性勝出**（1 天 MAE 7.7 → 45 天 31.5 sfu），依門檻不應上線；27 天處出現凹陷（太陽自轉） | `docs/forecast_verification.md` |
| **Ap 預報（1–45 天）** | **不發布產品** | 同上 | 3 天以後誤差**不隨提前量成長**（7.15 → 7.25 nT）且由**氣候平均**勝出——該尺度的 Ap 預報實質上就是氣候值 | `docs/forecast_verification.md` |
| **沿跡不確定度** | 分析工具 | 近圓軌道能量法封閉解，非傳播器 | 500 km／45 天：換驅動量預報差 **252 km**、密度模型 ±1σ 差 **359 km**——密度模型較大，只報一項會低估一半以上；400 km 時放大到 2 600 km 以上 | `docs/forecast_verification.md`、`tools/alongtrack_drivers.py` |
| **密度不確定度校準** | 實測 | 福衛七號精密定軌反演 ÷ MSIS，799 筆／10 個事件窗／2023-02→2026-06 | 1σ 由手訂常數改為實測：平靜 **0.223**（原猜 0.15，過於樂觀）、ap≥50 **0.282**（原猜 0.35，過於保守）。**校準的是散布不是偏差**，中位數不可引用為模式偏差 | `docs/density_calibration.json`、`tools/calibrate_density_uncertainty.py` |
| 事件卡生命週期 | 原型 | `draft → issued → superseded` 轉移、禁止重複發布、發布者記入稽核軌跡 | 尚未接正式人工簽核流程；API 未回傳 `reviewed_by`／`reviewed_at` | `tests/test_event_lifecycle.py` |
| 判定依據 `inference` | 已完成 | 四值列舉、永不為 null、網域取最弱項、無資料回 `unavailable` | 觀測／模型之分類依參數清單判定，非逐筆溯源 | `tests/test_event_lifecycle.py` |
| IGRF 基準場 | 已完成 | **值域合理性檢查**（F/D/I 落在臺灣公認範圍、磁傾角隨緯度單調遞增） | **未與任一測站實測序列逐點比對**；區域擾動仍為推估 | `tests/test_geomag.py` |

「成熟度」用語：`已完成` = 功能與驗證皆到位；`格式已驗證` = 對規格正確，
但未在目標工具上實測；`已介接` = 資料通了，尚未做效果校準；
`原型` = 可運作但參數未校準；`研究階段` = 未達可作業水準。

**尚未建置**：區域地磁擾動的在地實測（需磁力計串流）、**ROTI**（TEC 變化率指標，
需夠高取樣率的斜距 TEC，格點 TEC 不可代用）、多頻段影響矩陣的實證校準、
STK 端實際介接驗證。

分級規則涵蓋 `ORBIT_PREDICTION`、`HF_COMM`、`GNSS_PNT`、`GNSS_RTK` 四個網域，共 24 條。

構想書要求的影響矩陣還包含 VHF/UHF 與 S/X/Ka（SATCOM）、衛星操作。
這三個網域已宣告於 `configs/params.yaml` 的 `impact_domains`，
**但尚無任何分級規則**——它們仍會出現在 `/v1/nowcast` 與儀表板上，
標為 `尚無判據`（`criteria_total = 0`），而不是從表上消失。
理由與參數層級相同：畫面上少一列與綠燈難以分辨，讀者會把「還沒訂門檻」
讀成「查過沒事」。此行為由 `tests/test_risk_engine.py` 驗證。

**三個網域現在都有實測判據**，但成熟度不同：

| 網域 | 實測判據 | 代理判據 | 現況 |
|---|---|---|---|
| `HF_COMM` | GOES X 射線、質子通量 | — | 判據齊備 |
| `GNSS_PNT` | 福衛七號掩星 S4、TEC | Kp、閃焰 | L3 需 S4＋ROTI，**只有 S4 時回報 `partial`** |
| `ORBIT_PREDICTION` | `DRAG_ENHANCEMENT`（leoOrb 反演） | Kp、Ap | 兩組並用：Kp 給前導訊號，實測給量值 |

系統刻意把「沒資料」「部分可判」「沒事」分成三態，不會顯示綠燈誤導判讀。
`partial` 的語意是「已有的判據若超標仍會發報，但沒有告警**不等於**已確認平靜」。

**實測判據不只是補齊，它會修正代理的偏差。** 2024-04-29 至 05-20 的實測顯示，
熱氣層密度響應對 Kp 高度非線性——Kp 6–7 的增強中位僅 **1.19**，Kp ≥ 7 才到 **2.34**；
該窗內 Kp 規則觸發 15 次、實測規則僅 4 次且全落在 Gannon 事件，
即 **Kp 代理在中等擾動時明顯過度告警**。

---

## 目錄結構

```
configs/               設定即契約
  params.yaml            參數字典（UI 標籤、API 說明、品質值域皆由此生成）
  sources.yaml           資料源盤點（議題一交付物的機器可讀版）
  rules/*.yaml           L0–L4 分級門檻
packages/
  swx_core/              資料契約、參數字典、品質管線、雙時間軸資料層、
                         CSSI 格式、閃焰分級、參數判讀指引（interpret.py）
  orbit_drag/            熱氣層密度與大氣阻力（自 Sat_TraingDataExtension 移入，
                         該目錄名為上游倉庫的實際拼法，非本文件誤植）
  geomag/                地磁參考場 IGRF-14 與區域擾動框架（議題二）
  SOURCE_MAP.md          移入模組的來源與改動記錄
services/
  ingest/                各來源介接器（設定驅動）
  forecast/              預報引擎與驗證擂台（Kp 3–48 h／Hp30 1–6 h／F10.7 與 Ap 1–45 天）
  risk_engine/           分級規則引擎與事件卡
  exporter/              STK CSSI 檔、密度修正因子
  api/                   Flask REST API
apps/dashboard/
  app.py                 Streamlit 儀表板
  stem.py                STEM 教學頁（12–18 歲，四語內容與遊戲）
tools/
  e2e_demo.py            端到端鏈路演練
  whatif_threshold.py    門檻校準模擬
  cssi_compare.py        CSSI 匯出與來源實檔的逐行比對（可稽核）
  density_cross_check.py MSIS 2.1 vs NRLMSISE-00 同條件交叉比對
  make_lock.py           產生 requirements.lock
  make_source_list.py    由 configs/ 產生資料來源清單 docx（不手工維護）
tests/                   契約測試、規則引擎、地磁、密度測試
docs/                    架構書、資料源盤查、驗證報告
data/                    執行時產生（已 gitignore）
  swx_parquet/{參數}/{期間}/   觀測分區（cadence ≥1h 年分區，否則月分區）
  raw/{來源}/{年}/{月}/{日}/   原始落地（解析錯誤可重跑，不需重抓）
  seed/                        離線種子資料
  exports/                     STK 檔、修正因子、事件卡
  swx_ops.sqlite               事件卡與稽核紀錄
```

---

## 幾個設計決定

### 雙時間軸儲存

每筆資料同時記 `valid_time`（物理時間）與 `ingest_time`（入庫時間）。
沒有這個，歷史事件回放會用到事後訂正值，預報命中率會虛高而無法交代。

```python
store.query("DST", as_of="2024-05-10T18:00Z")   # 只看「當時已知」的資料
```

回填歷史資料時要注意：若一律標成「今天入庫」，回放到 2024 年會查不到任何東西
（語意上正確——我們當年確實沒有這筆資料，但議題七的回放就無從進行）。
`--backfill` 會以各來源的 `publication_lag_s` 重建「當時可取得性」。
這是**近似**，須在驗證報告中載明。首次建庫用 `--backfill`，之後例行擷取不加。

效果實例——回放到 Gannon 事件起始時刻，系統只知道 Ap=105，因此發出 **L3/G3**，
而不是事後才確定的 L4/G4：

```
$ python tools/e2e_demo.py --as-of 2024-05-11T00:00Z
SWX-20240510T0000-GS　GEOMAGNETIC_STORM　等級 L3（國際 G3）
```

### 擷取與服務讀寫分離

DuckDB 是單寫入者模型；擷取端只寫 Parquet 分區，服務端唯讀查詢，兩者不互卡。

### 規則即設定

L0–L4 門檻寫在 YAML，需求單位調整門檻不需改程式，並可當場回答
「這組門檻過去五年會發幾次警報」：

```
$ python tools/whatif_threshold.py --rule ORB-L3-KP6 --param KP_3H --sweep 5,6,7,8
 threshold  n_alerts  per_year  duty_cycle_pct  max_h
       5.0       170      30.2            7.39   96.0
       6.0        59      10.5            2.79   78.0
       7.0        25       4.4            1.42   78.0
       8.0        11       2.0            0.92   78.0
```

構想書的 TRL 表把「門檻須與需求單位共同校準，避免過度告警或漏報」列為風險，
這張表就是化解該風險的具體手段。儀表板「門檻校準」頁提供同樣功能的互動版。

### CSSI 格式單一實作

讀進來與寫出去給 STK 的是同一組欄位定義（`swx_core/cssi.py`）。
對 CelesTrak 實檔比對：**2,278/2,279 行一致**，唯一差異是**當日仍在更新的觀測列**
（來源在快照後又修訂了該日 Kp）；排除當日後為 **2,054/2,054 完全一致**，
三個區段的配置亦相符。欄位位置另對 GMAT `SolarFluxReader.cpp` 的
`substr(92)` 交叉驗證。

### 預報技巧照實呈現

驗證擂台以滾動起報回測，ML 模型必須贏過持續性、氣候平均、27 日復現三個基線才准上線。

**評估設定**：目標 Kp（3 小時解析度）｜訓練資料 2021-01 起（OMNI2 回填 6 年＝2021–2026）｜
滾動起報 4 折、訓練集永遠早於測試集並留 7 天 gap｜
**每折測試樣本 3,360 筆，四折合計 13,440 筆**（下表 MAE 為合計值上的統計）｜
改善率定義為 `1 − MAE_模型 ÷ MAE_基線`｜MAE 附 bootstrap 95% CI。
各折的訓練／測試期間明細見
[docs/forecast_verification.md](docs/forecast_verification.md)（README 只列合計樣本數）。

| horizon | 最佳模型 | MAE | 95% CI | 最佳基線 | 基線 MAE | 改善 |
|---|---|---|---|---|---|---|
| 3h | gbm | 0.624 | 0.616–0.633 | persistence | 0.675 | +7.6% |
| 6h | gbm | 0.809 | 0.798–0.820 | persistence | 0.874 | +7.4% |
| 12h | gbm | 0.968 | 0.955–0.981 | persistence | 1.048 | +7.6% |
| 24h | gbm | 1.051 | 1.037–1.066 | climatology | 1.070 | +1.8% |
| **48h** | **climatology** | **1.068** | 1.054–1.084 | — | — | **ML 未勝出** |

**構想書要求的 1 小時產品另建在 Hp30 上**（Kp 是 3 小時指數，
以它為目標時 1 小時 horizon 只是把同一個值換個說法）。同一座擂台、5 折滾動起報，
樣本 98,639 筆（30 分鐘格點，2020-11 起）：

| horizon | 最佳模型 | MAE | 基線 MAE | POD | FAR | BSS | 事件段命中率 | 中位提前量 |
|---|---|---|---|---|---|---|---|---|
| **1h** | **gbm** | **0.495** | persistence 0.530 | 0.601 | **0.337** | **0.475** | 0.492 | **1.0 h** |
| 3h | gbm | 0.733 | persistence 0.760 | 0.236 | 0.484 | 0.196 | 0.182 | 0.0 h |
| 6h | gbm | 0.882 | persistence 0.924 | 0.095 | 0.591 | 0.067 | 0.094 | **−0.75 h** |

**1 小時是目前唯一接近可用的產品**，但**不是全面勝出**——持續性基線的
POD 更高（0.729 對 0.601），gbm 贏在 MAE、FAR 與機率品質。要少漏報選持續性，
要少誤報選 gbm，這個取捨屬需求單位的決定。
6 小時的中位提前量為**負**，代表多半在事件開始後才第一次命中，是延遲偵測而非預報。

**提前量的定義**（構想書明列的 KPI，也是最容易各說各話的一項）：

以**事件段**計——起報時刻 t 的預報說的是 t+horizon，故

    提前量 = 事件起始 − 首次命中的起報時刻 = horizon −（首次命中的目標時刻 − 事件起始）

上限即 horizon；**可以是負的**（事後偵測），報表照實呈現不截斷；
目標時刻落在事件起始之前的告警不計入（它已算誤報）。
未命中的事件段仍計入分母。因此**提前量必須與事件段命中率一起讀**：
漏掉九成、剩一成準時命中，也能得到漂亮的提前量。

**事件型指標的定義**（不附定義的 POD/FAR 無法複核）：

- 事件：**Kp ≥ 5**（G1 以上），基率約 3%
- 判定：於目標時刻**逐點比對**，未設 ±時間容差窗（比容差版嚴格）
- POD = 命中數 ÷ 實際事件數；FAR = 誤報數 ÷ 所有發報數
- 機率門檻在**訓練折**上選（預設最大化 CSI），不得用測試折挑門檻

實測（4 折滾動起報，門檻取訓練折 CSI 最佳）：

事件基率僅約 3%，離開列聯表無法判斷 POD/FAR 是穩定結果還是小樣本波動，故一併列出
（以下為 GBM 於四折合計測試集上的實測值，操作點取訓練折 CSI 最佳）：

| horizon | 命中 | 誤報 | 漏報 | 正確否定 | POD | FAR | BSS | POD（in-sample） |
|---|---|---|---|---|---|---|---|---|
| 3h | 179 | 133 | 290 | 12,838 | 0.382 | 0.426 | 0.278 | 0.833 |
| 6h | 81 | 86 | 388 | 12,885 | 0.173 | 0.515 | 0.117 | 0.858 |
| 12h | 27 | 40 | 442 | 12,931 | 0.058 | 0.597 | 0.028 | 0.832 |
| 24h | 7 | 36 | 461 | 12,936 | 0.015 | 0.837 | −0.017 | 0.839 |
| 48h | 10 | 87 | 458 | 12,885 | 0.021 | 0.897 | −0.022 | 0.833 |

驗證表另輸出 **TSS** 與 balanced accuracy：

```
TSS  = POD − POFD          範圍 −1 至 1，0 表示不具區辨能力
POD  = 命中 ÷ (命中 + 漏報)
POFD = 誤報 ÷ (誤報 + 正確否定)      ← 與 FAR 分母不同，FAR 的分母是「所有發報數」
```

TSS 相較 accuracy **對事件基率較不敏感**，因此適合與 POD／FAR／BSS 並列使用；
**不應單獨用作模型優劣的判定**。本系統事件基率僅約 3%、正確否定達一萬餘筆，
accuracy 與 HSS 都會被這批「猜沒事就對」的樣本稀釋
（3h TSS 0.371、24h 僅 0.012，衰減比 HSS 呈現得更清楚）。

**均未達構想書的 POD ≥ 0.7、FAR ≤ 0.4。** 三點須一併說明：

1. **24 小時以上僅 7–10 次命中**，該列的 POD/FAR 已屬小樣本統計，不宜單獨引用。
2. **24 小時起 BSS 轉負**，代表該 horizon 的機率產品不優於「永遠報氣候頻率」。
3. **訓練折 POD 穩定在 0.83 左右，測試折卻從 0.38 掉到 0.02**——
   （訓練折 POD 為 **in-sample** 結果：以訓練資料餵回已訓練模型並套用同一操作點，
   **非**訓練集內部交叉驗證，故不可視為獨立驗證效能，僅用來揭露過擬合落差）——
   這個落差在所有 horizon 上都存在，是分類器過擬合的明確訊號，
   而非單純的 horizon 效應。改善方向應包含正則化與機率校準，
   不只是換模型或加特徵。

`--objective pod` 可把訓練折 POD 推到 0.728，但測試折只有 0.064；
程式會主動印出這個轉移落差，不讓訓練折的達標被誤讀為滿足 KPI。

**與文獻的關係，以及這不能解釋什麼。**
地磁暴預報綜述指出：超過約 3 小時的中期預報準確度急遽衰退，
NOAA 的 24 小時機率預報亦僅達約 50% 水準。
本專案「隨 horizon 衰退」的形狀與此描述一致。

**但文獻只說明長期預報本來就困難，不能用來免除本案的實作責任。**
本案同時觀察到明顯的訓練／測試落差（POD 0.83 → 0.38–0.02），
這是過擬合，屬於實作面問題，與物理可預報性是兩回事。
未達 KPI 的成因至少還包括：特徵不足（缺前驅量如宇宙線、耦合函數）、
事件樣本稀少、機率未校準、目標變數與操作點選擇、以及模型架構限制——
這些都尚未逐一排除。

因此正確的結論是：
**在本案目前的資料、特徵、模型與驗證設定下，48 小時預報未達可作業門檻**，
而不是「此事不可能做到」。

對 KPI 的建議亦應相稱：以目前的資料與模型配置，
「48 小時 POD ≥ 0.7」**不具足夠證據支持**，
該項 KPI 宜與需求單位就**事件定義、時間容差窗與比較基準**重新協商，
而非僅調整數值目標。文獻對照與建議的強化順序見
[docs/research_review.md](docs/research_review.md)。

### 技巧與預報值一起交付

預報值單獨旅行是這套系統最容易造成誤用的形式。因此驗證擂台的成績寫成
機器可讀的 [docs/forecast_skill.json](docs/forecast_skill.json)，
`GET /v1/forecast` 與儀表板都由它取數字，**不手抄**：

- 每一列預報都附該 horizon 的實測 POD／FAR／BSS／事件段命中率／中位提前量，
  以及它**贏過的最佳基線**——只給上線模型的分數，讀者會誤以為它全面較優。
- 成績檔缺席時回 `null` 而非 0。0 會被讀成「命中率 0」，`null` 才是「沒有量過」。
- 成績檔放在 `docs/` 而非 `data/exports/`：它是**宣稱的證據**，
  必須與引用它的報告同進版控；`data/` 不進版控，雲端部署就讀不到。

重跑：`python -m services.forecast.run --verify --target <kp|hp30|f107|ap> --write-summary`。

### 密度修正因子（摘要）

模型為 **MSIS 2.1**（`pymsis` 0.12），採**暴時 ap 模式**。修正因子定義為
同時刻、同地點、同高度、同 F10.7 下，地磁輸入由寧靜換成實際值的密度比：

```
storm_ratio = ρ(實際 ap 歷史) ÷ ρ(寧靜基準)
```

**寧靜基準的精確定義**（`Ap` 為日均指數、`ap` 為 3 小時歷史序列，兩者都要換）：

```
Ap_daily   = 4
ap_history = [4, 4, 4, 4, 4, 4, 4]     # MSIS 暴時模式的 7 個元素全部
```

只換日均值而留著真實 ap 歷史的話，暴時模式下基準與擾動態會變成同一件事、
比值恆為 1——這個錯誤發生過且不易察覺，故寫進交付 metadata
（`drag_correction.product_metadata()`）而非僅寫在註解。

**數值的適用範圍**：以下為 2024-05-08→14 期間、
於**固定參考點 lat 0°／lon 0°**、每 3 小時取樣、各高度帶取**該期間最大值**：
300–400 km **2.24×**、400–500 km **2.90×**、500–600 km **3.67×**。

**這不是全球最大值，也不是任一地點的定值。** 固定參考點是為了呈現
「地磁活動造成的相對時間變化」，**不代表任何特定任務區域的實際密度**。

座標影響已實測，並非可忽略：450 km、同一期間下，
臺灣位置（25°N／121°E）的峰值比參考點低 **8.45%**，
四個測試座標間的全距達 **14.9%**。
→ **對應特定任務區域時，必須以該區座標重算**：
`density_ratio(..., lat=25.0, lon=121.0)`。
重現：`python tools/density_cross_check.py --coords`。

**`storm_ratio` 是模型內部的相對比較量，不是由觀測反演得到的密度校正係數。**

在取得實測反演密度之前，以**換模型**取得模型分歧的量級：同條件下
MSIS 2.1 與 NRLMSISE-00 的峰值密度相差 **7.3–8.8%**（350／450／550 km）。
這是**模型間分歧，不是觀測誤差，也不能嚴格視為誤差的下界**——
兩個模型可能同向偏離實測、同時漏掉同一項物理，此時分歧小而誤差大。
它提供的是「在尚未經觀測校準前，模型選擇本身造成多少差異」的參考。

文獻記載 HASDM 於 200–800 km 的誤差為 6–8%，數量級相近，
但**兩者的誤差定義、資料來源與驗證條件都不同，不宜直接比較**。
重現：`python tools/density_cross_check.py`。
完整模型配置、暴時 ap 模式的實測影響（含前視洩漏修正）與驗證邊界見
[docs/density_model_validation.md](docs/density_model_validation.md)。

### 太陽閃焰的時效性

X 射線以光速抵達（約 8 分 20 秒），**沒有預警空間**。
HF 網域的閃焰相關等級可能由 L1 起跳，且屬「即時偵測 + 影響評估」而非預報——
**不宣稱具備事件發生前的預警能力**。
（`L0` 表示未觸發該網域規則，**不代表所有必要資料都可用**；
資料缺漏時規則回報 `unavailable` 而非 L0。）
能提前的只有活動區的 M/X 級閃焰機率，且僅產生 L1 提示，
測試會擋住任何把它升級為事件等級的改動。

### 地磁基準場與推估的區別

IGRF-14 基準場離線可算，不需外部資料（臺灣代表點 F≈45,007 nT、D≈−4.6°、I≈35.1°）。
**這些值目前只通過值域合理性檢查**——測試斷言 F 落在 43,000–47,000 nT、
磁傾角落在 30–42°、且磁傾角隨緯度單調遞增。**尚未取得任何測站的實測序列做逐點比對**，
因此不可稱為「已與實測驗證」。取得**鹿林／龍磻（Lunping, LNP）地磁觀測站**
（INTERMAGNET 網絡，約 25.0°N／121.17°E，中央氣象署運作）的實測序列後，
才能做正式驗證。
區域擾動 ΔH 則需在地磁力計實測；在此之前只有推估值，**一律標 `is_proxy=True`**，
有專門的測試守這個旗標。

附帶一提：臺灣地理緯度 23.5°N，但**地磁緯度僅約 19°N**，正落在赤道異常駝峰區——
電離層現象依地磁緯度分布，用地理緯度判斷會系統性失準。

---

## 教育與推廣

太空天氣的判讀門檻高，而**誤讀的代價與看不懂一樣大**。
系統因此把「怎麼看懂這些數字」當成一項產品，而非附屬說明：

| 素材 | 對象 | 內容 |
|---|---|---|
| 儀表板「**STEM 教學**」頁 | **12–18 歲學生** | 四語教材＋三個互動遊戲，用此刻的真實資料而非課本插圖 |
| 儀表板「使用指南」頁 | 一般使用者 | 依讀者的問題編排，含完整來源標註表 |
| 儀表板「名詞與判讀」頁 | 值勤人員 | 因果鏈圖解、各參數現值落在哪一區、門檻線 |
| [docs/glossary.md](docs/glossary.md) | 簡報講義、自習 | 名詞說明、參數判讀速查、**常見誤讀** |
| `packages/swx_core/interpret.py` | 其他介面重用 | 判讀指引的程式化版本（17 個參數） |

各素材共用同一組判讀基準，改一處即同步。

### STEM for Space Weather（12–18 歲）

**四語**：繁體中文／日本語／English／Bahasa Melayu，翻譯與內容同檔維護
（分檔會讓其中一種語言悄悄過期，有測試守住每一句都四語齊備）。

**三個互動遊戲**，每個對應一個常被誤解的觀念：

| 遊戲 | 教什麼 |
|---|---|
| 誰先到？ | 光 8 分 20 秒、質子數小時、CME 1–3 天——**為什麼閃焰無法預警** |
| 你來當預報員 | 給 Bz 與風速判斷會不會有暴——**南向 Bz 才會出事** |
| 等級對對碰 | Kp → G 級對照（NOAA 定義，有測試確保不教錯） |

**兩件刻意不簡化的事**：不把模式輸出說成觀測；不誇大危險
（太空天氣不會毀滅地球，講清楚實際影響的尺度）。教育推廣最容易犯的錯，
就是為了吸引注意而誇大，反而讓學生日後發現被騙。

**判讀門檻與告警門檻刻意分開**：
`interpret.py` 的「值得注意／警戒」是**科普教學用的一般性參考**，
取自公開文獻與 NOAA 尺度；系統實際發 L0–L4 的門檻在 `configs/rules/*.yaml`，
須與需求單位校準。兩者混為一談的話，一次科普簡報就可能反過來污染作業標準。

推廣時最該講的三件事（完整清單見 glossary）：

1. **三種擾動的抵達時間差三個數量級**——X 射線 8 分鐘、質子數十分鐘、CME 1–3 天。
   「閃焰預警」與「地磁暴預警」的難度天差地遠。
2. **臺灣不是低風險區**——地磁緯度僅約 19°N，位於赤道異常駝峰，
   是全球電離層最劇烈的地帶之一。用地理緯度判斷會系統性低估。
3. **沒有告警 ≠ 安全**——規則回報 `unavailable` 代表判定所需資料不存在。
   儀表板對此顯示灰色而非綠燈。

---

## 限制與非目標

明確劃出系統**不做**什麼，比列出做了什麼更能降低誤用風險：

- **不是國家級太空天氣預報中心**，不取代中央氣象署太空天氣作業辦公室。
- **不提供未經驗證的區域即時實測**：臺灣周邊地磁擾動目前只有推估值，
  一律標 `is_proxy=True`。
- **不把推估值呈現為觀測值**：事件卡的 `inference: proxy` 與
  `is_proxy` 旗標都會傳遞到 API 與儀表板。
- **不保證 STK/HPOP 與本系統的密度結果一致**：CSSI 驅動檔的格式已驗證，
  但 STK 端的實際載入與傳播結果尚未做交叉比對。
- **不宣稱能給出校準過的絕對密度**：福衛七號的投影面積、阻力係數與乾重皆未公開，
  故只做得到**相對於同一 F10.7 之寧靜期望值的比值**。`calibrated_by_observation`
  維持 `false`；要翻成 `true` 須向 TASA／NSPO 或 UCAR 取得衛星巨觀模型。
- **實測密度判據的門檻尚未定版**：僅以一個 21 天窗（65 個地磁寧靜樣本）標定，
  而寧靜期實測最大值 1.98 幾乎貼著 L1 門檻 2.0，樣本增加後極可能超過。
- **GNSS 閃爍的實測判定仍不完整**：S4 已由福衛七號掩星（`tacc_scn1c2`）提供，
  但 ROTI 仍無來源，故 `GNSS-L3-SCINT` 回報 `partial`——**該規則可發報，
  但「沒有告警」不等於確認平靜**。且掩星幾何與地面測站不同，門檻尚未校準。
- **任何 horizon 的預報皆非正式作業產品；超過 12 小時者不得用於作業決策**：
  訓練折 POD 約 0.83、測試折僅 0.38–0.02，過擬合落差在**所有 horizon** 上都存在，
  因此 1–12 h 亦僅供研究參考；24h 起 BSS 轉負、48h 未通過基線門檻。
  此告誡不只寫在文件——儀表板預報頁顯示紅色警示，`/v1/obs` 回應含本系統預報時
  帶 `advisory.code = "RESEARCH_GRADE_FORECAST"`，呼叫端讀 JSON 就看得到。
- **不宣稱地磁基準場已與實測驗證**：IGRF 目前只通過值域合理性檢查，
  尚未與任何測站的實測序列逐點比對。
- **不產生行動命令**：系統輸出的是風險等級與建議處置，
  L3 以上須經人工確認後發布，任務層級的決策由值勤人員做。

---

## 資料來源

**引用本系統的任何數字時，須一併標註原始產製者。**
每個資料源、影像與動畫都在 `configs/` 中帶 `attribution`
（`provider`／`product`／`url`／`terms`），由契約測試守住不得遺漏；
儀表板「使用指南」頁列出完整對照表。

交付用的來源清單（Word）由設定檔產生、不手工維護：

```bash
python tools/make_source_list.py     # → docs/SpaceWeather資料來源清單YYYYMMDD.docx
```

影像一律**直接連結產製者提供的公開網址**、不下載轉存，理由是不衍生重製與再散布。
但兩點須載明：使用時仍受各來源的 attribution／terms 與熱連結政策約束，
若來源不允許嵌入則應改為連結其官方頁面；且**熱連結不適合稽核與重現**——
來源更新或撤除後，歷史報告將無法重現該畫面，
需要留存證據時應另存快照並記錄 checksum 與取得時刻。

### 動畫

兩種作法，**依單幀大小決定，不是依偏好決定**：

| 作法 | 適用 | 理由 |
|---|---|---|
| **MP4**（`st.video`） | 高解析度序列 | SUVI 304Å 單幀 1.1 MB × 359 幀 = **397 MB**，瀏覽器逐幀載入不切實際；編碼後同內容 12 MB。採 NASA／ESA 自產的 7 支 MP4 |
| **逐幀播放器** | 單幀 < 100 KB | 由 SWPC 幀索引 JSON 即時組成，**永遠是最新的一段**、不需伺服器端編碼。WSA-Enlil 無官方 MP4，只能走這條 |

逐幀播放器**等距抽樣**至設定幀數（Enlil 169 → 60 幀），並**保留頭尾**——
取前 N 幀只會看到過去，取後 N 幀只會看到預測，兩者都讓動畫失去意義（有測試守住）。
播放前先預載全部幀並顯示進度：不預載直接播，第一輪會因逐幀下載而卡頓，看起來像壞掉。

現成 MP4 共 7 支（合計 88 MB，瀏覽器僅在按下播放時才下載）：

| 動畫 | 儀器 | 大小 | 動畫才看得出來的事 |
|---|---|---|---|
| 太陽黑子 | SDO/HMI Intensitygram | 12 MB | 黑子隨自轉橫越日面；活動區正在長大還是衰減 |
| 光球磁圖 | SDO/HMI Magnetogram | 12 MB | 極性區塊的剪切與扭轉（δ 型 → X 級閃焰機率高） |
| 日珥與色球 | SDO/AIA 304Å | 12 MB | 日珥噴發的過程，即 CME 的起源 |
| 閃焰 | SDO/AIA 94Å | 21 MB | 閃焰亮化的時序，可對照 GOES X 射線 |
| 三色合成 | SDO/AIA 211+193+171 | 5 MB | 冕洞位置——冕洞高速流是不伴隨 CME 的擾動來源 |
| 日冕儀 C2 | SOHO/LASCO C2 | 11 MB | CME 爆發瞬間；是否為正對地球的暈狀 |
| 外日冕 C3 | SOHO/LASCO C3 | 15 MB | CME 離開太陽後的傳播，可估抵達時間 |

同一組超過兩段動畫時改用選擇器，並在選項標明檔案大小——
按下去才知道要載 21 MB 是很差的體驗。

| 來源 | 提供 | 狀態 |
|---|---|---|
| CelesTrak CSSI SW-All | F10.7、Kp、ap、Ap、ISN、Cp、C9 | ✅ 2021→2041（含月預測） |
| GFZ Kp nowcast | Kp、ap、F10.7、SN | ✅ 備援兼近即時 |
| **e-GNSS I95**（國土測繪中心） | 基準站網電離層誤差指標，分本島 VRS／金門／澎湖三網 | ✅ 已納入自動更新（實測 3–4 秒）。**授權申請中**（經夏漢民太空中心）。官方僅以圖表發布，數值由圖表擷取，標 `suspect`／tier 2 |
| **SWPC 45 日 Ap／F10.7 預報** | 逐日驅動量預測，45 天 | ✅ **軌道預測實際依賴的量**，也是預報擂台上要打敗的作業基線。無回填管道（只發布當前一份），故實測成績自輪詢之日起累積 |
| GFZ Hp30／ap30 | 30 分鐘地磁指數 | ✅ 提升暴起始時刻解析度；**1 小時預報的目標變數**（例行只解析近 120 天，訓練用歷史以 `--reparse --window-days` 回填） |
| SWPC GOES X-ray | 0.05–0.4 / 0.1–0.8 nm 通量 | ✅ |
| SWPC GOES 閃焰事件 | 閃焰起訖時間與 A–X 分級 | ✅ |
| SWPC GOES 積分質子 | ≥10 MeV | ✅ |
| SWPC RTSW 磁場／太陽風 | IMF Bz/Bt、速度、密度、溫度 | ✅ |
| SWPC 太陽活動區 | 黑子面積、M/X 級閃焰機率 | ✅ |
| SWPC 3 日地磁預報 | 逐 3h Kp 預報 | ✅ 預報引擎的**對照基準** |
| SWPC 27 日展望 | 逐日 F10.7／Ap／最大 Kp | ✅ |
| SWPC OVATION | 極光橢圓赤道側邊界緯度 | ✅ |
| **SWPC D-RAP** | D 層吸收最高受影響頻率（全球＋臺灣） | ✅ 架構書原列 C2「需協調」，實為公開產品 |
| Kyoto WDC Dst | 逐時 Dst | ✅ |
| **NASA OMNI2** | 逐時**經時間位移校正與跨衛星合併**的 IMF 與太陽風資料（非單一儀器原始觀測）；Kp／Dst／ap／F10.7 為 OMNI 併入同一時間軸的地磁與太陽活動指數，原始產製者為 GFZ／WDC Kyoto／加拿大 DRAO。1963 起 | ✅ 預報引擎的訓練資料來源。各參數的原始來源與處理方式見 `configs/sources.yaml` |
| **中央氣象署 SWOO** | CWA 以在地觀測產製的區域太空天氣產品：TWTEC（地面 GNSS 網**反演**之總電子含量）、TWDI（磁力計 ΔH 各站**中位數**） | ✅ 經授權介接。**非公開 API，第三方須另行取得授權** |
| **福衛七號 TACC**（閃爍） | 掩星 S4 振幅閃爍指數（`scn1c2`），取臺灣周邊並 15 分鐘分箱 | ✅ **GNSS_PNT 網域首個實測判據**。單日打包 54 MB，不納入自動更新 |
| **福衛七號 TACC**（定軌） | 精密定軌（`leoOrb`，SP3-c，60 秒節奏）反演之軌道平均阻力衰減率與密度增強倍數 | ✅ **ORBIT_PREDICTION 網域首個實測判據**。彈道係數在比值中消掉，故不需非公開的衛星參數。單日 1.4–2.2 MB，需連續多日，不納入自動更新 |
| 地基 GNSS TEC/ROTI/S4（在地） | 電離層閃爍實測 | ⛔ 需外部協調 |
| 區域磁力計即時串流 | 區域地磁 | ⛔ 需外部協調 |
| 電離層探測儀 | foF2 | ⛔ 需外部協調 |

「可運作」的定義：`configs/sources.yaml` 標 `status: ready`，且在最近一次
`python -m services.ingest.run --source all` 中完成**連線 → 解析 → 品質標記 → 入庫**
全程無錯誤。21 個可運作來源中有 **17 個納入背景自動更新**；
`gfz_hp30`（單獨約 46 秒）與 `omni2_hourly`（六年歷史回填）排除於頁面載入路徑外，
改由手動「完整」按鈕或排程主機處理。未列於上表而標 `planned` 的 3 個來源為 `tw_gnss_tec`、
`tw_magnetometer`、`tw_ionosonde`，皆屬需機關協調項。
`nlsc_egnss_i95` 標 `ready` 並已納入自動更新，但**授權仍在申請中**（經夏漢民太空中心協助向國土測繪中心提出）——先輪詢是因為它**沒有回填管道**，晚一天就少一天；核准前其原始圖檔與衍生數值不進公開版控，也不放進 `data/demo`。

執行期另有 `degraded` 狀態：來源可取得但資料齡期超過 `latency_budget_s`，
於 `/v1/health/data` 與儀表板「資料健康」頁標示。**來源存在不等於資料可用。**

完整盤查見 [docs/data_sources_c2c3.md](docs/data_sources_c2c3.md)：C2/C3 應拆成
「現成可用／免費需註冊／需機關協調」三級，真正高風險的只有最後一級。

---

## 儀表板

線上版：<https://spaceweather.streamlit.app/>　本機：`streamlit run apps/dashboard/app.py`。
頁面清單以側欄實際顯示為準
（由 `tests/test_dashboard_pages.py` 守住與本表一致）：

| 頁面 | 網址代稱 | 用途 |
|---|---|---|
| **值勤模式** | `duty` | **一屏掌握全局**：各網域燈號＋最近事件卡＋資料齡期＋關鍵指標判讀 |
| 太空環境總覽 | `overview` | 各網域紅綠燈、關鍵指標趨勢、資料齡期 |
| 參數時序 | `series` | 任意參數繪圖，可填 as_of 進入回放模式 |
| 事件卡 | `events` | 事件卡全文與 SDA 介接 JSON、規則可用性 |
| 太陽閃焰 | `flares` | 閃焰事件表、X 射線時序、D 層吸收 |
| **RTK 現場查核** | `rtk` | I95 現況（分網）、兩類肇因查核表、模式 × 事件型態影響矩陣 |
| 短時預報 | `forecast` | 兩組地磁目標（Hp30 1／3／6 h、Kp 3–48 h）；預報值與該 horizon 的實測技巧（POD／FAR／提前量／可信度）並列。頁尾另有 **F10.7／Ap 的長提前量驗證**（1–45 天）與外部作業系統的量級對照 |
| 地磁基準場 | `geomag` | IGRF 參考場、測站表、ΔH 推估、Hp30 解析度對比 |
| 軌道與密度修正 | `density` | 密度修正倍率、STK 驅動檔下載。頁尾另有**驅動量的選擇值多少公里**：把預報擂台量到的 sfu／nT 換算成沿跡距離，並與密度模型自身的偏差比大小 |
| 資料健康 | `health` | 各通道齡期、品質旗標分布、資料源盤點 |
| 門檻校準 | `thresholds` | 互動式門檻掃描（供校準工作坊使用） |
| **太陽與行星際影像** | `imagery` | 13 張公開影像＋**9 段動畫**：黑子／日珥／磁圖／閃焰／三色合成／日冕儀 C2·C3、CME 傳播模擬、D 層吸收、全球 TEC、極光橢圓；另有**向日葵九號地球全球面盤**作為對照，以及獨立成組的**繪算影像**（月相），刻意與實拍分開呈現 |
| **名詞與判讀** | `glossary` | **教育推廣用**：因果鏈、各參數現值落在哪一區、常見誤讀 |
| **使用指南** | `guide` | 依讀者的問題編排的 app 內說明，含完整來源標註表 |
| **STEM 教學** | `stem` | **12–18 歲教學頁**，四語（繁中／日本語／English／Bahasa Melayu）＋三個互動小遊戲；含閃焰（94Å）、光球磁圖，以及「現在的地球與月亮長怎樣？」一節——**地球是拍的、月亮是算的**，並列的用意就是教會分辨 |

### 直達連結

每一頁都可以用網址直接開啟，不必開進去再點側欄：

```
https://spaceweather.streamlit.app/?page=stem           STEM 教學頁
https://spaceweather.streamlit.app/?page=duty           值勤模式
https://spaceweather.streamlit.app/?page=density        軌道與密度修正
```

**STEM 教學頁另吃 `lang` 參數**，四種語言可各自直達：

```
https://spaceweather.streamlit.app/?page=stem&lang=zh   繁體中文（預設）
https://spaceweather.streamlit.app/?page=stem&lang=ja   日本語
https://spaceweather.streamlit.app/?page=stem&lang=en   English
https://spaceweather.streamlit.app/?page=stem&lang=ms   Bahasa Melayu
```

**段落錨點**：教學頁的「從太空看地球的天氣」一段另有固定錨點，
網址可直接落在那一段，不必口頭指引「往下捲到第四節」：

```
https://spaceweather.streamlit.app/?page=stem#earth-weather
https://spaceweather.streamlit.app/?page=stem&lang=en#earth-weather
```

該段並列了向日葵九號的全球面盤影像與 NICT 即時網頁的入口按鈕
（<https://himawari8.nict.go.jp/>，可縮放、可回看過去 24 小時）。
影像頁的「地球大氣（對照組）」分組亦可見：`?page=imagery`。

代稱刻意用 ASCII 而非中文頁名——分享時不會變成一串百分號編碼，
教學場合要把網址寫在投影片或白板上。中文頁名直接帶在網址上也接受，
舊連結不會壞。代稱與頁面的對應由 `tests/test_dashboard_pages.py` 守住。

設計原則：**缺資料顯示灰色「無資料」而非綠色「正常」**——
綠燈會讓值勤人員誤以為已確認該網域無異常。

---

## 常用指令

```bash
# 擷取
python -m services.ingest.run --source celestrak_sw_all
python -m services.ingest.run --source all --offline        # 封閉網路
python -m services.ingest.run --source omni2_hourly --years 6

# 預報
python -m services.forecast.run --coverage                  # 特徵覆蓋率體檢
python -m services.forecast.run --verify                    # 全 horizon 驗證擂台
# 以 POD 目標挑操作點，並同時輸出訓練／測試落差（達標與否由輸出為準）
python -m services.forecast.run --verify --objective pod
python -m services.forecast.run --predict --write           # 產生預報並寫入
python -m services.forecast.run --verify --target hp30      # 1／3／6 h（30 分鐘格點）
python -m services.forecast.run --verify --target f107      # 1–45 天（日格點，阻力驅動量）
python -m services.forecast.run --verify --target ap
python -m tools.alongtrack_drivers --alt 500 --days 45   # 驅動量選擇 → 沿跡公里數
python -m tools.calibrate_density_uncertainty --write    # 密度不確定度的實測校準
python -m services.forecast.run --verify --write-summary    # 成績寫入 docs/forecast_skill.json
python -m services.forecast.run --predict --target hp30     # 構想書要求的 1 小時產品

# 匯出
python -m services.exporter.stk_spaceweather --out out/SpaceWeather-All-v1.2.txt
python -m services.exporter.stk_spaceweather --as-of 2024-05-10T12:00Z   # 回放
python -m services.exporter.drag_correction --start 2024-05-08 --end 2024-05-14

# 回放演練
python tools/e2e_demo.py --start 2022-02-01 --end 2022-02-10   # Starlink 再入
python tools/e2e_demo.py --as-of 2024-05-10T12:00Z             # 無前視偏差回放

# CSSI 一致性比對（把 README 的格式宣稱變成可重跑的檢查）
python tools/cssi_compare.py                 # 位元組層級，對 data/seed/SW-All.txt
python tools/cssi_compare.py --level field   # 診斷差異落在哪一欄

# 影像連線煙霧測試（單元測試不連網，外部端點搬家時只有這支會紅燈）
python tools/media_smoke.py                  # STEM 頁引用的媒體
python tools/media_smoke.py --all            # 設定檔內全部 22 項
```

**時間範圍語意**：`--start` 與 `--end` 皆為**含端點**（閉區間 `[start, end]`）。
未給時分秒的日期解為該日 `00:00Z`，所以 `--end 2022-02-10` 涵蓋到該日零時、
不含該日其餘時段；要涵蓋整日請寫 `--end 2022-02-10T23:59:59Z`。

---

## API

14 個端點，唯讀為主、無狀態、可快取。
端點集合由 `tests/test_api_contract.py` 守住——新增端點而未同步本節，測試會紅燈。

**回應語意**（以下為目前實際行為，非規劃）：

| 狀態碼 | 意義 |
|---|---|
| 200 | 請求成功。**但資料本身可能過期**——須檢查 `degraded` 與 `data_age_s` |
| 400 | 缺少必要參數（如 `/v1/obs` 未給 `param`） |
| 404 | 參數未註冊，或事件 ID 不存在 |
| 500 | 未預期的伺服端例外，回應 `{"error": {"code": "INTERNAL_ERROR", "message": "..."}}`。**不回傳內部例外訊息**（避免洩漏路徑與 SQL 片段），詳情寫入 server log；僅 debug 模式附 `detail` |

**沒有 422**：參數格式錯誤與值域錯誤一律回 400，不細分。
呼叫端不需（也不應）針對 422 撰寫分支；日後若引入，會同步更新契約測試。

**沒有 503**：資料源逾時或劣化時，本系統**仍回 200** 並在 `degraded` 與
`data_age_s` 標示，讓呼叫端自行決定可接受的新鮮度——把「服務不可用」與
「資料不夠新」混成同一個狀態碼會讓呼叫端無法區分。此為刻意設計，非疏漏。

`/v1/obs` 帶 `data_age_s`（**僅由觀測列計算**的齡期秒數）、
`latest_observed_utc`、`forecast_to_utc`（預報涵蓋到何時，無預報時為 `null`）
與 `degraded`（觀測是否逾越該參數的更新週期）。

齡期刻意排除預報列：預報的 `valid_time` 在未來，一併取 `max()` 會讓齡期變成負值，
`degraded` 的判斷式就永遠為假——**只要有任何預報列存在，過期的觀測通道
就再也不會被標記為劣化**。此為實際發生過的缺陷，已由
`tests/test_api_contract.py::test_data_age_excludes_forecast_rows` 守住。

`/v1/rules` 的 `status` 有三種，意義不同：

| 狀態 | 意義 | 「沒有告警」代表什麼 |
|---|---|---|
| `ok` | 所有宣告的判據都有資料 | **已確認未達門檻** |
| `partial` | 部分判據有資料，仍會評估並可能發報 | **不等於確認平靜**——缺少的判據可能單獨觸發 |
| `unavailable` | 一個判據都沒有 | **完全無法判定**，不代表風險為零 |

實例：`GNSS-L3-SCINT` 宣告 `requires_params: [S4, ROTI]`。福衛七號掩星接上後
S4 有了、ROTI 仍無來源，故為 `partial`——規則會在 S4 超標時發報，
但缺 ROTI 的情況下不可把「無告警」讀成安全。

這三者是「沒事」／「部分可判」／「沒資料」的區分機制。

`GET /v1/forecast` 的每一列都把**預報值與該 horizon 的實測技巧綁在一起**：

```json
{
  "target": "hp30",
  "issued_utc": "2026-08-17T23:00:00Z",
  "issued_basis": "latest_observation floored to 30min",
  "forecasts": [
    {
      "valid_time": "2026-08-18T00:00:00Z",
      "horizon_h": 1.0,
      "value": 1.58,
      "storm_probability": 0.006,
      "confidence": 0.6,
      "skill":          {"model": "gbm",         "POD": 0.601, "FAR": 0.337,
                         "BSS": 0.475, "ep_recall": 0.492, "lead_h_med": 1.0},
      "skill_baseline": {"model": "persistence", "POD": 0.729, "FAR": 0.444,
                         "BSS": 0.444, "ep_recall": 0.475, "lead_h_med": 1.0}
    }
  ]
}
```

**`skill_baseline` 不是裝飾**：此例中上線模型的誤警率較低、機率品質較好，
但**基線的命中率更高**。只回傳上線模型的分數，呼叫端會誤以為它全面較優。

`horizon_h` 由 `valid_time − issued_utc` 還原，`issued_utc` 取該目標參數的最新
觀測時刻並**對齊到目標格點**——觀測可能標在格間（SWPC 估計 Kp 標在 00:05），
不對齊會讓 horizon 還原成 2.92 小時，技巧查表就查不到。

預報引擎的起報錨點也定為**目標變數最後一筆觀測**（對齊格點），兩邊因此一致。
不這樣做會有兩個後果：面板最後一格常是「有太陽風、沒有目標值」的狀態，
從那裡起報等於宣稱有一筆不存在的觀測；而且 API 還原的 horizon 會對不上
——實測時出現過 1 小時預報被還原成 15.5 小時。

回應只含**最近一次起報**的那批預報（以 `ingest_time` 取最新批次）。
資料層會累積每一次 `--predict --write` 的結果，不過濾就會把上週錨點的
6 小時預報與今天的 1 小時預報混在同一張表上。

回應含本系統預報（`source_id: swx_forecast`）時，另帶 `advisory`：

```json
{
  "param": "KP_3H",
  "count": 16,
  "data_age_s": 3600,
  "degraded": false,
  "advisory": {
    "code": "RESEARCH_GRADE_FORECAST",
    "not_for_operational_use_beyond_h": 12,
    "message": "超過 12 小時的預報為研究階段產出，不建議用於作業決策：測試折 BSS 於 24h 起轉負，且訓練/測試折存在過擬合落差。",
    "reference": "docs/forecast_verification.md"
  },
  "records": [{"valid_time": "2026-08-18T12:00:00Z", "value": 3.7, "data_type": "FCS", "source_id": "swx_forecast"}]
}
```

純觀測不帶此欄位（否則告誡會被當成雜訊忽略）。兩種情形皆由
`tests/test_api_contract.py::test_research_grade_advisory_present_for_own_forecast`
與 `::test_no_advisory_for_pure_observations` 守住。

`/health` 帶 `data`（`data_origin`／`is_demo`／`operational`／`snapshot_time`），
讓呼叫端分辨服務端的是示範快照還是實際擷取的資料。

| 端點 | 說明 |
|---|---|
| `GET /health` | 服務與資料源狀態 |
| `GET /v1/params` | 參數字典 |
| `GET /v1/sources` | 資料源盤點 |
| `GET /v1/health/data` | 各通道資料齡期與品質 |
| `GET /v1/obs?param=&from=&to=&as_of=` | 觀測序列（`as_of` 觸發回放） |
| `GET /v1/nowcast` | 各網域當前等級 |
| `GET /v1/forecast?target=kp\|hp30&horizon=` | 預報序列，**每一列附該 horizon 的實測技巧與最佳基線** |
| `GET /v1/events?from=&to=` | 事件卡清單 |
| `GET /v1/events/{id}` | 單一事件卡（SDA 介接格式） |
| `GET /v1/events/{id}/history` | 事件卡修訂歷程 |
| `GET /v1/rules` | 規則狀態（含 `unavailable` 者） |
| `GET /v1/flares` | 太陽閃焰事件（含分級與 NOAA R 級） |
| `GET /v1/exports/stk/spaceweather.txt` | STK/GMAT CSSI 驅動檔 |
| `GET /v1/exports/drag-correction` | 密度修正因子 |

---

## 環境變數（皆可省略）

| 變數 | 預設 | 說明 |
|---|---|---|
| `SWX_DATA_DIR` | `./data` | 資料根目錄 |
| `SWX_CONFIG_DIR` | `./configs` | 設定目錄 |
| `SWX_ROOT` | 自動偵測 | 專案根目錄 |
| `SWX_ORBIT_DB` | 未設 | 既有 `space_db.duckdb` 路徑（掛載 TLE 全庫用） |
| `SWX_API_HOST` / `SWX_API_PORT` | `127.0.0.1` / `5100` | API 服務位址 |
| `SWX_API_DEBUG` | `false` | Flask debug 模式 |

---

## 重現本文數字

本文列出的主要計算結果都提供重現入口。稽核時建議照此順序執行：

| 宣稱 | 指令 | 預期 |
|---|---|---|
| CSSI 格式一致性 | `python tools/cssi_compare.py` | 已定稿列 2,054/2,054 位元組一致；合計 2,278/2,279 |
| IGRF 臺灣參考場 | `python -c "import sys;sys.path.insert(0,'packages');from geomag import summary;print(summary())"` | F≈45,007 nT、D≈−4.62°、I≈35.13° |
| 密度修正倍率 | `python -m services.exporter.drag_correction --start 2024-05-08 --end 2024-05-14` | 峰值 3.67×（500–600 km） |
| 預報驗證表 | `python -m services.forecast.run --verify --splits 4` | 與 `docs/forecast_verification.md` 相同 |
| Hp30 1 小時預報 | `python -m services.forecast.run --verify --target hp30` | gbm MAE 0.495、POD 0.601、FAR 0.337、BSS 0.475、中位提前量 1.0 h |
| 門檻掃描 | `python tools/whatif_threshold.py --rule ORB-L3-KP6 --param KP_3H --sweep 5,6,7,8` | Kp≥6 約每年 10.5 次 |
| 密度模型分歧 | `python tools/density_cross_check.py` | MSIS 2.1 vs NRLMSISE-00 相差 7.3–8.8% |
| 實測密度 vs 模式 | `python tools/density_obs_vs_model.py --start 2024-04-29 --end 2024-05-20` | 觀測 <1.5 時比值 0.85、1.5–2.5 時 1.40、≥3.5 時 1.70 |
| 端到端鏈路 | `python tools/e2e_demo.py` | Gannon 事件判為 L4／G4 |
| 無前視偏差回放 | `python tools/e2e_demo.py --as-of 2024-05-11T00:00Z` | 同一事件判為 L3／G3 |

> 「實測密度 vs 模式」需先取得 leoOrb 資料：
> `python -m services.ingest.run --source tacc_leoorb --date 2024.140`
> （該來源排除於背景自動更新之外，須手動或排程執行）

### 重現條件

**在資料快照、模型設定與執行環境相同時，可重現相同結果。**
使用較新資料或未鎖定的相依版本時，數字會有小幅差異：

| 項目 | 現況 |
|---|---|
| 資料快照 | `data/seed/`（CSSI 快照的產製時刻見檔內 `UPDATED` 標頭） |
| Python | 3.13.9 |
| 作業系統 | Windows 11（另於 Linux／WSL2 可執行） |
| 時區 | 全系統 UTC |
| 相依鎖定 | `requirements.lock`（57 套件，單一平臺快照，非跨平臺求解） |
| 版本識別 | **尚未在輸出中帶 `run_id`／`git_commit`／`config_hash`** |

後兩項是已知缺口，非交付級的重現規格；
建議的補強方式見 [docs/research_review.md](docs/research_review.md) §5.1
（對齊 COSPAR ISWAT 的驗證中繼資料架構）。

**數字會隨資料累積而變動**：門檻掃描的「每年次數」取決於回放期間長度，
預報指標取決於訓練資料量。與本文有小幅出入屬正常，
量級或結論方向不同才需追查。

**CSSI 比對的判準**（避免「換個判準就一致」）：以 CSSI 欄寬右補空白後做
**位元組相等**比較，不使用數值容差；以 `(區段, 日期)` 配對而非行序，
區段歸屬本身也是比對項。日期 ≥ 來源檔 `UPDATED` 當日者標為**未定稿**
（來源仍會修訂）並分開計算——判準取自來源檔自身而非執行日，
使同一份快照無論何時重跑都得到同一結論。

---

## 部署

### 資料更新

儀表板在開站時檢查資料齡期，**超過 60 分鐘即在背景重新擷取**，不阻塞頁面：

```bash
python -m services.ingest.refresh --status          # 只看齡期與納入的來源
python -m services.ingest.refresh --max-age-min 60  # 逾時才更新
python -m services.ingest.refresh --force --full    # 強制，含重量級來源
```

側欄顯示齡期與更新進度，並提供「更新」（17 個快速來源，約 33–55 秒）
與「完整」（另含 `gfz_hp30`）兩個按鈕。更新一律寫入**真實的 `data/` 目錄**，
因此雲端首次成功更新後，示範快照的 DEMO 橫幅會自動消失。

判斷齡期用的是 `ingest_time` 而非 `valid_time`——後者舊可能只是該通道本來就
更新得慢，重抓也不會變新。且**必須排除未來時刻**：CelesTrak 月預測列的
`valid_time` 遠到 2041 年，回填推算的 `ingest_time` 也在未來，
不濾掉會讓齡期變負值、自動更新永遠不觸發（有測試守住）。

### Streamlit Cloud

儀表板可直接部署到 [share.streamlit.io](https://share.streamlit.io)：

| 欄位 | 值 |
|---|---|
| Repository | `RhynoW/SpaceWeather` |
| Branch | `main` |
| Main file path | `apps/dashboard/app.py` |
| 部署網址 | <https://spaceweather.streamlit.app/> |

**不需要設定任何 secrets。** 環境變數只有一個選用項：
`SWX_DISABLE_SOURCES`（逗號分隔的 `source_id`）可在**單一站台**關掉個別來源，
不必改 `configs/sources.yaml`——後者會連排程主機與本機開發環境一起關掉。

雲端是全新 clone、`data/` 沒有觀測分區，此時 `swx_core.config.data_dir()`
會自動退回 `data/demo/` 的示範快照（含 449 個 Parquet 分區、36 個參數），
因此**一開啟就有畫面**，不必等待線上擷取。

設計上刻意讓「有真資料時絕不使用示範快照」——本機開發若誤讀到過期快照
卻沒察覺，比空白畫面更危險。

**Streamlit Cloud 只適合當展示層，不要拿它當資料服務。** 具體限制：

| 限制 | 後果 |
|---|---|
| 容器檔案系統**不持久** | app 內執行擷取寫入的 `data/` 會在重啟／重新部署後消失 |
| 容器會因閒置而休眠 | 沒有可靠的排程執行環境，無法保證定時擷取 |
| 與 app 生命週期綁定 | 擷取會佔用同一個行程，拖慢或阻塞使用者互動 |
| 資源與時間上限 | 長時間回填（如 OMNI2 六年）不適合在此執行 |

因此**不建議**在雲端 app 內執行：

```bash
python -m services.ingest.run --source all   # ← 不要在 Streamlit Cloud 內跑
```

正確作法是把擷取與儲存放到 app 之外：以獨立 worker／排程主機執行擷取，
寫入持久化儲存（物件儲存或資料庫），app 只讀。

**雲端站台與 e-GNSS I95**：I95 的使用授權**仍在申請中**（經夏漢民太空中心
協助向國土測繪中心提出）。雲端站台跑的是同一份設定，因此也會擷取 I95 並在
「RTK 現場查核」頁顯示；核准前的處置是：**不進版控、不進 `data/demo`**，
畫面上一律標示為由官方圖表擷取的非官方衍生值並標註產製者。
若需要在核准前讓公開站台完全不碰它，於該站台設
`SWX_DISABLE_SOURCES=nlsc_egnss_i95` 即可，排程主機仍照常累積歷史
（I95 沒有回填管道，停掉的時段事後補不回來）。

**「RTK 現場查核」頁看不到 I95 時**，該頁會直接說出成因，不必進容器查：
`running` 還在抓、`skipped` 沒納入更新（去看 `status` 與 `SWX_DISABLE_SOURCES`）、
`failed` 抓了但失敗（去看網路與憑證）、`empty` 抓到了但圖上讀不出數值
（對方版面可能改版）、`ok` 抓到了但查詢窗內沒有落點。
「資料健康」頁另有一張**逐來源**的最近更新結果表——上方的通道表只看得到
已寫進資料層的參數，一個從頭到尾抓失敗的來源在那裡是隱形的。

**安全性**：本專案目前所有資料源皆為公開、免認證，
因此雲端部署**不需要任何憑證**。日後若接入需認證的來源
（如 Madrigal、Earthdata），憑證一律走平臺 secrets，
**不得寫入 repository**——本 repo 為公開。

**示範快照會逐漸過期**，其產製時刻見 `data/demo/seed/SW-All.txt` 的
`UPDATED` 標頭。系統以 `swx_core.data_origin()` 判定資料性質：
使用示範快照時，儀表板頂端顯示 **DEMO DATA — NOT OPERATIONAL** 橫幅，
`/health` 亦回傳 `data.is_demo = true`。
只顯示資料齡期不足以防止誤讀——快照內的資料在其自身時間軸上看起來是新的。

### 本機

```bash
streamlit run apps/dashboard/app.py     # http://localhost:8501
python -m services.api.app              # http://127.0.0.1:5100
```

---

## 開發

```bash
python -m pytest tests -q
python -m pytest tests -q -k cssi     # 只跑 CSSI 格式契約測試
python -m pytest tests -q -k density  # 只跑密度與 ap 模式測試
```

測試守的是**跨子計畫介面**而非實作細節：CSSI 格式可逆性、雙時間軸無前視偏差、
品質旗標與參數字典一致性、規則引擎的駐留與遲滯行為、推估與實測的區別。

新增參數前須先在 `configs/params.yaml` 註冊，否則入庫會被判為
`unregistered_param`；有測試確保所有來源宣告的參數都已註冊。
