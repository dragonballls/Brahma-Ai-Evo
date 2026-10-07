from __future__ import annotations

import http.client

import pytest


class _Response:
    def __init__(self, status, body=b"", location=None):
        self.status = status
        self._body = body
        self._location = location

    def getheader(self, name):
        if name.lower() == "location":
            return self._location
        return None

    def read(self, _size):
        body, self._body = self._body, b""
        return body

    def close(self):
        pass


def test_download_public_to_file_require_https_rejects_http_and_nonstandard_port():
    from core.network_safety import download_public_to_file

    with pytest.raises(ValueError, match="absolute HTTP"):
        download_public_to_file(
            "http://example.com/file",
            bytearray(),
            require_https=True,
        )

    with pytest.raises(ValueError, match="port 443"):
        download_public_to_file(
            "https://example.com:8443/file",
            bytearray(),
            require_https=True,
        )


def test_download_public_to_file_require_https_rejects_downgrade_redirect(monkeypatch):
    from core import network_safety

    class FakeConnection:
        def __init__(self, *args, **kwargs):
            self.sock = None

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return _Response(302, location="http://example.com/other")

        def close(self):
            pass

    monkeypatch.setattr(
        network_safety.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(http.client, "HTTPConnection", FakeConnection)

    with pytest.raises(ValueError, match="downgraded"):
        network_safety.download_public_to_file(
            "https://example.com/file",
            bytearray(),
            require_https=True,
            allowed_redirect_hosts={"example.com"},
        )


def test_download_public_to_file_strips_credentials_when_redirect_port_changes(monkeypatch):
    from core import network_safety

    requests = []

    class FakeConnection:
        def __init__(self, host, port, timeout):
            self.host = host
            self.port = port
            self.sock = None

        def request(self, method, path, headers=None):
            requests.append((self.host, self.port, path, dict(headers or {})))

        def getresponse(self):
            if len(requests) == 1:
                return _Response(302, location="http://example.com:8080/other")
            return _Response(200, body=b"ok")

        def close(self):
            pass

    monkeypatch.setattr(
        network_safety.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", kwargs.get("port", 80)))],
    )
    monkeypatch.setattr(http.client, "HTTPConnection", FakeConnection)

    output = bytearray()

    class Output:
        def write(self, chunk):
            output.extend(chunk)
            return len(chunk)

    network_safety.download_public_to_file(
        "http://example.com/start",
        Output(),
        headers={
            "Authorization": "Bearer secret",
            "Proxy-Authorization": "Basic secret",
        },
        allowed_redirect_hosts={"example.com"},
    )

    assert requests[0][3]["Authorization"] == "Bearer secret"
    assert requests[0][3]["Proxy-Authorization"] == "Basic secret"
    assert "Authorization" not in requests[1][3]
    assert "Proxy-Authorization" not in requests[1][3]
    assert bytes(output) == b"ok"


def test_public_download_rejects_multicast_resolution(monkeypatch):
    from core import network_safety
    monkeypatch.setattr(network_safety.socket, "getaddrinfo", lambda *args, **kwargs: [
        (2, 1, 6, "", ("224.0.0.1", kwargs.get("port", 80)))
    ])
    with pytest.raises(ValueError, match="multicast"):
        network_safety.download_public_to_file(
            "https://example.com/file",
            type("Output", (), {"write": lambda self, chunk: len(chunk)})(),
            require_https=True,
        )
