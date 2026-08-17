# 移入來源對照表

記錄自 `F:\GitHub\Sat_TraingDataExtension`（上游專案成果，
所有權可複用及保留）移入之模組來源與改動。**用途為維護追溯，非權利聲明**——
兩案會各自演進，此表讓日後能回答「這段程式當初從哪來、改了什麼」。

移入日期：2026-08-17

| 本案位置 | 來源檔 | 對照 | 改動 |
|---|---|---|---|
| `packages/orbit_drag/atmospheric.py` | `atmospheric_drag.py` | A7 | 物理不變（MSIS 2.1 密度、King-Hele 偏心軌道、B_eff 中位數自校準、`is_reentry_decay` 再入守門）。驅動參數改由 `swx_core.SwxStore` 供給，因而支援 `as_of` 回放；新增 `density_ratio()` 產生 storm_ratio 供密度修正因子使用 |
| `packages/swx_core/quality.py` | `data_quality_audit.py` | A10 | 沿用 good/suspect/rejected 三級制與「規則＋成因字串」設計，套用對象由 TLE 換成太空天氣觀測；突波門檻語意改為「每個名目週期」 |
| `services/ingest/gfz_nowcast.py` | `data/space_weather/fetch_space_weather.py` | A5 | 沿用 GFZ 固定寬度解析邏輯。原案只取每日最大 Kp，本案保留逐 3 小時原始值（分級規則需要駐留時間判斷） |
| `services/ingest/celestrak_sw.py`（CSV 版） | `space_weather_ap.csv` | A3 | 該檔即 CelesTrak SW-All 的 CSV 序列化，可直接承接該案 2021→2041 完整歷史 |
| `services/api/app.py` | `backend_duckdb_v2.py` | A13 | 沿用 Flask + `Settings.from_env` + 唯讀資料存取的底座模式 |
| `services/risk_engine/engine.py`（門檻掃描概念） | `fusion_fpr_sweep.py`、`ids_domain_fpr.py` | B1 | 借 FPR 掃描思路，換成任務風險門檻校準（`tools/whatif_threshold.py`） |

## 尚未移入（後續階段）

| 來源檔 | 對照 | 用途 |
|---|---|---|
| `thermosphere_*.py`、`geopotential_energy.py` | A9 | 能量耗散法密度反演（京大 EPS 2026 複現）。接上後可把密度修正因子從「模型輸出」升級為「觀測校正之模型輸出」 |
| `statistical_detectors.py` | B3 | CUSUM／BOCPD／SSA／3σ-MAD，供地磁暴與 TEC 擾動起始偵測 |
| `three_layer_common_eval.py`、`bootstrap_ci.py` | B2 | 預報驗證擂台（同一測試集／GroupKFold OOF／FPR floor 操作點） |
| `conjunction_pipeline.py`、`constellation_anomaly.py` | A11 | 交會分析與星系異常，供 SDA 掛鉤 |
| `backend_duckdb_v2.py` 的 CZML 端點 | A14 | SDA 3D 環境圖層 |
| `ids_truth_set/`、MEME 精密星曆 | A12 | 軌道誤差比較的真值（`tools/orbit_backtest.py` 需要） |
