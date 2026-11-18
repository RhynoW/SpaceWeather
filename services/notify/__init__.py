"""services.notify — 事件卡通報（架構書 §11 告警推播、§16.2 G7）。

transactional outbox 模式：

  1. 值勤員發布事件卡（EventStore.issue）時，**同一個 SQLite 交易**依
     configs/notify.yaml 寫入待送訊息（notification_outbox）。
     發布成功即保證有待送訊息；交易失敗則兩者都不存在——不會出現
     「已發布但沒人收到」或「收到了但系統說沒發布」。
  2. 投遞程序（services.notify.dispatch）另外執行，讀待送訊息、送出、
     回寫送達時間；失敗以指數退避重試，超過上限標 failed。
     每次送達與失敗都寫入 append-only 稽核表。

把「發布」與「送達」拆開，兩者才能各自被稽核與計時——
§14「L3 以上 30 分鐘內發布」量的是前者，對方何時收到是後者。
"""
