import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def safe_address(monkeypatch):
    def set_address(raw):
        monkeypatch.setattr(
            __import__("core.network_safety", fromlist=["socket"]).socket,
            "getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", (raw, 80))],
        )
    return set_address

def test_fixed_https_network_helper_rejects_non_global_resolution(monkeypatch):
    from core import network_safety

    monkeypatch.setattr(network_safety.socket, "getaddrinfo", lambda *a, **k: [
        (2, 1, 6, "", ("192.168.1.8", 443))
    ])
    with pytest.raises(ValueError, match="non-global"):
        network_safety.validate_fixed_https_url(
            "https://example.com/data",
            {"example.com"},
        )


def test_fixed_https_network_helper_rejects_credentials_and_scheme():
    from core.network_safety import validate_fixed_https_url

    with pytest.raises(ValueError):
        validate_fixed_https_url("http://example.com/data", {"example.com"})
    with pytest.raises(ValueError):
        validate_fixed_https_url("https://user:pass@example.com/data", {"example.com"})


def test_public_http_transport_pins_connection_to_validated_dns_ip(monkeypatch):
    from core import network_safety
    import http.client

    class Response:
        status = 200
        def read(self, limit):
            assert limit == 65
            return b"ok"
        def close(self):
            pass

    class FakeConnection:
        def __init__(self, host, port, timeout):
            self.host = host
            self.port = port
            self.timeout = timeout
            self.sock = None
        def request(self, method, path, headers=None):
            self.connect()
        def connect(self):
            raise AssertionError("fetch_public_bytes must replace connect")
        def getresponse(self):
            return Response()
        def close(self):
            pass

    calls = []
    def fake_connect(address, timeout):
        calls.append((address, timeout))
        return object()

    monkeypatch.setattr(
        network_safety.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 80))],
    )
    monkeypatch.setattr(network_safety.socket, "create_connection", fake_connect)
    monkeypatch.setattr(http.client, "HTTPConnection", FakeConnection)

    status, body = network_safety.fetch_public_bytes(
        "http://example.com/status",
        timeout=5,
        max_response_bytes=64,
    )
    assert status == 200
    assert body == b"ok"
    assert calls == [(("93.184.216.34", 80), 5)]


def test_public_transport_rejects_non_global_dns_for_all_public_http(safe_address):
    from core.network_safety import fetch_public_bytes
    with pytest.raises(ValueError, match="non-global"):
        safe_address("192.168.1.8")
        fetch_public_bytes("http://example.com/status")


def test_public_transport_preserves_redirect_response_without_following(monkeypatch):
    from core import network_safety
    import http.client

    class Response:
        status = 302
        def read(self, limit):
            return b"redirect-body"
        def close(self):
            pass

    class FakeConnection:
        def __init__(self, *args, **kwargs):
            self.sock = None
        def request(self, *args, **kwargs):
            pass
        def getresponse(self):
            return Response()
        def close(self):
            pass

    monkeypatch.setattr(
        network_safety.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 80))],
    )
    monkeypatch.setattr(http.client, "HTTPConnection", FakeConnection)
    status, body = network_safety.fetch_public_bytes("http://example.com/redirect", max_response_bytes=64)
    assert status == 302
    assert body == b"redirect-body"


def test_fixed_https_rejects_multicast_resolution(monkeypatch):
    from core import network_safety
    monkeypatch.setattr(network_safety.socket, "getaddrinfo", lambda *a, **k: [
        (2, 1, 6, "", ("224.0.0.1", 443))
    ])
    with pytest.raises(ValueError, match="multicast"):
        network_safety.validate_fixed_https_url("https://example.com/data", {"example.com"})


def test_public_http_rejects_multicast_resolution(monkeypatch):
    from core import network_safety
    monkeypatch.setattr(network_safety.socket, "getaddrinfo", lambda *a, **k: [
        (2, 1, 6, "", ("224.0.0.1", 80))
    ])
    with pytest.raises(ValueError, match="multicast"):
        network_safety.fetch_public_bytes("http://example.com/status")


def test_crypto_live_price_rejects_injection_like_coin_ids():
    from features import crypto_live_price
    result = crypto_live_price.execute(coin_id="bitcoin&ids=ethereum")
    assert result.get("error") == "Invalid cryptocurrency identifier."


def test_crypto_live_price_uses_bounded_fixed_https_transport(monkeypatch):
    from features import crypto_live_price

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False

    def fake_open(url, **kwargs):
        assert url.startswith("https://api.coingecko.com/")
        assert kwargs["allowed_hosts"] == {"api.coingecko.com"}
        assert kwargs["timeout"] == 8
        return Response()

    monkeypatch.setattr(crypto_live_price, "open_fixed_https", fake_open)
    monkeypatch.setattr(
        crypto_live_price,
        "read_bounded",
        lambda response, limit: (assert_limit(limit) or b'{"bitcoin":{"usd":123.45}}'),
    )
    result = crypto_live_price.execute(coin_id="bitcoin")
    assert result["price"] == 123.45


def assert_limit(limit):
    assert limit == 128 * 1024
    return None


def test_market_analysis_rejects_unsafe_symbol_and_unbounded_limit():
    from features import market_analysis
    assert market_analysis.execute(symbol="BTCUSDT&redirect=https://evil.example")["error"] == "Invalid trading pair symbol."
    assert "between 1 and 1000" in market_analysis.execute(symbol="BTCUSDT", limit=1001)["error"]


def test_market_analysis_accepts_binance_monthly_interval():
    source = (ROOT / "features" / "market_analysis.py").read_text(encoding="utf-8")
    assert 'interval = raw_interval if raw_interval == "1M" else raw_interval.lower()' in source


def test_iss_tracking_uses_fixed_host_bounded_transport():
    source = (ROOT / "features" / "tracks_international_space_station.py").read_text(encoding="utf-8")
    assert "open_fixed_https(" in source
    assert 'allowed_hosts={"api.wheretheiss.at"}' in source
    assert "read_bounded(response, 128 * 1024)" in source
    assert "requests.get(" not in source
