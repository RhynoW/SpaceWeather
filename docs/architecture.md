# 太空天氣整合資訊與 SDA 應用系統架構建議書

> 對應計畫：太空天氣整合資訊與 SDA 應用模型建構研究（117–118 年，28,000 仟元）
> 版本：**v0.2**（依 `F:\GitHub\Sat_TraingDataExtension` 既有資產全面改寫；v0.1 為無複用之理想架構）
> 系統代號（暫）：**SWX-SDA**（Space Weather eXchange for SDA）
> 參照：`Sat_TraingDataExtension/docs/SDA構想書_可行性風險評估_20260716.md`（該案已對本構想書做過四級能力對映）

---

## 0. 一句話定位

**不重建國家級預報中心，而是建立「任務化轉譯層」**：把既有的國內外太空天氣觀測與指數，轉成任務單位可判讀（L0–L4）、可通報（事件卡）、可介接（API／圖層）、可計算（STK/HPOP 大氣阻力參數）的四類產品。

**v0.2 的核心調整**：議題四（大氣阻力與 SDA/STK 介接）與議題七（歷史回溯驗證）**不是從零開始**——`Sat_TraingDataExtension` 已有可運作的物理引擎、18 年 TLE 全庫、太空天氣參數庫與嚴謹的評估擂台。本架構改以「複用既有骨幹 ＋ 新建電離層／預報／通報三塊」為主軸，並據此重排里程碑與 TRL 起點。

---

> **命名說明**：本文以**層名**（擷取層、資料層、模型轉譯層、預報層、風險轉譯層、
> 產品與介接層、展示與通報層）指稱架構分層，不使用 L 編號——
> `L0–L4` 在本案專指**任務風險等級**，兩者若共用編號極易誤讀。

## 1. 設計原則

| # | 原則 | 架構意涵 |
|---|---|---|
| P0 | **既有資產優先** | 凡 `Sat_TraingDataExtension` 已驗證可運作者，一律複用其實作或模式，不重寫；新建只投在該案零基礎的區塊（電離層、預報、多頻段、通報流程） |
| P1 | 任務導向轉譯優先 | 每一筆科學指標最終都必須能追溯到某一條「任務影響」規則；無法轉譯者不進儀表板主畫面 |
| P2 | 子計畫鬆耦合、契約強耦合 | 子計畫一／二／三之間只透過**版本化資料契約**（schema + API）互動，不共用程式內部結構 |
| P3 | 雙時間軸（bitemporal）儲存 | 每筆資料同時記錄 `valid_time` 與 `ingest_time`，才能做出**無前視偏差**的回放與預報驗證。既有 `raw_tle_archive` 已有 `epoch_utc` / `downloaded_at_utc` 雙欄，此模式直接推廣 |
| P4 | 不承諾絕對準確 | 預報產品強制附帶 `confidence`、命中率／誤警率、提前量；分級規則帶遲滯避免抖動告警 |
| P5 | 來源可替換、降級可運作 | 每一資料項至少定義主來源＋備援；外部斷線時進入 degraded mode 並標示資料齡期，而非顯示舊值假裝正常 |
| P6 | 可重播（replayable） | 任一歷史時刻的產品可由原始資料重新產生，並與當時實際發布結果比對 |
| P7 | 內外網分離 | 外部擷取與內部作業判讀分屬不同資安區，以單向傳輸／排程搬運銜接 |
| P8 | 規則即設定 | L0–L4 門檻、多頻段影響矩陣以宣告式 YAML 維護並納入版控，需求單位可校準門檻而不需改程式 |
| P9 | **讀寫分離、擷取與服務分離** | DuckDB 為單寫入者模型；擷取端只寫 Parquet 分區，服務端以唯讀連線查詢，避免 14 GB 主庫被寫鎖阻斷 |

---

## 2. 既有資產複用盤點

以下為實際讀取 `F:\GitHub\Sat_TraingDataExtension` 程式碼與資料庫後的確認結果。

### 2.1 直接複用（程式可搬、資料可接）

| # | 本案需求 | 既有資產 | 實際狀態 |
|---|---|---|---|
| A1 | 軌道基底資料 | `space_db.duckdb` → `raw_tle_archive`（1,871 萬筆）、`tle_table`（5,890 萬筆）、`tle_raw`（1,492 萬筆） | 14 GB 全庫；含 sma/ecc/inc/raan/argp/M/n/bstar/energy/rmin/rmax 已解算欄位 |
| A2 | TLE 自動擷取 | `download_TLE_unified.py`＋`tle_catnr.py`（Alpha-5 六位編目）＋`config_spacetrack.py` | Space-Track 與本地檔雙模式，DDL 自帶 |
| A3 | **F10.7／Ap／Kp 參數庫** | `space_weather_ap.csv`（CelesTrak SW-All） | **2021-01-01 → 2041-10-01**，2,247 天；含 KP1–8、AP1–8、AP_AVG、F10.7_OBS/ADJ、81 天中心／末端平均，且資料型別分 OBS / INT / PRD / **PRM（預測至 2041）** |
| A4 | F10.7 長期歷史 | `f107_cache.csv` | 2004 年起逐日 |
| A5 | Kp nowcast 擷取 | `data/space_weather/fetch_space_weather.py` | GFZ `Kp_ap_Ap_SN_F107_nowcast.txt` 解析＋與 cache 合併＋日彙整，已可運行 |
| A6 | 太陽活動區 | `space_weather_history.db` → `solar_regions`（SWPC：位置、面積、黑子分類、磁分類） | SQLite，可併入 |
| A7 | **熱氣層密度／阻力物理引擎** | `atmospheric_drag.py` | pymsis（MSIS 2.1）逐時密度、King-Hele 偏心軌道近地點加權、**逐衛星等效彈道係數 B_eff 中位數自校準**、`is_reentry_decay` 再入守門 |
| A8 | 全庫阻力殘差輸出 | `run_drag_residual.py` → `data/drag/drag_resid_*.csv` | 欄位 `drag_resid_da`、`drag_resid_absmax_7d` |
| A9 | **密度反演（觀測側）** | `thermosphere_starlink_tomography.py`、`thermosphere_meme_bccal.py`、`thermosphere_kyoto_repro.py`、`geopotential_energy.py` | 已複現京大 Yamamoto (2026, EPS 78:175) 能量耗散法：EGM96 n=12 全場位能、機動跳階分離、NNLS 斷層重建、逐星 BC 自校準；輸出 ρ_obs/ρ_MSIS 比值分佈 |
| A10 | 資料品質標記 | `data_quality_audit.py` | good／suspect／rejected 三級＋成因規則（缺口外推、Δi 跳變、極端 B*、檢查碼） |
| A11 | 交會／碰撞／SDA 掛鉤 | `conjunction_pipeline.py`（KD-tree 粗篩＋TCA＋Pc）、`run_conjunction_tca.py`、`cdm_predictor.py`、`constellation_anomaly.py`、`hcw_intent.py` | 已運作 |
| A12 | 真值集 | `ids_truth_set/`（ILRS/IDS 14 顆測高衛星 operator 點火日誌）、MEME 精密星曆（283–284 顆 Starlink，公尺級） | 議題七「區分機動 vs 阻力事件」的黃金真值 |
| A13 | API 服務底座 | `backend_duckdb_v2.py`（Flask＋DuckDB＋CORS＋`Settings.from_env`） | 含 `/api/orbit`、`/api/conjunction`、`/api/orbit_czml`、`/api/conjunction_czml`、`/api/rpo_czml` |
| A14 | 3D 態勢展示 | 上列 CZML 端點＋CesiumJS | **CZML 即為 SDA 環境圖層的現成載體** |
| A15 | 儀表板框架 | `conjunction_app/app.py`、`maneuver_app*.py`、`synthetic_app.py`（Streamlit） | 多頁式應用模式成熟 |
| A16 | 文字自動生成 | `ssa_rag_client.py`（SSA-RAG `/ask`，僅 stdlib＋requests，設計即為可移植） | 事件卡敘述、每日摘要 |
| A17 | 報告自動化 | `docs/md_to_docx.py`、`docs/build_*_ppt.py` | 事件復盤報告、期中／期末報告產出 |

### 2.2 模式複用（借架構，換領域）

| # | 本案需求 | 既有模式 | 移植方式 |
|---|---|---|---|
| B1 | **L0–L4 門檻校準** | `fusion_scorer.py`（多通道→單一連續分數，GroupKFold OOF 邏輯斯回歸）、`fusion_fpr_sweep.py`、`ids_domain_fpr.py` | 把「機動機率分數」換成「任務風險分數」；FPR 掃描即**誤警率控制**，直接對應構想書「避免過度告警」 |
| B2 | **預報驗證框架** | `three_layer_common_eval.py`（同一測試集／同一 GT／同一操作點／GroupKFold OOF／隨機對照）、`bootstrap_ci.py` | 三層擂台換成「基線 vs 統計 vs ML 預報」三層；episode 級 recall＋latency 直接對應**命中率＋提前量** |
| B3 | 變點偵測 | `statistical_detectors.py`（CUSUM／BOCPD／SSA／3σ-MAD，純 numpy/scipy） | 地磁暴起始、TEC 擾動起始偵測可直接套用同一組偵測器 |
| B4 | 時序模型 | `lstm_autoencoder.py`、`patch_transformer.py`、`ml_bigru_labeler.py`、`ml_model2_anomaly.py` | 6 小時預報的 Tier 2 模型骨架 |
| B5 | 資料庫分發 | `prc_maneuver/build_slim_db.py`（14 GB → 168 MB slim 庫） | 內網部署／展示用精簡庫 |
| B6 | 專案慣例 | `.claude/CLAUDE.md`（繁中、先結論後細節、改動 >3 檔先說計畫） | 沿用 |

### 2.3 必須新建（該案零基礎）

| # | 項目 | 對應議題 | 說明 |
|---|---|---|---|
| C1 | Dst／SYM-H、IMF Bz、太陽風速度密度、X-ray 通量、粒子通量擷取 | 一 | 既有僅 F10.7 與 Ap/Kp；其餘通道全缺（NOAA SWPC／OMNIWeb／Kyoto WDC 公開，屬中低風險） |
| C2 | TEC／ROTI／S4／閃爍／電離層探測／掩星 | 一、五 | 零基礎，且需 TASA／中央氣象署協調資料 |
| C3 | 全球地磁參考模型＋區域磁力計 | 二 | 零基礎，需 IGRF/CHAOS＋在地測站 |
| C4 | 1/3/6 小時預報引擎 | 三 | 既有全為事後／近即時偵測，**無任何 forecasting 模型** |
| C5 | 多頻段訊號影響矩陣 | 五 | 電波傳播領域，與軌道動力學無交集 |
| C6 | L0–L4 軍事分級定義與通報流程 | 六 | 技術骨架可複用 B1，但等級定義、通報對象、處置建議須需求方共同校準 |
| C7 | STK/HPOP 實際介接與驗證 | 四 | 既有以 SGP4／dsgp4 傳播，**無 STK 整合**；需授權與端到端驗證 |
| C8 | 事件卡狀態管理與發布流程 | 六 | OLTP 性質（修訂、審核、發布），既有無此類服務 |

### 2.4 複用帶來的三個結論

1. **議題四的 TRL 起點應上修**。構想書列「大氣阻力模型與 STK/SDA 介接輸出」現有 TRL 3。實際上物理引擎（A7）、觀測反演（A9）、全庫輸出（A8）皆已運作並有真值驗證（A12），**缺的只有 STK 端介接（C7）**。建議在 TRL 表把此項現有等級改列 **4–5**，並把風險敘述改為「STK 端格式與授權」而非「模型未建立」——這對審查是加分而非減分，因為有實作可佐證。

2. **密度修正因子可從「模型輸出」升級為「觀測校正之模型輸出」**。一般作法是拿 F10.7/Ap 餵 NRLMSIS 得密度；本案因有 A9，可額外由 TLE／MEME 星曆**反演實際密度**，輸出 `ρ_obs/ρ_MSIS` 比值場。這是構想書未預期的增強，也是議題四能通過「以案例比較驗證」的關鍵證據。

3. **複用無權利障礙，起步可大幅提前**。上游專案 `Sat_TraingDataExtension` 之成果依契約僅需解繳程式碼與報告，**所有權可複用及保留，且與本計畫分屬不同委託來源**。故程式與資料庫可直接搬用，無須等待權利確認，也不必為交付邊界做隔離設計。實務影響有二：(a) 14 GB `space_db.duckdb` 可直接複製或 ATTACH，2021 年起的 SW 參數與 2004 年起的 F10.7 即刻可用；(b) §17 第 4 項所稱「無法事後補救的歷史資料」，其中 TLE 與 F10.7/Ap/Kp 兩大類**已經在庫內**，2022-02 Starlink 再入與 2024-05 Gannon 兩個回放事件的軌道與驅動參數不必重抓，只需補 C1 各通道的歷史。
   （僅第三方資料本身的條款仍獨立適用：Space-Track 限制對外再散布、CelesTrak 有其引用規範。這只影響「產品能否含原始資料對外發布」，不影響內部研究複用。）

---

## 3. 總體架構（分層）

```
┌────────────────────────────────────────────────────────────────────────────┐
│ 展示與通報層  儀表板 / 事件卡 / 每日摘要 / 任務前風險提示 / 告警推播       │  SP3
│                  ← 複用 Streamlit(A15) + CesiumJS/CZML(A14) + RAG 文字(A16) │
├────────────────────────────────────────────────────────────────────────────┤
│ 產品與介接層  SWX API │ STK/HPOP 匯入檔 │ GIS/CZML 風險圖層 │ 事件卡      │  SP3
│                  ← 複用 Flask+DuckDB 底座(A13)；新建 STK 介接(C7)、事件卡(C8)│
├────────────────────────────────────────────────────────────────────────────┤
│ 風險轉譯層    L0–L4 分級規則引擎 │ 多頻段影響矩陣 │ 任務剖面風險計算       │  SP2
│                  ← 複用門檻校準/FPR 掃描模式(B1)；新建等級定義(C6)、矩陣(C5) │
├────────────────────────────────────────────────────────────────────────────┤
│ 預報層        1/3/6 小時短時預報 │ 特徵庫 │ 模型登錄 │ 驗證擂台            │  SP2
│                  ← 複用驗證擂台(B2)、變點偵測(B3)、時序模型(B4)；預報引擎新建 │
├────────────────────────────────────────────────────────────────────────────┤
│ 模型轉譯層    地磁(新) │ 電離層(新) │ **熱氣層/阻力(已具備 A7+A9)** │ 訊號(新)│ SP1
├────────────────────────────────────────────────────────────────────────────┤
│ 資料層        DuckDB 分析庫(A1) + Parquet 落地 + 作業狀態庫 │ 品質標記(A10)│  SP1
├────────────────────────────────────────────────────────────────────────────┤
│ 擷取層        TLE(A2) │ CelesTrak SW(A3) │ GFZ Kp(A5) │ SWPC/TEC/磁力計(新)│  SP1
└────────────────────────────────────────────────────────────────────────────┘
   橫向服務：排程 │ 資料品質監控 │ 稽核與版本 │ 身分權限 │ 備份備援
```

---

## 4. 子計畫責任邊界與交付契約

| 層 | 主責 | 交付物（即介面契約） |
|---|---|---|
| 擷取～模型層 | **子計畫一** | `swx_observation` 標準時序（Parquet＋DuckDB view）、`param_registry` 參數字典、品質標記規則、區域地磁擾動指標、TEC/ROTI/S4 產品、熱氣層密度與 `ρ_obs/ρ_model` 比值場 |
| 預報～風險層 | **子計畫二** | `forecast_series`（1/3/6h＋信心度）、`rules/*.yaml`（L0–L4 門檻）、`matrix/*.yaml`（多頻段）、驗證報告（POD/FAR/CSI/提前量，以 B2 擂台產出） |
| 產品～展示層 | **子計畫三** | OpenAPI 規格與 SWX API、事件卡 JSON Schema 與發布服務、STK/HPOP 匯入檔產生器、CZML/GeoJSON 風險圖層、原型儀表板、回放平台、操作手冊 |

> **治理規則**：子計畫三不得直接讀取子計畫一、二的內部中間檔，一律走契約介面。這是 118 年「整合展示」不淪為人工拼裝的唯一保障。

---

## 5. 擷取層

### 5.1 統一介接器契約

沿用 `download_TLE_unified.py` 的環境變數與 `.env` 模式（既有已有 `SPACE_TRACK_IDENTITY` 等鍵），設定集中於 `configs/sources.yaml`：

```yaml
- source_id: celestrak_sw_all
  tier: 1
  provides: [F107_OBS, F107_ADJ, F107A_81C, KP1_8, AP1_8, AP_AVG]
  cadence_s: 86400
  reuse_from: "Sat_TraingDataExtension/space_weather_ap.csv"   # 既有資產
  data_types: [OBS, INT, PRD, PRM]        # PRM 預測至 2041 → 直接支援長期軌道傳播
  fallback: [gfz_nowcast]
  latency_budget_s: 172800

- source_id: gfz_nowcast
  tier: 2
  provides: [KP_3H, AP_3H, F107, SN]
  cadence_s: 10800
  reuse_from: "data/space_weather/fetch_space_weather.py"
  endpoint: "https://kp.gfz-potsdam.de/app/files/Kp_ap_Ap_SN_F107_nowcast.txt"

- source_id: swpc_dscovr_mag        # 新建（C1）
  tier: 1
  provides: [IMF_BZ, IMF_BT, SW_V, SW_N]
  cadence_s: 60
  fallback: [swpc_ace_mag]
  latency_budget_s: 300
```

### 5.2 來源盤點（議題一）

| 類別 | 參數 | 主來源 | 狀態 |
|---|---|---|---|
| 太陽輻射通量 | F10.7、F10.7A(81d) | CelesTrak SW-All | **已具備 A3**（含預測至 2041） |
| 地磁指數 | Kp、Ap（3h 與日） | CelesTrak SW-All ＋ GFZ nowcast | **已具備 A3/A5** |
| 太陽活動區 | 黑子面積、分類、磁分類 | SWPC solar_regions | **已具備 A6** |
| 太陽 X-ray | GOES XRS 通量、閃焰事件 | NOAA/SWPC | 新建 C1 |
| 太陽風／IMF | SW_V、SW_N、Bz、Bt | DSCOVR RTSW（備援 ACE） | 新建 C1 |
| 高能粒子 | 質子 ≥10/≥100 MeV | GOES SEM | 新建 C1 |
| 環電流指數 | Dst、SYM-H | Kyoto WDC（備援 SWPC 估計） | 新建 C1 |
| 區域地磁 | 三軸磁場、ΔH | 國內磁力計站 | 新建 C3，**須外部協調** |
| 電離層 | TEC、ROTI、S4、σφ | 地基 GNSS 網、IGS GIM | 新建 C2，**須外部協調** |
| 電離層剖面 | foF2、hmF2、MUF(3000) | 探測儀、FS-3/FS-7 掩星 | 新建 C2，**須外部協調** |
| 軌道 | TLE 全庫、MEME 精密星曆 | Space-Track、SpaceX 公開星曆 | **已具備 A1/A2/A12** |

### 5.3 擷取模式

- **原始落地優先**：`raw/{source_id}/{yyyy}/{mm}/{dd}/{ts}.{ext}`，先原封存檔再解析。解析錯誤可重跑，不需重新向外抓取——既有 `tle_downloads_temp → tle_downloads` 的搬移模式即此設計，沿用。
- **Parquet 分區寫入**：每個 collector 只寫自己的 Parquet 檔（`data/swx_parquet/param={code}/date={yyyy-mm-dd}/{source}.parquet`），避免與 14 GB DuckDB 主庫爭寫鎖（P9）。
- **資料齡期監控**：`age = now - latest_valid_time` 超過 `latency_budget_s` 即標黃／標紅並寫入品質事件。

---

## 6. 資料層

### 6.1 儲存分工（v0.2 的關鍵修正）

v0.1 曾建議 PostgreSQL+TimescaleDB+PostGIS。實際盤點後改為**沿用既有 DuckDB 生態**，理由：既有 14 GB TLE 資產與全部分析程式都建立在 DuckDB 上，遷移成本高且無實質收益；本案資料量級（時序指數與格網）DuckDB 完全勝任。但 DuckDB 為單寫入者，故做三分：

| 用途 | 技術 | 內容 |
|---|---|---|
| **分析／檔案庫** | DuckDB（複用 `space_db.duckdb`，另建 `swx_db.duckdb` 或 ATTACH） | TLE 全庫、觀測時序、模型輸出、歷史回放 |
| **近即時落地** | Parquet 分區（Hive 式） | 各 collector 獨立寫入；DuckDB 以 `read_parquet(..., hive_partitioning=1)` 建 view 聯集 |
| **作業狀態** | SQLite（或小型 PostgreSQL） | 事件卡與其修訂、通報紀錄、審核狀態、使用者與權限——OLTP 性質，需並發寫入與交易 |

GIS 圖層不另建 PostGIS：TEC/閃爍等為規則格網，存 Parquet，輸出時轉 GeoJSON／CZML 即可（複用 A14 的 CZML 產生模式）。少一個要維運的服務。

### 6.2 核心時序表（bitemporal）

```sql
-- swx_db.duckdb
CREATE TABLE swx_observation (
  valid_time    TIMESTAMP NOT NULL,   -- 物理有效時間
  ingest_time   TIMESTAMP NOT NULL,   -- 入庫時間（回放用；對應既有 downloaded_at_utc）
  param_code    VARCHAR   NOT NULL,   -- 參照 param_registry
  value         DOUBLE,
  unit          VARCHAR   NOT NULL,
  source_id     VARCHAR   NOT NULL,
  source_tier   SMALLINT  NOT NULL,
  quality_flag  VARCHAR   NOT NULL,   -- good / suspect / rejected（沿用 A10 用語）
  quality_reason VARCHAR,             -- 觸發規則（沿用 A10 設計）
  confidence    REAL,
  lat           DOUBLE, lon DOUBLE,   -- 具空間性者
  grid_id       VARCHAR,
  revision      INTEGER   NOT NULL DEFAULT 0,  -- 來源修正版（Dst quicklook → final）
  data_type     VARCHAR                        -- OBS/INT/PRD/PRM（沿用 CelesTrak 語彙）
);
```

`data_type` 直接沿用 CelesTrak 的 OBS／INT／PRD／PRM 語彙，好處是 A3 的既有欄位零轉換入庫，且「觀測值 vs 預測值」在資料層就分得清楚——這對預報驗證（不可拿預測當真值）是必要的。

**回放查詢**（P3／P6）：

```sql
SELECT DISTINCT ON (valid_time) valid_time, value
FROM swx_observation
WHERE param_code = 'DST' AND ingest_time <= ? AND data_type IN ('OBS','INT')
ORDER BY valid_time, source_tier, ingest_time DESC;
```

### 6.3 參數登錄（`param_registry`）

單一權威字典：`param_code`、中英文名稱、單位、值域、正常／異常門檻、時間解析度、空間屬性、所屬領域、對應國際分級（G/R/S）、對應任務影響網域。**UI 標籤、API 欄位說明、資料字典文件皆由此表生成**。首批載入即 A3 的全部欄位（KP1–8、AP1–8、AP_AVG、F10.7 四種變體）。

### 6.4 品質控管

直接移植 `data_quality_audit.py` 的三級制與「規則＋成因字串」設計：

`範圍檢核 → 突波／梯度檢核 → 跨源交叉比對 → 時間同步 → 缺漏標記與補值（僅標記不覆蓋）→ 可信度計算`

補值寫入新列並標 `quality_flag='suspect'`＋`quality_reason='interpolated'`，原始列保留。

---

## 7. 模型轉譯層

| 模組 | 狀態 | 輸入 | 輸出 |
|---|---|---|---|
| `model_thermo` | **已具備（A7+A9）** | F10.7、Ap/Kp、Dst；TLE／MEME 星曆 | MSIS 2.1 密度場、`ρ_obs/ρ_model` 比值場、B_eff、阻力殘差、再入守門 |
| `model_geomag` | 新建 C3 | IGRF/WMM、區域磁力計、Kp/Ap/Dst/SYM-H | 區域擾動指數 `dB_TW`、地磁暴相位（初相／主相／恢復相） |
| `model_iono` | 新建 C2 | TEC、ROTI、S4、foF2、掩星剖面 | 電離層擾動圖層、閃爍機率圖、HF 吸收指標、MUF/LUF |
| `model_signal` | 新建 C5 | 上列＋頻段／鏈路參數 | HF 吸收 dB、GNSS 斜距延遲、閃爍失鎖機率、X/Ka 太陽射電爆干擾窗 |

### 7.1 熱氣層／阻力模組（議題四核心，已具備）

既有物理鏈（`atmospheric_drag.py`）：

```
近圓軌道阻力衰減：  da/dt = -B · ρ · √(μa)
偏心軌道（King-Hele）：於近地點取密度，幾何因子 geom = e^{-z}[I₀(z)+2e·I₁(z)]，z = a·e/H
逐衛星校準：        B_eff = median( -Δa_i / s_i )      ← 中位數穩健，排除機動離群
阻力殘差：          drag_resid = Δa + B_eff · s
```

既有觀測反演鏈（`thermosphere_*.py`，京大 EPS 2026 法複現）：

```
比能量：E = ½v² − Φ(r)，Φ 用 EGM96 n=12 全場位能（測地振盪由 ~19500 J/kg 降至 ~90 J/kg）
機動分離：E 的離散跳階 = 站位保持機動，取最長潔淨弧
密度：dE/dt = −½ B ρ v_r(v_r·v)  →  ρ_obs = (−dE/dt)/(⟨v³⟩·BC_s)
逐星 BC 自校準：BC_s = median_arcs[ (−dE/dt)/(⟨v³⟩·ρ_msis) ]   ← 消除各星彈道係數差異
斷層：ρ = exp(−(h−h_r)/H)·f(φ, λ_LT)，球諧 Lmax=1，least_squares／NNLS 反演
```

**本案要新增的只有三件事**：

1. 把 `ρ_obs/ρ_MSIS` 比值整理成**依高度帶×時間**的 `rho_correction` 產品表（含不確定度區間）。
2. 把驅動參數輸出成 STK 可讀格式（見 §9.3）。
3. 以事件期（2024-05 Gannon 等）做三組傳播比較，量化誤差縮減（見 §9.3 驗證）。

**誠實界定**（既有文件已自陳，本案應沿用同樣的界定，不可誇大）：TLE 之 sma 雜訊 24–75 m，故 TLE 版為**聚合、相對**之粗版；MEME 精密星曆版精度較高但僅涵蓋 Starlink；弧群聚於窄高度帶，**無法獨立回收尺度高**（需固定 H）；2D 日變場需加速度／POD 法，能量法不可及。

---

## 8. 預報層（新建 C4，但驗證框架複用 B2）

```
歷史+即時資料 ──► 特徵庫 ──► 模型群 ──► 預報序列 ──► 校準 ──► 驗證擂台
                  (滯後、滑動   (基線/統計/ML)  (+信心度)   (複用 three_layer
                   統計、事件旗標)                          _common_eval 模式)
```

### 8.1 模型分層策略

- **Tier 0 基線**：持續性（persistence）與氣候平均。**所有 ML 模型必須贏過基線才准上線**。
- **Tier 1 統計／物理啟發**：太陽風–磁層耦合函數 → Kp/Dst 遞推；TEC 日變化＋擾動殘差。變點偵測直接用 `statistical_detectors.py`（B3）的 CUSUM/BOCPD/SSA/3σ-MAD。
- **Tier 2 機器學習**：以 `lstm_autoencoder.py`／`patch_transformer.py`（B4）為骨架，輸出**機率分布**而非單點值。

### 8.1.1 兩組預報目標（實作後補記）

構想書要求 1／3／6 小時三種產品。實作時發現 **1 小時 horizon 不能建在 Kp 上**：
Kp 是 3 小時指數，1 小時 horizon 得到的是同一個 3 小時值的另一種說法，
而暴起始時刻本身已被 3 小時取樣糊掉 1–2 小時，提前量根本量不出來。

故預報層改為兩組目標並行，各自訓練、各自驗證，成績不可橫向比較：

| 目標 | 格點 | horizon | 用途 |
|---|---|---|---|
| `KP_3H` | 3 小時 | 3／6／12／24／48 h | 既有產品；48 h 為研究性延伸 |
| `HP30` | 30 分鐘 | **1**／3／6 h | 構想書的 1 小時產品；提前量可量測 |

Hp30 例行擷取只解析近 120 天（全檔逾 70 萬列），訓練用歷史以
`--reparse --window-days 2100` 由既有原始落地檔重新解析取得，不需重抓。

### 8.2 驗證擂台（直接移植 `three_layer_common_eval.py` 的方法論）

該檔的設計正是為了回應「三層各用不同測試集、數值不可比」的審查意見，本案面臨完全相同的問題（基線 vs 統計 vs ML 預報），故整套移植：

| 既有設計 | 本案對應 |
|---|---|
| 同一測試集、同一 Ground Truth、同一評估單元 | 同一事件集、同一觀測真值、同一預報時窗 |
| GroupKFold(5) 依 norad 分組，OOF 分數 | 依**事件**分組，杜絕同一場地磁暴跨 train/test |
| FPR ≤ 0.05 floor 操作點 | **誤警率上限**作為固定操作點 |
| episode 級 recall（48h 合併）＋ latency | 事件級**命中率** ＋ **提前量** |
| naive 隨機分數對照 | 同左，驗證擂台具鑑別力 |
| `bootstrap_ci.py` 信賴區間 | 同左，指標需附 CI 而非單一數字 |

**指標**：POD、FAR、CSI、Heidke skill score、**事件段命中率與提前量**、可靠度圖、Brier skill score，MAE 附 bootstrap CI。提前量以事件段計（定義見 docs/forecast_verification.md），可為負值代表事後偵測；必須與事件段命中率並列，單獨呈現會誤導。輸出 `validation_report.html` 作為期末報告附件。

### 8.3 模型登錄

`model_registry`：模型 ID、版本、訓練資料期間、特徵集雜湊、超參數、驗證分數、上線／退役時間。每筆預報結果記錄 `model_id@version`。

---

## 9. 風險轉譯層

### 9.1 規則引擎（宣告式）

```yaml
# configs/rules/gnss_pnt.yaml
domain: GNSS_PNT
band: L
version: 2026.1
rules:
  - id: GNSS-L3-SCINT
    when:
      any:
        - {param: S4,   op: ">=", value: 0.6, dwell_min: 15, area_frac: 0.3}
        - {param: ROTI, op: ">=", value: 0.8, dwell_min: 15}
    level: L3
    hysteresis: {clear_below: 0.45, clear_dwell_min: 30}
    confidence_from: [S4.confidence, forecast.confidence]
    impact: "定位精度劣化、載波失鎖風險升高；授時同步可能中斷"
    action: "改用多頻多系統接收、延後高精度作業、啟用慣性／守時備援"
    notify: [通信單位, 衛星地面站, SDA 操作席]
```

引擎特性：遲滯與駐留時間避免抖動告警；多網域各自評級後取最高為總級但保留分項；可信度傳遞（達 L3/L4 但低信心者標「待確認」）。

### 9.2 門檻校準（複用 B1）

`fusion_fpr_sweep.py`／`ids_domain_fpr.py` 的 FPR 掃描邏輯直接移植為 `whatif_threshold.py`：需求單位調整門檻後，立即以歷史資料回答「過去兩年會發出幾次 L3、其中幾次為誤警」。構想書 TRL 表把「門檻須與需求單位共同校準」列為風險，這個工具就是化解該風險的具體手段。

### 9.3 多頻段影響矩陣（新建 C5）

矩陣維度：`頻段 × 事件型態 × 任務型態 × 時段/幾何`。

| 頻段 | 主要機制 | 判定指標 | **須排除之非太空天氣因素** |
|---|---|---|---|
| HF (3–30 MHz) | D 層吸收、MUF/LUF 變動、極蓋吸收 | X-ray 通量（R 級）、SEP、foF2/MUF | 設備功率、天線、頻率規劃、日夜變化 |
| VHF/UHF | 電離層閃爍、法拉第旋轉 | S4、ROTI、地方時／地磁緯度 | 地形遮蔽、多路徑、人為干擾 |
| GNSS L-band | TEC 斜距延遲、閃爍失鎖 | TEC/STEC、S4、σφ、失鎖率 | 接收機品質、遮蔽、干擾／欺騙、多路徑 |
| S-band | 閃爍（較弱）、鏈路餘裕壓縮 | S4、鏈路預算 | 雨衰、指向誤差、仰角 |
| X/Ka-band | 太陽射電爆發（對日角）；電離層影響很小 | 太陽射電通量、太陽–站–星夾角 | **雨衰為主因**、設備、對日干擾週期 |
| 軌道預報 | 熱氣層密度上升→阻力增加 | `ρ_obs/ρ_model`、Ap/Kp、`drag_resid` | **既有 `is_reentry_decay` 守門可區辨自然再入**；機動、TLE 缺口外推（A10 已標記） |

最後一列是本案相對其他太空天氣系統的獨特處：**「軌道異常是地磁暴還是機動」這條排除鏈已有可運作的實作**（A7 的阻力殘差＋A10 的品質標記＋A12 的 operator 真值），正對應構想書技術瓶頸④「避免異常機動誤判」。

---

## 10. 產品與介接層

### 10.1 SWX API

沿用 `backend_duckdb_v2.py` 的 Flask＋DuckDB 唯讀連線＋`Settings.from_env` 模式，新增路由：

```
GET  /v1/params                              參數字典
GET  /v1/obs?param=DST&from=&to=&as_of=      觀測序列（as_of 觸發回放）
GET  /v1/nowcast                             各網域即時等級
GET  /v1/forecast?target=kp|hp30&horizon=    預報序列＋信心度＋該 horizon 實測技巧
                                             （已實作：每列附 POD/FAR/提前量與最佳基線）
GET  /v1/events  |  /v1/events/{id}          事件清單／事件卡
POST /v1/risk/mission-brief                  任務前風險提示
GET  /v1/matrix/band-impact                  多頻段影響矩陣與查核表
GET  /v1/exports/stk/spaceweather.txt        STK CSSI 驅動檔
GET  /v1/exports/jb2008/{solfsmy|dtcfile}    JB2008 驅動檔
GET  /v1/exports/drag-correction?alt=&from=  密度修正因子表
GET  /v1/layers/{layer}.geojson              GIS 風險圖層
GET  /v1/layers/{layer}.czml                 ← 複用既有 CZML 產生器(A14)，Cesium 直接吃
GET  /v1/stream/alerts                       SSE 即時告警
```

既有 `/api/orbit_czml`、`/api/conjunction_czml`、`/api/rpo_czml` 保留，成為 SDA 圖層與太空天氣圖層疊加的基礎。所有回應含 `data_age_s`、`degraded` 旗標。

### 10.2 事件卡資料模型

```json
{
  "event_id": "SWX-20260517-GS-001",
  "schema_version": "1.0",
  "issued_utc": "2026-05-17T06:15:00Z",
  "revision": 3,
  "supersedes": "SWX-20260517-GS-001@r2",
  "type": "GEOMAGNETIC_STORM",
  "international_scale": "G4",
  "mission_level": "L3",
  "confidence": 0.72,
  "timeline": {"onset_utc": "...", "peak_utc": "...", "expected_end_utc": "...", "duration_h": 18},
  "affected_region": {"type": "Polygon", "coordinates": [[]]},
  "drivers": [{"param": "DST", "value": -180, "unit": "nT", "source_id": "kyoto_wdc"}],
  "impacts": [
    {"domain": "GNSS_PNT", "band": "L", "level": "L3",
     "metric": {"tec_delay_m": 8.4, "s4_p90": 0.71},
     "statement": "定位精度劣化與載波失鎖風險升高",
     "exclusions_checked": ["干擾", "遮蔽", "接收機"]},
    {"domain": "ORBIT_PREDICTION", "level": "L3",
     "metric": {"rho_ratio_400km": 1.9, "drag_resid_absmax_7d": 0.83,
                "along_track_err_24h_km": 3.2},
     "exclusions_checked": ["機動(operator log)", "TLE 缺口外推", "自然再入"]}
  ],
  "orbit_products": {
    "f107": 168.0, "ap_forecast": [56, 80, 67],
    "rho_correction": [{"alt_band_km": [300, 400], "ratio": 2.1, "unc": 0.4,
                        "method": "energy-dissipation, BC self-calibrated"}],
    "stk_export": "/v1/exports/stk/spaceweather.txt?as_of=2026-05-17T06:15Z"
  },
  "recommendations": ["延後高精度 PNT 作業", "低軌目標軌道預報改用 6h 更新週期"],
  "sda_hooks": {"record_in_sda": true, "correlate_with": ["ANOMALOUS_MANEUVER", "CONJUNCTION"]},
  "forecast_basis": {"model_id": "kp-gbm", "model_version": "1.4.2", "horizon_h": 6},
  "sources": ["celestrak_sw_all", "kyoto_wdc", "spacetrack_tle"]
}
```

`ORBIT_PREDICTION` 分項的 `metric` 與 `exclusions_checked` 皆可由既有管線（A8、A10、A12）自動填入——這是複用帶來的直接好處：事件卡最硬的一欄不必人工填寫。事件卡文字敘述（`statement`、`recommendations`）可由 `ssa_rag_client.py`（A16）生成後人工覆核。

事件卡本體存於**作業狀態庫**（SQLite/PostgreSQL，§6.1），因其有修訂、審核、發布狀態機。

### 10.3 STK/HPOP 介接

| 階段 | 方式 | 成本評估 |
|---|---|---|
| A（117 Q2–Q3） | **檔案投放**：產生 CSSI `SpaceWeather-All-v1.2.txt`，置入 STK 讀取路徑 | **極低**。既有 `space_weather_ap.csv` 就是 CelesTrak SW-All 的 CSV 序列化，欄位（KP1–8、AP1–8、AP_AVG、F10.7_OBS/ADJ/81d、OBS/INT/PRD/PRM 型別）與 STK 讀的 CSSI 文字檔內容同源，只需寫**格式轉換腳本**，不需新增資料源 |
| B（117 Q4–118 Q2） | **STK 自動化**：Python API 建場景、載 TLE、套兩組驅動參數、批次傳播、輸出誤差比較 | 中。既有無 STK 整合（C7），需授權與端到端驗證；TLE 來源與比較腳本可複用 |
| C（118 Q3–Q4） | **資料產品交付**：只交付密度修正因子表＋事件標記＋匯入格式規範 | 中。避免綁定 STK，供未來 SDA 平臺自建軌道模組採用 |

> 經費對應：子計畫三兩年各編列 STK 研究版授權 2 套（1,000,000 元／年），支撐階段 B/C。

**階段 B 的驗證設計**（決定議題四能否達 TRL 5）：對選定低軌衛星執行三組傳播——(a) 平靜期預設參數、(b) 觀測驅動 F10.7/Ap、(c) 本案 `rho_correction` 修正——比較 24/48/72 小時**沿軌向誤差**。真值用 MEME 精密星曆（A12，公尺級）而非另一組 TLE，這點很重要，否則誤差比較會被 TLE 自身雜訊（sma 24–75 m）淹沒。比較流程寫成 `tools/orbit_backtest.py`，不可人工操作。

### 10.4 風險圖層

規則格網存 Parquet，輸出 GeoJSON（平面地圖）與 **CZML**（Cesium 3D，複用 A14）。圖層：TEC 值與梯度、ROTI/S4 閃爍機率、HF 吸收、極光邊界、地磁擾動強度、**LEO 阻力增量帶**（後者可由 A8/A9 直接產生，是既有能力的直接視覺化）。

---

## 11. 展示與通報層

沿用 Streamlit 多頁式（A15）作為原型，正式展示以既有 Flask＋Cesium 前端（A13/A14）承載。

| 畫面 | 內容 | 複用來源 |
|---|---|---|
| 太空環境總覽 | 六網域紅綠燈（太陽／地磁／電離層／GNSS／HF／軌道）＋24h 趨勢＋資料齡期 | Streamlit 框架 |
| 事件卡 | 時間軸、指標變化、影響分項、處置建議、匯出 | 新建＋RAG 文字(A16) |
| 任務前風險提示 | 輸入任務剖面 → 1/3/6h 風險摘要 | 新建 |
| 多頻段查核表 | 互動決策樹：異常現象 → 排除因素 → 太空天氣可能性 | 新建 |
| SDA 環境圖層 | Cesium 地圖＋圖層切換＋時間軸，可疊加軌道與交會 | **A14 CZML 直接複用** |
| 軌道風險 | 阻力殘差、密度比值、再入守門、交會清單 | **A8/A9/A11 直接複用** |
| 事件復盤 | 選定時窗自動生成復盤報告 | **A17 報告產生器複用** |
| 資料健康 | 各來源齡期、缺漏率、品質旗標分布、模型上線狀態 | A10 模式 |

**通報管道**：儀表板橫幅 → SSE 推播 → 郵件／內部通訊；**L3 以上需人工確認後發布**（human-in-the-loop）。

---

## 12. 部署與資安分區

```
┌── Zone A 外部擷取區（DMZ）──────────┐
│ collector-* / raw landing / Parquet  │  對外 HTTPS 出向，只出不進
└──────────────┬──────────────────────┘
               │ 單向傳輸（data diode／排程搬運，僅資料檔）
┌──────────────▼── Zone B 作業區 ─────┐
│ DuckDB 分析庫（swx + 複用 space_db）│
│ Parquet 湖 │ SQLite 作業狀態庫       │
│ model_* │ forecast │ risk_engine     │
│ swx-api (Flask) │ dashboard │ 排程   │
└──────────────┬──────────────────────┘
               │ 內網 API（唯讀）
┌──────────────▼── Zone C 應用區 ─────┐
│ STK 工作站 ×2 │ 未來 SDA 平臺 │ 展示 │
└─────────────────────────────────────┘
```

- **Docker Compose ＋ 單機伺服器**起步，不導入 Kubernetes。
- **精簡庫分發**：複用 `build_slim_db.py`（B5）模式產生內網展示用小庫（該案已做到 14 GB → 168 MB），解決跨區搬運與展示機容量問題。
- 備份 3-2-1；DB 每日快照；稽核表 append-only（通報、事件卡修訂、門檻異動）。

### 技術選型（v0.2，以既有為準）

| 分類 | 選型 | 與 v0.1 差異／理由 |
|---|---|---|
| 語言 | Python 3.12 | 不變；既有全 Python |
| 分析庫 | **DuckDB** ＋ Parquet | **改**（原 TimescaleDB）：複用既有 14 GB 資產與全部分析程式 |
| 作業狀態庫 | **SQLite**（可升 PostgreSQL） | **新增**：事件卡與通報流程需交易與並發寫入，不適合 DuckDB |
| 空間資料 | Parquet ＋ GeoJSON/CZML 輸出 | **改**（原 PostGIS）：格網資料不需空間資料庫，少一個維運服務 |
| API | **Flask**（複用 `backend_duckdb_v2.py`） | **改**（原 FastAPI）：既有底座可直接擴充；OpenAPI 以 apispec 補上 |
| 3D 展示 | **CesiumJS + CZML** | **新增**：既有已有三組 CZML 端點，SDA 圖層的最短路徑 |
| 儀表板 | Streamlit（原型）＋ 既有前端（正式） | 微調 |
| 密度模型 | **pymsis (MSIS 2.1)** | 明確化；既有已用 |
| 軌道 | sgp4／dsgp4／skyfield ＋ STK（授權） | 明確化；交叉驗證用既有 SGP4 鏈 |
| 排程 | Prefect 2（或 Windows 排程＋既有 .bat 模式） | 既有以 `.bat`＋腳本排程；規模不大時可先沿用，避免導入成本 |
| ML | scikit-learn／LightGBM／PyTorch | 既有已用（B4） |

---

## 13. 期程與里程碑（依複用重排）

### 117 年

| 季 | 子計畫一 | 子計畫二 | 子計畫三 |
|---|---|---|---|
| Q1 | **資產移入與整併**；`param_registry` v1；A3/A4/A5 入庫 | 預報需求定義、事件集選定 | 架構定案、資料契約 v1、環境建置、**既有 API 底座移植** |
| Q2 | C1 擷取器（Dst/IMF/太陽風/X-ray/粒子）；品質規則移植 | 特徵庫、基線模型（Tier 0） | SWX API v0；**STK 階段 A 打通**；儀表板骨架 |
| Q3 | C3 地磁基準場＋區域擾動 v1；C2 電離層資料協調 | 1/3h 預報雛形；L0–L4 規則 v1 | 事件卡 Schema v1＋作業狀態庫；回放平台 |
| Q4 | 電離層產品 v1；`rho_correction` 產品表定版 | 6h 預報雛形；多頻段矩陣 v1 | 原型展示（≥1 案例端到端）；STK 階段 B 起步 |

**117 年關鍵驗收**：對任一選定歷史事件，從原始資料 → 分級 → 事件卡 → STK 匯入檔 → 軌道誤差比較，端到端跑完且可重播。

> 因 A7/A9/A12 已具備，此驗收的「軌道誤差比較」段落在 **Q2 即可先行單獨驗證**，不必等全鏈路。建議把它當成 117 年期中報告的第一個硬成果。

### 118 年

| 季 | 重點 |
|---|---|
| Q1 | 來源擴充與自動更新；模型修正；驗證擂台（B2 移植）完成 |
| Q2 | ≥15 歷史事件回測，POD/FAR/CSI／提前量報告（附 bootstrap CI）；門檻與需求單位校準工作坊 |
| Q3 | STK 階段 B/C；`rho_correction` 定版；GIS/CZML 圖層與 SDA 介接規格定版 |
| Q4 | 完整情境展示、操作手冊、成果移轉、工程化建置建議書 |

---

## 14. 驗收指標

| 面向 | 指標 | 目標 |
|---|---|---|
| 資料 | 介接來源數／自動更新成功率／資料齡期達標率 | ≥15 源／≥98%／≥95% |
| 品質 | 品質旗標正確率、跨源一致性檢核通過率 | ≥95% |
| 預報 | 6h 地磁擾動 POD／FAR／相對基線技巧提升 | POD ≥0.7、FAR ≤0.4、**優於 persistence 基線**（附 CI） |
| 分級 | 歷史事件重播之漏報數／過度告警率 | L3 以上漏報 0／過度告警 ≤20% |
| 軌道 | 事件期間 24h 沿軌向誤差縮減（真值＝MEME 星曆） | ≥30%（相對未修正） |
| 密度 | `ρ_obs/ρ_MSIS` 比值分佈 | 對照京大 tomography/SWARM 之 0.6–1.2、均值 0.95 區間 |
| 介接 | API 契約穩定性、SDA 匯入成功率 | 破壞性變更 0 次／匯入成功率 100% |
| 作業 | 事件卡自事件起算發布時間 | L3 以上 ≤30 分鐘 |

---

## 15. 風險與對策

| 風險 | 影響 | 對策 |
|---|---|---|
| 跨案程式碼分歧 | 兩案各自演進，物理模組出現兩份不同實作 | 複製時記錄來源版本（`packages/SOURCE_MAP.md`），物理模組整理成 `packages/orbit_drag` 單一權威版本，修正回饋原案 |
| 電離層／區域磁力計資料取得（C2/C3） | 議題二、五無法成立 | 117 Q1 即啟動 TASA／中央氣象署協調；備案為「先以全球 GIM 與鄰近站建模並標示適用範圍」 |
| 多頻段實測通聯紀錄不可得 | 議題五只能停在文獻矩陣 | 構想書 TRL 表已自承此點；先以歷史與文獻模型驗證，並把「排除因素查核表」做實 |
| 6h 預報本質不確定性 | KPI 不易達標 | 以機率輸出＋相對基線技巧提升為主要指標，不承諾絕對命中率；先做「LEO 拖曳風險」單一目標再擴張 |
| DuckDB 單寫入者 | 擷取與查詢互卡 | P9 讀寫分離：collector 只寫 Parquet；服務唯讀連線；重建走離線批次 |
| 14 GB 主庫跨資安區搬運 | 部署困難 | 複用 slim 庫模式（B5） |
| 外部資料源異動／中斷 | 擷取失效 | 每項至少一備援源、原始落地可重解析、degraded mode 明示 |
| 事件樣本不足（太陽活動週期下行） | ML 訓練受限 | 納入 2015／2017／2022／2024 事件；優先統計模型；機率輸出取代單點值 |
| 過度告警造成使用者疲乏 | 系統被棄用 | 遲滯＋駐留時間＋門檻 what-if 工具（B1）＋L3 以上人工確認 |
| 子計畫成果拼裝失敗 | 118 年整合展示跳票 | 契約先行（117 Q1 定 v1）、117 Q4 端到端貫通演練 |
| 綁定 STK 單一工具 | 後續平臺移轉困難 | 階段 C 交付格式規範＋修正因子表，並以 sgp4/dsgp4 交叉驗證 |

---

## 16. 程式庫結構（已實作）

```
SpaceWeather/
├── docs/architecture.md      本文件
├── configs/                  設定即契約
│   ├── params.yaml             參數字典（§6.3）：UI 標籤、API 說明、品質值域皆由此生成
│   ├── sources.yaml            資料源盤點（§5.2）：議題一交付物的機器可讀版
│   └── rules/*.yaml            L0–L4 分級門檻（§9.1）：orbit_prediction / hf_comm / gnss_pnt
├── packages/
│   ├── swx_core/               schema（雙時間軸契約）、params、config、quality、
│   │                           store（Parquet+DuckDB）、cssi（CSSI 格式唯一實作）、flare
│   ├── orbit_drag/             熱氣層密度與大氣阻力（自 A7 移入）
│   └── SOURCE_MAP.md           逐檔記錄移入來源與改動（維護追溯用）
├── services/
│   ├── ingest/                 base（統一契約）、celestrak_sw、gfz_nowcast、
│   │                           swpc_json、swpc_solar、run（排程進入點）
│   ├── risk_engine/            engine（規則引擎）、eventcard（事件卡＋SQLite 作業狀態庫）
│   ├── exporter/               stk_spaceweather（CSSI 驅動檔）、drag_correction
│   └── api/                    app（Flask REST）
├── tools/
│   ├── e2e_demo.py             端到端鏈路演練（§17 第 3 項）
│   └── whatif_threshold.py     門檻校準模擬（§9.2）
├── tests/                      契約測試＋規則引擎測試（37 項）
└── data/                       執行時產生（gitignored）
    ├── swx_parquet/{param}/{期間}/    觀測分區（cadence ≥1h → 年分區，否則月分區）
    ├── raw/{source}/{yyyy}/{mm}/{dd}/ 原始落地
    ├── seed/                          離線種子
    ├── exports/                       STK 檔、修正因子、事件卡
    └── swx_ops.sqlite                 事件卡與稽核紀錄
```

> **未採跨目錄 import 的理由**（純技術）：兩案會各自演進，live import 會讓本案被
> 原案的重構隨時打斷，且原案根目錄有 200 餘支腳本與 14 GB 資料，相依面過大。
> 改為移入並整理成套件，來源以 `packages/SOURCE_MAP.md` 記錄。

---

## 16.1 實作現況（2026-08-24 更新）

> 計數類數字（來源數、規則數、端點數、頁數）以 README 為準——那裡有
> `tests/test_readme_consistency.py` 對照設定檔自動檢查，本節則否。

| 層 | 狀態 | 已驗證的事實 |
|---|---|---|
| 擷取層 | ✅ 22 個來源、19 個可運作 | CelesTrak CSSI、GFZ nowcast、SWPC（X 射線／閃焰事件／積分質子／RTSW 磁場／RTSW 太陽風／估計 Kp／太陽活動區／3 日預報／27 日展望／D-RAP）、Kyoto Dst、NASA OMNI2、GFZ Hp30、SWPC OVATION、中央氣象署 SWOO、福衛七號 TACC ×2；另 3 個（在地 GNSS TEC/ROTI/S4、磁力計、電離層探測）為 planned，待外部協調 |
| 資料層 | ✅ | 雙時間軸查詢、變更偵測（重抓整份檔案只寫入實際變動的列）、品質三級制、cadence 感知分區 |
| 模型層 | ✅ 熱氣層＋地磁基準場；◐ 電離層（D 層吸收已接）；⛔ 區域地磁擾動（待在地實測） | 2024-05 Gannon 400 km storm_ratio 2.56×（暴時 ap 模式）；IGRF-14 臺灣 F≈45,007 nT／I≈35.1°，與實測相符 |
| 預報層 | ✅ Kp 3–48h＋**Hp30 1／3／6h**（1 小時產品建在 30 分鐘格點上） | 滾動起報回測；Kp 3–24h 勝基線 2–8%，48h 氣候平均勝出（Tier 0 門檻擋下）；Hp30 1h 為唯一 BSS 明顯為正者（0.475）；四項 KPI（命中率／誤警率／**提前量**／可信度）皆已可算 |
| 風險層 | ✅ 3 網域 19 條規則（另 3 個已宣告網域尚無規則，於 nowcast 標為「尚無判據」） | Gannon 事件正確產生 L4（Kp 9.0、Ap 271）；駐留與遲滯行為有測試涵蓋 |
| 產品層 | ✅ | CSSI 匯出對 CelesTrak 實檔**2,278/2,279 行一致**（唯一差異為當日仍在更新的觀測列；排除當日後 2,054/2,054 完全一致，含區段配置）；密度修正因子表 |
| 展示層 | ✅ API＋儀表板 | 14 個 API 端點（含 `/v1/forecast`，預報值與該 horizon 的實測技巧同列）；Streamlit 14 頁，全數以 AppTest 驗證無例外 |

**已實測的關鍵數字**：

- CSSI 格式讀寫可逆性：2,278/2,279 行 byte-identical（含 OBSERVED 2054、DAILY_PREDICTED 45、MONTHLY_PREDICTED 180 三段配置）。唯一不一致者為快照當日的觀測列——來源在快照後又修訂了該日 Kp，非格式缺陷；排除當日更新列後為 2,054/2,054。以 `tools/cssi_compare.py` 可複核
- 2024-05-11 Gannon 峰值：Kp 9.0、Ap 271，事件卡判為 L4／G4
- 400 km 熱氣層密度：4.6e-12 → 1.09e-11 kg/m³（storm_ratio 2.56×，暴時 ap 模式）
- 密度修正倍率峰值：300–400 km 2.24×／400–500 km 2.90×／500–600 km 3.67×
- 門檻校準（5.7 年回放）：Kp≥6 → 每年 10.3 次 L3；Kp≥7 → 每年 4.4 次

**實作過程中修正的三個設計錯誤**（記錄下來，避免日後重犯）：

1. **多參數規則不可共用純量遲滯門檻**。Kp（0–9）與 Ap（0–400）量級差兩個數量級，
   混在一起取極值會讓解除條件永不成立，Gannon 事件段一度延長為 432 小時。
   改為每參數各自的解除門檻。
2. **突波門檻不可用固定「每小時」表示**。同一個數字對 1 分鐘級與 1 日級參數的意義
   差三個數量級，日尺度參數等於沒檢查。改以「每個名目更新週期」表示。
3. **回填資料的 ingest_time 不能標成「現在」**。否則 as_of 回放到 2024 年會查不到
   任何東西——語意上正確（當年確實沒有這筆資料），但議題七的歷史回放就無從進行。
   已加入 `--backfill` 模式，以各來源的 `publication_lag_s` 重建當時可取得性。
   此為近似，須在驗證報告中載明。

4. **MSIS 預設模式只讀日均 Ap**。原案把日均 Ap 填滿 7 元素 aps 陣列，
   但該陣列的第 1–6 元素只在 `geomagnetic_activity=-1` 時生效，等於 3 小時
   解析度從未進入模型。更嚴重的是它構成**物理層的前視洩漏**——日均 Ap 含入
   當天稍晚才發生的暴，用於當天稍早的密度計算即為偷看未來。已改用暴時 ap 模式，
   暴起始與恢復期密度差異最大 53%，暴前虛高修正約 35%。
   連帶必須注意：平靜基準也要一併替換 ap 歷史，否則比值恆為 1 且不會報錯。

5. **低頻指數被逐時重複寫入，會在下游造成「最後一筆勝出」的靜默覆蓋**。
   OMNI 把 F10.7（日指數）複製到每個小時，`from_observations` 對同一天逐列寫入時，
   權威來源（tier 1，記於 00:00）的值被低階來源的重複值覆蓋。
   **不會報錯**，只是匯出給 STK 的檔案悄悄變錯——CSSI 一致行數從 2,279 掉到 252。
   修法有二：擷取端只保留來源的原生格點；組表端限定日尺度欄位只採 00:00 的列，
   並讓 tier 最小者最後寫入。

6. **Kp 的量化是三分位，不是十分位**。CSSI 檔存的是 round(Kp×10)，1/3 → 3；
   若直接除以 10 會得到 0.3 而非 0.333，KpSum 會系統性偏低。且此還原**只適用觀測值**，
   預測段的 Kp 不在三分位格點上（實檔可見 22、24），強行套用會把 24 變成 23。

## 17. 最先要做的五件事（117 年前兩個月）

1. **凍結資料契約 v1**：`param_registry`、`swx_observation`、事件卡 JSON Schema。三個子計畫簽字確認。
2. **移入既有資產並跑通回歸**：`atmospheric_drag.py`＋`run_drag_residual.py`＋`space_weather_ap.csv` 在新環境重現既有輸出，作為複用成功的驗收。
3. **打通端到端最小鏈路**：CelesTrak SW → DuckDB → 1 條規則 → 1 張事件卡 → STK 匯入檔。因 A3/A7/A13 已具備，此鏈路可在數週內見到成果。
4. **補齊回放事件的缺漏通道**：選定 5 個事件（含 2022-02 Starlink 再入、2024-05 Gannon G5）。TLE 與 F10.7/Ap/Kp 已在既有庫內，只需補 C1 各通道（Dst/IMF/太陽風/X-ray/粒子）之歷史——**這是唯一無法事後補救的部分**，部分近即時來源不保留歷史。
5. **與需求單位辦第一次門檻校準工作坊**：用 2021 年起的既有 Ap/Kp 資料展示「這組門檻在過去五年會發幾次警報」，及早收斂 L0–L4 定義。

---

## 附錄 A：與構想書研究議題之對應（含複用狀態）

| 議題 | 架構落點 | 複用狀態 |
|---|---|---|
| 一 多源資料整合與品控 | §5 L0、§6 L1 | 部分具備（F10.7/Ap/Kp/黑子）；其餘通道新建 C1/C2 |
| 二 地磁模型與區域擾動 | §7 `model_geomag`、`packages/geomag` | ◐ **基準場（IGRF-14）已完成**；區域擾動需在地磁力計實測（C3） |
| 三 6 小時短時預報 | §8 L3 | 引擎新建 C4；**驗證擂台複用 B2** |
| 四 大氣阻力與 STK/SDA 介接 | §7.1、§10.3 | **物理與反演已具備 A7/A9**；僅 STK 端新建 C7 |
| 五 多頻段訊號影響 | §9.3 | 新建 C5；**軌道預報一列已具備**；HF 分項因 D-RAP 現成可用而可提前動工 |
| 六 分級通報與事件卡 | §9.1、§10.2 | 骨架複用 B1；等級定義與流程新建 C6/C8 |
| 七 歷史回溯與作業驗證 | §6.2 bitemporal、`tools/replay.py`、§14 | **資料層與真值層已具備 A1/A12**；回放平台新建 |

## 附錄 A-1：C2／C3 風險分級之修正（2026-08-17 盤查後）

架構書原將地磁區域模型（C3）與電離層（C2）整批列為「需外部協調、高風險」。
實際盤查（詳見 `docs/data_sources_c2c3.md`）後應拆為三級：

| 級別 | 內容 | 狀態 |
|---|---|---|
| A 現成可用 | Dst、Hp30、OMNI、**D 層吸收 D-RAP**、極光橢圓、IGRF 參考場 | ✅ 全部已介接 |
| B 免費需註冊 | INTERMAGNET（臺灣有 LNP 站）、Madrigal 全球 TEC、COSMIC-2 掩星 | 行政流程，非技術風險 |
| C 需機關協調 | 臺灣在地 GNSS 閃爍實測、磁力計即時串流、CWA 產品、任務單位通聯紀錄 | 真正的高風險項 |

兩個對計畫有利的具體結果：

1. **議題二的「全球地磁場基準模型」已可宣告完成**（IGRF-14，離線可算，
   臺灣 F≈45,007 nT／D≈−4.6°／I≈35.1°，與實測量級相符且有測試涵蓋）。
   尚待補齊的只有需要在地實測的區域擾動部分。
2. **議題五的 HF 分項不必等到第二年**：D-RAP 是公開的全球格網產品，
   直接給出因 D 層吸收而不可用的最高頻率，已介接並在臺灣周邊取樣。

附帶發現，應寫入議題五的矩陣說明：臺灣地理緯度 23.5°N，但**地磁緯度僅約 19°N**，
落在赤道異常駝峰區。以地理緯度判斷電離層現象會系統性失準。

---

## 附錄 B：待與需求單位確認事項

1. SDA 平臺是否已有既定介接標準（欄位、傳輸協定、資安要求）？若有，事件卡 Schema 應對齊而非另創。
2. 內外網分區的實際條件：是否可設 DMZ 擷取區？單向傳輸延遲上限為何？（直接影響告警時效指標）
3. 通報對象與權責單位清單，以及 L3/L4 人工確認流程由誰執行。
4. 是否可提供任務單位實際通聯異常紀錄／衛星操作紀錄，作為多頻段矩陣的實證校準資料。
5. 磁力計與電離層探測站點位置與可用性（影響議題二區域模型的空間解析度）。
