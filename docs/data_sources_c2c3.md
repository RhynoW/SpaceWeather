# 地磁與電離層資料源盤查（架構書 C2／C3）

> 調查日期：2026-08-17。標「已驗證」者為**實際發出請求確認可取得**，非文獻推斷。
> 對應架構書 §5.2 來源盤點、構想書議題一／二／五。

## 結論先講

架構書把地磁區域模型（C3）與電離層（C2）整批列為「需外部協調、高風險」。
實際盤查後應**拆成三級**，因為其中有相當比例是現成可用的：

| 級別 | 內容 | 對計畫的意義 |
|---|---|---|
| **A 現成可用**（已介接或可立即介接） | Dst、Hp30、OMNI 歷史、**D 層吸收 D-RAP**、極光橢圓、IGRF 參考場、IRI 經驗模式 | 議題二的全球基準與議題五的 HF 分項**不必等外部協調就能動工** |
| **B 免費但需註冊** | INTERMAGNET 觀測站資料、Madrigal 全球 TEC、SuperMAG、COSMIC-2 掩星 | 數週內可取得，屬行政流程而非技術風險 |
| **C 需機關協調** | 臺灣本地磁力計即時串流、CWA 太空天氣作業辦公室產品、地基 GNSS 網原始觀測、任務單位通聯紀錄 | 真正的高風險項，應集中資源在此 |

**最大的單一發現**：SWPC 的 **D-RAP（D-Region Absorption Prediction）** 是公開的全球格網產品，
直接給出「因 D 層吸收而不可用的最高頻率」。這正是議題五 HF 分項所需的核心量，
且**不需要任何外部協調**。本專案已完成介接（來源 `swpc_drap`，含臺灣周邊取樣）。

---

## A. 現成可用

| 來源 | 提供 | 取用方式 | 狀態 |
|---|---|---|---|
| **NOAA SWPC D-RAP** | 全球「最高受影響頻率」格網（MHz），5 分鐘更新 | `services.swpc.noaa.gov/text/drap_global_frequencies.txt` | ✅ 已驗證並介接（`swpc_drap`），取全球最大值與臺灣格點 |
| **Kyoto WDC Dst** | 逐時 Dst 即時值 | `wdc.kugi.kyoto-u.ac.jp/dst_realtime/`（euc-jp HTML） | ✅ 已驗證並介接（`kyoto_dst`） |
| **NASA OMNI2** | 逐時 IMF/太陽風/Kp/Dst/ap/F10.7/AE，1963 年起 | `spdf.gsfc.nasa.gov/pub/data/omni/low_res_omni/omni2_{year}.dat` | ✅ 已驗證並介接（`omni2_hourly`）。**預報引擎唯一可用的長期訓練資料** |
| **GFZ Hp30／ap30** | 30 分鐘解析度地磁指數（1985 年起） | `www-app3.gfz-potsdam.de/kp_index/Hp30_ap30_complete_series.txt` | ✅ **已介接**（`gfz_hp30`，近 120 天）。Kp 的 3 小時解析度會糊掉暴起始時刻 1–2 小時 |
| **SWPC 3 日地磁預報** | 逐 3 小時 Kp 預報，72 小時 | `services.swpc.noaa.gov/text/3-day-geomag-forecast.txt` | ✅ 已介接（`swpc_geomag_forecast`），作為預報引擎的**對照基準** |
| **SWPC 27 日展望** | 逐日 F10.7／Ap／最大 Kp | `services.swpc.noaa.gov/text/27-day-outlook.txt` | ✅ 已介接（`swpc_27day_outlook`） |
| **SWPC OVATION 極光** | 極光橢圓機率格網 → 赤道側邊界緯度 | `services.swpc.noaa.gov/json/ovation_aurora_latest.json` | ✅ **已介接**（`swpc_ovation`）。邊界緯度是擾動深入程度的直觀指標 |
| **IGRF-14 / WMM** | 全球地磁參考場 | Python `ppigrf` | ✅ **已安裝並建模**（`packages/geomag`）。議題二基準場**已完成**，離線可算 |
| **IRI-2020** | 電離層經驗模式（foF2、hmF2、TEC 氣候態） | Python `iri2016`／`PyIRI`（pip，需編譯 Fortran） | ⬜ 未安裝。無實測時的可用基準，也是實測品質的比對對象 |

---

## B. 免費但需註冊

| 來源 | 提供 | 門檻 | 備註 |
|---|---|---|---|
| **INTERMAGNET** | 全球地磁觀測站 1 秒／1 分鐘資料 | 註冊＋引用規範；資料經 GIN（巴黎／愛丁堡／Golden／京都）分發，72 小時內上傳 | **臺灣有 Lunping（LNP）觀測站**（約 25.0°N, 121.17°E，1965 年設站），由中央氣象署運作。若能取得 LNP 即時串流，議題二的區域擾動指標即有在地實測基礎 |
| **Madrigal / CEDAR** | 全球 GNSS TEC 格網、非同調散射雷達、電離層剖面 | 免費註冊（姓名／email／單位），有 `madrigalWeb` Python API | ✅ 站點已驗證可達。是取得全球 TEC 最省事的合法途徑 |
| **SuperMAG** | 全球磁力計整合、SME/SML 指數 | 免費註冊 | 提供比 Kp 更細緻的亞暴指標 |
| **COSMIC-2（福衛七號）** | GNSS 掩星電子密度剖面（ionPrf）、podTec | CDAAC 開放瀏覽 | ✅ 已驗證 `data.cosmic.ucar.edu/gnss-ro/cosmic2/` 可直接列目錄。**這是構想書點名的福衛七號資料**，取得門檻遠低於預期 |
| **IGS GIM（IONEX）** | 全球 TEC 地圖 | CDDIS 需 NASA Earthdata 帳號（已驗證會轉向登入）；UPC-IonSAT、ESOC 等分析中心另有管道 | ✅ UPC 站點可達。建議先用 Madrigal 或 UPC，避免帳號相依 |

---

## C. 需機關協調（真正的高風險項）

| 項目 | 為何無法自取 | 建議對口 |
|---|---|---|
| 臺灣本地磁力計**即時**串流 | INTERMAGNET 為 72 小時內上傳，作業級即時資料需直接介接測站 | 中央氣象署 |
| CWA 太空天氣作業辦公室產品 | 國內作業級預報與警報，非公開 API | 中央氣象署 |
| 臺灣地基 GNSS 網原始觀測（TEC/ROTI/S4） | 需原始 RINEX 或測站級閃爍指數，非全球格網可替代 | 中央氣象署／內政部國土測繪中心／中大太空系 |
| 福衛七號在地加值產品 | CDAAC 為原始與標準產品，在地反演與驗證需 TASA 支援 | TASA |
| 任務單位實際通聯／干擾紀錄 | 議題五矩陣的實證校準資料 | 需求單位 |

---

## 進度與下一步

**已完成**（2026-08-17）：

1. ✅ **GFZ Hp30 已介接**（`gfz_hp30`）。30 分鐘解析度，直接改善「提前量」KPI 的可量測性。
2. ✅ **SWPC OVATION 已介接**（`swpc_ovation`）。輸出極光橢圓赤道側邊界緯度。
3. ✅ **IGRF-14 基準場已建立**（`packages/geomag`）。臺灣代表點 F≈45,007 nT、
   D≈−4.6°、I≈35.1°，與實測量級相符（測試涵蓋）。
   **議題二的「全球地磁場基準模型」可宣告完成**，剩下的是需要在地實測的區域擾動部分。
   已提供 `regional_disturbance()`（實測路徑）與 `regional_disturbance_proxy()`
   （推估路徑，一律標 `is_proxy=True`）兩條路，磁力計資料到位即可切換。

   附帶發現：臺灣地理緯度 23.5°N，但**地磁緯度僅約 19°N**，正落在赤道異常駝峰區。
   用地理緯度判斷電離層現象會系統性失準——這點應寫進議題五的矩陣說明。

**接續建議**：

4. **申請 Madrigal 帳號並介接全球 TEC**（1 週，含行政）。可讓 GNSS_PNT 網域的
   `GNSS-L2-TEC` 規則從 `unavailable` 變成可判定——雖然是全球格網而非在地實測，
   但足以支撐初版，且能明確標示其空間解析度限制。
5. **試接 COSMIC-2 ionPrf**（1–2 週）。取得臺灣周邊掩星剖面，反演 foF2／hmF2，
   這是議題一「福衛相關產品」的實質內容。
6. **同步啟動 C 級協調**。上述 1–5 可在協調進行中並行推進，
   讓計畫不會卡在單一外部相依上。

---

## 對架構書的修正建議

架構書 §2.3 把 C2（電離層）整批列為「零基礎，且需 TASA／中央氣象署協調資料」。
依本次盤查，應修正為：

> C2 電離層：**D 層吸收（HF 相關）已有公開產品可用，已介接**；
> 全球 TEC 與掩星剖面屬「免費但需註冊」，非高風險；
> 真正需要協調的是**臺灣在地**的 GNSS 閃爍實測與磁力計即時串流。

這個修正對計畫是有利的：它把「高風險」的範圍縮小到確實需要協調的部分，
其餘可立即動工，議題五的 HF 分項也不必等到第二年。
