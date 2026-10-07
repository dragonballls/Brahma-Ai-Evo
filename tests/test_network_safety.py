import pytest


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
