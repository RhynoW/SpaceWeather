"""擷取失敗的判讀：要說出斷在連線的哪一層，而不是給一句籠統的猜測。

2026-10-28 雲端站台的 I95 錯誤是 ConnectTimeout，畫面卻提示「多半是 WAF 或憑證鏈」。
兩者在這裡都可排除——TCP 沒連上就還沒進入 TLS，WAF 則會回 403。
指錯方向的診斷比沒有診斷更糟，這些測試守住判讀與錯誤原文的對應。
"""

from __future__ import annotations

import pytest

from services.ingest.base import diagnose_fetch_failure

# 雲端站台實際回報的錯誤原文（2026-10-28），一字不改
CLOUD_I95 = (
    "RuntimeError: nlsc_egnss_i95 無法取得資料：attempt1: ConnectTimeout: "
    "HTTPSConnectionPool(host='egnss.nlsc.gov.tw', port=443): Max retries exceeded with "
    "url: /rtkstatus.aspx (Caused by ConnectTimeoutError(<HTTPSConnection("
    "host='egnss.nlsc.gov.tw', port=443) at 0x7bf28b9d1e50>, 'Connection to "
    "egnss.nlsc.gov.tw timed out. (connect timeout=30)'))（且無 local_fallback）"
)


def test_cloud_i95_failure_is_diagnosed_as_network_layer():
    kind, hint = diagnose_fetch_failure(CLOUD_I95)
    assert kind == "network"
    # 必須明說「不是憑證」，這正是舊提示指錯的地方
    assert "不是憑證問題" in hint
    assert "SWX_DISABLE_SOURCES" in hint


@pytest.mark.parametrize("detail, kind", [
    ("ConnectionError: HTTPSConnectionPool(...): Max retries exceeded (Caused by "
     "NewConnectionError('...: [Errno 111] Connection refused'))", "network"),
    ("ConnectionError: (Caused by NameResolutionError(\"Failed to resolve "
     "'egnss.nlsc.gov.tw'\"))", "dns"),
    ("SSLError: HTTPSConnectionPool(...): (Caused by SSLError(SSLCertVerificationError("
     "1, '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed')))", "tls"),
    ("HTTPError: 403 Client Error: Forbidden for url: https://example.gov.tw/x", "blocked"),
    ("ReadTimeout: HTTPSConnectionPool(...): Read timed out. (read timeout=30)", "slow"),
    ("ValueError: something nobody anticipated", "unknown"),
])
def test_each_layer_is_recognised(detail, kind):
    assert diagnose_fetch_failure(detail)[0] == kind


def test_tls_handshake_timeout_is_not_mistaken_for_a_slow_server():
    """TLS 交握逾時的訊息也含 timed out，但斷點在憑證／TLS 層，須先比到 tls。"""
    detail = ("SSLError: HTTPSConnectionPool(...): (Caused by SSLError("
              "'The handshake operation timed out'))")
    assert diagnose_fetch_failure(detail)[0] == "tls"


def test_unknown_does_not_guess():
    kind, hint = diagnose_fetch_failure("RuntimeError: 未設定 endpoint")
    assert kind == "unknown"
    for guess in ("WAF", "憑證", "DNS"):
        assert guess not in hint
