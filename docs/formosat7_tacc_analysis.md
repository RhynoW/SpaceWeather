# 福衛七號 TACC 資料分析（TDPC 與 TROPS）

> 分析日期：2026-08-18。對象：[TACC 資料下載](https://tacc.cwa.gov.tw/v2/download.html)
> 與 [TROPS 下載](https://tacc.cwa.gov.tw/v2/trops_download.html)。
>
> 所有「已驗證」項目均為**實際發出 HTTP 請求並下載檔案確認**，非由網頁文字推斷。

## 結論先講

**`scn1c2`（S4 閃爍指數）可直接匿名下載，且更新到昨天。**

本專案 `GNSS_PNT` 網域的 `GNSS-L3-SCINT` 規則（`S4 ≥ 0.6` 或 `ROTI ≥ 0.8`）
目前回報 `unavailable`——因為沒有 S4 資料源。我先前在 CDAAC 逐層瀏覽
`cosmic1/repro2021` 與 `cosmic2/provisional` 都找不到 `scnLv1`，
因此在 `research_review.md` 中寫「**在確認取得管道前，不可宣稱 S4 已有資料來源**」。

**TACC 就是那個管道。** 這是本次分析唯一真正改變專案能力邊界的發現。

---

## 一、實測的目錄結構

`https://tacc.cwa.gov.tw/data-service/` 可直接瀏覽，無需帳號：

```
data-service/
  fs3_cosmic/  fs3_cosmic2013/          福衛三號（歷史）
  fs7_provisional/                      福衛七號 provisional
  fs7rt_tdpc/                           福衛七號 TDPC 即時
  fs7rt_trops/                          福衛七號 TROPS 即時
  vip/
```

### 各層實際內容〔已驗證〕

| 路徑 | 產品 |
|---|---|
| `fs7rt_tdpc/level1b` | conPhs、leoOrb、podTc2、**scn1c2**、scnPhs |
| `fs7rt_tdpc/level2` | EDP、atmPrf、avnPrf、bfrPrf、echPrf、ivmL2m、**scnLv2**、sktPlt、wetPf2 |
| `fs7rt_trops/level1b` | ionPhs、leoOrb、podTc2、**scn1c2** |
| `fs7rt_trops/level2` | **ionPrf**、igaPrf |
| `fs7rt_trops/level3` | **GIS** |

目錄以 `YYYY.DDD`（年.年積日）分日。

### 與提供的清單不一致之處

以下差異須留意，**以實際目錄為準**：

| 清單所述 | 實際目錄 | 說明 |
|---|---|---|
| `scnLv1` | `scn1c2`（level1b）、`scnLv2`（level2） | 未見 `scnLv1`；S4 實際在 `scn1c2` |
| TROPS 有 `atmPhs`／`wetPrf`／`bfrPrf`／`atmPrf` | `fs7rt_trops/level2` **只有** `ionPrf`、`igaPrf` | TROPS 即時目錄目前**僅供應電離層產品**，大氣產品在 TDPC 側 |
| TROPS 有 `GIS` | 在 `level3/GIS`，非 level2 | 路徑不同 |
| TDPC 有 `goxBin`（GOX/IGOR） | 未見；level0 另有其他項 | GOX/IGOR 為福衛三號接收機，TDPC 側未見此產品 |

---

## 二、時間涵蓋與更新〔已驗證〕

| 產品 | 起始 | 最新 | 落後 |
|---|---|---|---|
| `fs7rt_trops/level1b/scn1c2` | 2019.197 | **2026.229** | 約 1 天 |
| `fs7rt_trops/level2/ionPrf` | 2019.197 | **2026.229** | 約 1 天 |
| `fs7rt_trops/level2/igaPrf` | 2020.046 | 2026.229 | 約 1 天 |
| `fs7rt_tdpc/level1b/scn1c2` | 2022.075 | 2026.229 | 約 1 天 |
| `fs7rt_trops/level3/GIS` | — | 2026.210 | **約 19 天** |

**TROPS 的 S4 有七年連續紀錄且更新到昨天。** 這同時滿足兩種需求：
即時判定（規則引擎）與歷史回測（驗證擂台）。

**GIS 落後約 19 天，不可用於即時判定**，只適合事後分析與模式比對。

---

## 三、`scn1c2` 的內容〔已下載驗證〕

格式為 **netCDF classic**（magic `CDF\x01`），單檔約 7 KB。
檔名 `scn1c2_YYYY.DDD.HHH.SS.SS.Gnn.OS001_....nc`，
一個檔＝一次掩星事件 × 一顆 GNSS 衛星。

實測變數：

| 類別 | 變數 |
|---|---|
| 振幅閃爍序列 | `s4_L1`、`s4_L2` |
| 統計量 | `s4max_L1/L2`、`s4min_L1/L2` |
| **峰值定位** | `lat_s4max_L1/L2`、`lon_s4max_L1/L2`、`alt_s4max_L1/L2`、`lct_s4max_L1/L2`（地方時） |
| 相位閃爍 | `sigma_phi_L1/L2`、`sigmaphimax`、`sigmaphimin` |
| 幾何 | `elev_s4max`、`alt_start/stop`、`lat_start/stop`、`lon_start/stop` |
| 時間 | `start_time`、`stop_time`、`timeOfProcessing` |

**有峰值的經緯度、高度與地方時**，這是能否做區域判定的關鍵——
沒有定位資訊的 S4 無法回答「臺灣上空現在有沒有閃爍」。

`lct`（地方時）欄位特別有用：文獻指出赤道異常區的閃爍好發於**日落後數小時**，
有地方時就能直接驗證這個氣候態，不必自行換算。

---

## 四、資料量與取用策略

單日檔案數實測：

| 日期 | TROPS scn1c2 檔案數 |
|---|---|
| 2026.220 | 8,591 |
| 2026.225 | 10,615 |
| 2026.228 | 9,741 |
| 2026.229 | 9,381 |

約 **9,000–10,600 檔／日 × 7 KB ≈ 65–75 MB／日**。TDPC 側另有相近數量。

**不能整批下載。** 建議策略：

1. **依檔名先篩**——檔名含年積日與時分，可先鎖定時間窗。
2. **下載後依 `lat_s4max`／`lon_s4max` 篩臺灣周邊**（例如 18–28°N、116–126°E），
   只保留落在範圍內的事件。實際落在該框內的比例需實測，預期為個位數百分比。
3. **入庫時保留 `lct` 與 `alt_s4max`**，供後續建立在地氣候態。
4. 歷史回測另行批次處理，不與即時通道共用排程。

福衛七號的**軌道傾角約 24°**，掩星事件密集分布於低緯——
對臺灣（地磁緯度約 19°N、位於赤道異常影響範圍）而言，
這是覆蓋率最好的一組資料，而非最差。

---

## 五、對本專案的意義

### 可直接填補的缺口

| TACC 產品 | 對應參數 | 目前狀態 | 接上後 |
|---|---|---|---|
| `scn1c2` | **`S4`** | 無資料源，`GNSS-L3-SCINT` 回報 `unavailable` | **規則可判定** |
| `ionPrf` | **`FOF2`** | `planned` | 由電子密度剖線反演 F2 層臨界頻率 |
| `podTc2` | `TEC` | 已有 CWA 的 TWTEC（單一代表值） | 補上**衛星間射線 TEC** 的空間分布 |
| `ivmL2m` | 新增 | — | 電漿飄移速度與密度（原位量測） |
| `GIS` | 新增 | — | 三維電子密度分布（同化產品，落後 19 天） |

### 必須誠實載明的限制

**掩星幾何 ≠ 地面測站。** `scn1c2` 的 S4 是沿**臨邊射線**在切點附近取得，
不是某個固定測站正上方的垂直觀測。兩者的取樣體積、時間解析度與
代表性都不同，**不能直接與地面 GNSS 的 S4 混用或互相驗證**。
CWA SWOO 的 S4 來自逾 100 個地面站，那是另一種量。
入庫時應以不同 `source_id` 區分，並在參數字典註明觀測幾何。

**GIS 是同化產品，不是觀測。** 檔名 `GIS_Ne_IRI_RO_GPS_*` 顯示它同化了
IRI 背景模式、掩星（RO）與地面 GPS。依本專案的 `inference` 契約，
由它推得的結論應標 `modelled` 而非 `observed`。

**使用條款須先確認。** 各產品目錄下有 Release Memorandum（PDF），
例如 `F7C2_SpWx_DataRelease_5.pdf`、`F7C2_SW_Data_Release3_Memo.pdf`。
**接入前須閱讀並將條款寫進 `configs/sources.yaml` 的 `attribution.terms`**，
與本專案其他來源一致。目前尚未確認是否要求註冊、引用格式或再散布限制。

---

## 五之二、Release Memorandum 的實質內容〔已閱讀〕

已下載並閱讀 `F7C2_SpWx_DataRelease_5.pdf`（2022-03-16，18 頁）。
由 The Aerospace Corporation 的 Cal/Val Lead 提出，UCAR、美國太空軍、
NOAA 與國家太空中心（NSPO）會簽核准。

### 授權與引用

**該文件是技術性的釋出說明，不是資料使用協議。** 通篇未見註冊要求、
引用格式規定或再散布限制；第 5 節僅列出下載連結與格式文件連結
（CDAAC 與 TACC 兩處皆有）。

→ 因此**目前找不到禁止程式化取用的條款**。但這與「已取得明確授權」不同，
建議接入前仍去信 TACC 確認，並將回覆寫入 `configs/sources.yaml` 的
`attribution.terms`——與本案處理 CWA SWOO 的原則一致。
格式文件：`tacc.cwa.gov.tw/v2/en/fileformat.html#scn1c2`。

### 四種閃爍產品的差別（這決定該接哪一個）

| 產品 | 如何產生 | 涵蓋 |
|---|---|---|
| **`scn1c2`** | TGRS **星上**即時運算，10 秒節奏，對每顆追蹤到的 GNSS 衛星 | **連續** |
| `scnLv2` | 地面由高取樣資料重算 S4 **與相位閃爍 σφ**，10 秒滑動窗、1 秒輸出 | **僅星上 S4 超過門檻的事件** |
| `scnPhs` | 高取樣相位與振幅原始資料（GPS 50 Hz／GLONASS 100 Hz） | 同上，僅事件 |
| `podTc2` | 低取樣絕對 TEC、SNR **與 S4** | 連續 |

→ **要做即時規則判定應接 `scn1c2`**（連續覆蓋）。`scnLv2` 精度較高但只在
事件期間存在，**不能用來判斷「現在沒有閃爍」**——沒有檔案代表未觸發門檻，
而不是代表已確認平靜。這正是本專案「沒資料 ≠ 沒事」原則要處理的情況。

### 三個接入前必須處理的技術問題

**1. RFI 汙染會產生假的 S4。** 這是最重要的一項。
memo 明載：地面射頻干擾會扭曲 TGRS 的訊雜比，**產生虛假的 S4 數值**。
Cal/Val 團隊因此提供兩個欄位，皆已含於 `podTc2`／`scnPhs`／`scn1c2`／`scnLv2`：

- **RFI 指數**——與干擾強度成正比
- **RFI 品質旗標**——當多條天頂軌與至少一條掩星軌同時出現 S4 抬升時設定

memo 原話是這個旗標「可用於從電離層閃爍研究中排除受 RFI 汙染的資料」。
**不套用這個旗標就直接入庫，會讓 `GNSS-L3-SCINT` 規則產生誤警。**
入庫時應把 RFI 旗標映射到本案的品質三級制（建議標 `suspect` 而非直接丟棄，
與現行突波處理一致：標記而不刪除，原值保留）。

**2. S4 時間戳曾有 −5 秒的修正。** 早期釋出的 `podTc2` 把 S4 的時間戳
記在 10 秒平均區間的**結尾**，現已改為對應**中點**，因此時間**調整了 −5 秒**。
混用新舊資料時會有 5 秒偏移。對 3 小時解析度的規則無影響，
但做事件起始時刻的精確比對時必須注意。

**3. 2020 年 2–3 月之前的資料未經驗證。** 高取樣閃爍資料的釋出對應
TGRS 飛控軟體 V4.3.4（2020 年 2–3 月上傳）及之後的版本；
在此之前收集的資料 memo 明言「尚未驗證」。
→ **七年紀錄中，2019.197 至 2020 年初的部分不應直接用於建立氣候態基線。**

### 一項可引用的驗證證據

閃爍事件的地理定位以**反向傳播分析**取得，並與 NASA **GOLD** 衛星的
遠紫外影像比對——GOLD 觀測到的電漿空洞與 F7C2 的不規則體定位
呈現高度相關（memo 圖 2）。L1 與 L2 各自求得的定位亦互相一致（圖 3）。

這對本案有實際用處：`GNSS-L3-SCINT` 規則的**定位可信度有第三方驗證支撐**，
不是只有產製者自述。

---

## 六、建議的接入順序

| 順位 | 動作 | 成本 | 理由 |
|---|---|---|---|
| 1 | 去信 TACC 確認取用條款並寫入 `attribution.terms` | 小 | memo 未見限制條款，但「未見禁止」不等於「已獲授權」 |
| 2 | 接 `scn1c2` → `S4`，**必須同時讀入 RFI 品質旗標** | 中 | **唯一能讓 `GNSS-L3-SCINT` 脫離 `unavailable` 的連續資料源**；不套 RFI 旗標會產生誤警 |
| 3 | 以歷史建立在地閃爍氣候態（**排除 2020 年初之前**） | 中 | 驗證擂台要求新模型須勝過氣候態基線；目前該網域連基線都沒有。TGRS V4.3.4 之前的資料未經驗證，不可納入 |
| 4 | 接 `ionPrf` → `FOF2` | 中 | 讓 HF 網域從只靠 D-RAP 擴充到 F 層 |
| 5 | 接 `podTc2` → `TEC`（空間分布） | 中 | 與 CWA 的單點 TWTEC 互補 |
| 6 | 評估 `GIS` 與 `ivmL2m` | 中 | GIS 落後 19 天，僅適合事後分析 |

**第 3 項容易被忽略但很重要**：本專案的驗證擂台要求任何新模型必須勝過
氣候態基線。`GNSS_PNT` 網域目前連基線都建立不了，因為沒有歷史資料。
TROPS 的七年 S4 紀錄同時解決了「即時判定」與「基線建立」兩件事。

---

## 七、與其他來源的關係

| 來源 | S4 的來源 | 幾何 | 涵蓋 | 落後 |
|---|---|---|---|---|
| **TACC `scn1c2`** | 福衛七號 TGRS 星上運算 | 臨邊／切點 | 全球低緯密集 | 約 1 天 |
| **TACC `scnLv2`** | 地面由高取樣相位／振幅重算 | 同上 | **僅超過門檻的事件** | 約 1 天 |
| **CWA SWOO** | 逾 100 個地面 GNSS 站 | 地面→衛星斜路徑 | 臺灣周邊 | 約 1 天 |
| CDAAC `podTc2` | 同 TGRS，**低取樣 S4** | 同上 | 全球 | 約 1 天 |
| CDAAC `scn1c2` | — | — | **公開瀏覽路徑未提供** | — |

**先前認知的修正**：我原先以為 CDAAC 完全沒有 S4 途徑。實際上
`podTc2` 依 Release Memo 5 即含「low rate absolute TEC, **SNR, and S4**」，
且 CDAAC 的 `nrt/level1b` 有 `podTc2`（實測 2026.229 當日存在）。
真正只有 TACC 提供的是**專用的 `scn1c2`／`scnLv2`／`scnPhs`**。
差別在取樣率與是否為專用產品，不是「有或沒有」。

**兩者互補而非重複**：地面站給的是臺灣上空的連續監測，
掩星給的是廣域分布與長期紀錄。若兩者同時入庫，
應以 `source_id` 與觀測幾何區分，**不可在同一條規則中混用門檻**——
地面站的 S4 ≥ 0.6 與掩星的 S4 ≥ 0.6 不是同一件事。

---

## 延伸閱讀

- [data_sources_c2c3.md](data_sources_c2c3.md)　地磁與電離層資料源盤查
- [cwa_swoo_analysis.md](cwa_swoo_analysis.md)　CWA SWOO 架構分析與介接記錄
- [research_review.md](research_review.md)　依公開學術研究的強化檢視（§1-3 記載 S4 管道未確認）
