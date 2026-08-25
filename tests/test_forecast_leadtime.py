"""提前量（lead time）與事件段命中率。

構想書把「提前量」與命中率、誤警率、可信度並列為預報 KPI，但提前量的定義
比其他三個更容易各說各話——同一組預報，換一個定義可以從「提前 6 小時」
變成「延遲 6 小時」。所以定義本身要有測試守著，而不是只寫在報告裡。

本檔驗證的定義（見 `verify.episode_lead_time` 的 docstring）：
起報時刻 t 的預報說的是 t+horizon，故提前量 = 事件起始 − 首次命中的起報時刻，
上限即 horizon；若首次命中的目標時刻落在事件起始之後超過 horizon，值為負，
代表**事後才偵測到**，不是提前。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from services.forecast.verify import episode_lead_time, storm_episodes


def _times(n: int, freq: str = "3h") -> pd.DatetimeIndex:
    return pd.date_range("2024-05-01", periods=n, freq=freq, tz="UTC")


def test_alarm_on_onset_gives_full_horizon_lead():
    """告警正好指向事件起始那一刻時，提前量等於 horizon——這是上限。"""
    t = _times(10)
    truth = np.zeros(10, int)
    truth[4:7] = 1                      # 事件段：第 4–6 個目標時刻
    pred = np.zeros(10, int)
    pred[4] = 1                         # 命中事件起始
    ep = episode_lead_time(t, truth, pred, horizon_h=6)
    assert ep.n_episodes == 1 and ep.n_detected == 1
    assert ep.leads_h == [6.0]
    assert ep.recall == 1.0


def test_late_detection_yields_negative_lead():
    """事件開始後才第一次命中，值為負——這不是提前量，報告上不得四捨五入成 0。"""
    t = _times(12)
    truth = np.zeros(12, int)
    truth[4:9] = 1
    pred = np.zeros(12, int)
    pred[8] = 1                         # 事件起始後 12 小時（4 格 × 3h）才命中
    ep = episode_lead_time(t, truth, pred, horizon_h=6)
    assert ep.leads_h == [6.0 - 12.0]


def test_missed_episode_counts_but_contributes_no_lead():
    """漏掉的事件段仍要計入分母。只報命中者的平均提前量會粉飾漏報。"""
    t = _times(12)
    truth = np.zeros(12, int)
    truth[2:4] = 1
    truth[8:10] = 1
    pred = np.zeros(12, int)
    pred[2] = 1                         # 只命中第一段
    ep = episode_lead_time(t, truth, pred, horizon_h=3, merge_gap_h=3.0)
    assert ep.n_episodes == 2
    assert ep.n_detected == 1
    assert len(ep.leads_h) == 1
    assert ep.recall == 0.5


def test_alarm_before_onset_is_not_credited():
    """指向事件起始之前的告警不算命中。

    它在逐點列聯表裡已計為誤報；若又拿來充當提前量，等於用誤報換 KPI。
    """
    t = _times(10)
    truth = np.zeros(10, int)
    truth[5:8] = 1
    pred = np.zeros(10, int)
    pred[3] = 1                         # 目標時刻落在事件之前
    ep = episode_lead_time(t, truth, pred, horizon_h=6)
    assert ep.n_episodes == 1
    assert ep.n_detected == 0
    assert ep.leads_h == []


def test_short_gap_inside_one_storm_stays_one_episode():
    """一場暴中間掉到門檻下一格不該被算成兩場，否則事件數被稀釋。"""
    t = _times(12)
    truth = np.zeros(12, int)
    truth[[3, 4, 6, 7]] = 1             # 第 5 格短暫低於門檻
    spans = storm_episodes(t, truth, merge_gap_h=6.0)
    assert len(spans) == 1
    assert spans[0] == (t[3], t[7])

    # 併距縮到剛好一格：中間 6 小時的空檔就不再併，變成兩場
    apart = storm_episodes(t, truth, merge_gap_h=3.0)
    assert len(apart) == 2


def test_lead_never_exceeds_horizon():
    """提前量的上限就是 horizon——超過代表定義寫錯了。"""
    rng = np.random.default_rng(0)
    t = _times(400)
    truth = (rng.random(400) < 0.15).astype(int)
    pred = (rng.random(400) < 0.30).astype(int)
    for h in (1, 3, 6, 48):
        ep = episode_lead_time(t, truth, pred, horizon_h=h)
        assert all(lead <= h for lead in ep.leads_h)


def test_thirty_minute_grid_resolves_sub_hour_lead():
    """1 小時 horizon 的提前量只有在細格點上才量得出來。

    這正是 Hp30 目標存在的理由：在 3 小時格點上，1 小時等級的提前量
    連一格都不到，量出來的永遠是 0 或 ±3 小時。
    """
    t = _times(20, freq="30min")
    truth = np.zeros(20, int)
    truth[10:14] = 1
    pred = np.zeros(20, int)
    pred[11] = 1                        # 事件起始後 30 分鐘的目標時刻
    ep = episode_lead_time(t, truth, pred, horizon_h=1, merge_gap_h=3.0)
    assert ep.leads_h == [0.5]
