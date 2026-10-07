import http.client
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core.browser_pinned_proxy import PinnedBrowserProxy, _PinnedResolver


def test_pinned_resolver_retains_first_safe_ip_against_rebinding(monkeypatch):
    resolver = _PinnedResolver()
    calls = {"count": 0}

    def fake_getaddrinfo(host, port, type=None):
        calls["count"] += 1
        if calls["count"] == 1:
            return [(2, 1, 6, "", ("1.1.1.1", port))]
        return [(2, 1, 6, "", ("192.168.1.8", port))]

    monkeypatch.setattr("core.browser_pinned_proxy.socket.getaddrinfo", fake_getaddrinfo)
    assert resolver.resolve("example.test", 443, allow_loopback=False) == "1.1.1.1"
    assert resolver.resolve("example.test", 443, allow_loopback=False) == "1.1.1.1"
    assert calls["count"] == 1


def test_pinned_resolver_rejects_global_answer_for_local_hostname(monkeypatch):
    resolver = _PinnedResolver()
    monkeypatch.setattr(
        "core.browser_pinned_proxy.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("1.1.1.1", 443))],
    )
    with pytest.raises(ValueError, match="outside loopback"):
        resolver.resolve("service.localhost", 443, allow_loopback=True)


def test_pinned_proxy_forwards_loopback_http_without_exposing_proxy_auth():
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["path"] = self.path
            seen["proxy_auth"] = self.headers.get("Proxy-Authorization")
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    target = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    target_thread = threading.Thread(target=target.serve_forever, daemon=True)
    target_thread.start()
    proxy = PinnedBrowserProxy()
    try:
        proxy.start()
        port = int(proxy.server_url.rsplit(":", 1)[1])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "GET",
            f"http://127.0.0.1:{target.server_address[1]}/probe",
            headers={"Proxy-Authorization": "should-not-forward"},
        )
        response = conn.getresponse()
        assert response.status == 200
        assert response.read() == b"ok"
        assert seen["path"] == "/probe"
        assert seen["proxy_auth"] is None
        conn.close()
    finally:
        proxy.close()
        target.shutdown()
        target.server_close()
        target_thread.join(timeout=2)

def test_connect_tunnel_forwards_initial_and_bidirectional_bytes():
    import socket

    received = []
    ready = threading.Event()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            ready.set()
            data = self.request.recv(5)
            received.append(data)
            self.request.sendall(data)

    target = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    target_thread = threading.Thread(target=target.serve_forever, daemon=True)
    target_thread.start()
    proxy = PinnedBrowserProxy()
    raw = None
    try:
        proxy.start()
        proxy_port = int(proxy.server_url.rsplit(":", 1)[1])
        target_port = int(target.server_address[1])
        raw = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
        raw.sendall(
            f"CONNECT 127.0.0.1:{target_port} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{target_port}\r\n\r\nhello".encode("ascii")
        )
        raw.settimeout(5)
        response = raw.recv(4096)
        assert b"HTTP/1.1 200 Connection Established\r\n" in response
        assert ready.wait(2)
        assert received == [b"hello"]
        if b"hello" not in response:
            response += raw.recv(4096)
        assert b"hello" in response
    finally:
        if raw is not None:
            raw.close()
        proxy.close()
        target.shutdown()
        target.server_close()
        target_thread.join(timeout=2)


def test_duplicate_singleton_request_headers_are_rejected_before_upstream_connect():
    import socket

    handler = object.__new__(_Handler)
    handler.server = type("Server", (), {
        "owner": type("Owner", (), {"_resolver": _PinnedResolver()})()
    })()
    handler._response_committed = False
    handler._upstream_request_started = False

    with pytest.raises(ValueError, match="Duplicate Host header"):
        handler._parse_headers(b"Host: 127.0.0.1\r\nHost: 127.0.0.1\r\n")


def test_absolute_form_target_and_host_are_checked_before_socket_creation(monkeypatch):
    import socket

    handler = object.__new__(_Handler)
    handler.server = type("Server", (), {
        "owner": type("Owner", (), {"_resolver": _PinnedResolver()})()
    })()
    handler._upstream_request_started = False

    def fail_connect(*args, **kwargs):
        raise AssertionError("upstream socket creation occurred before target validation")

    monkeypatch.setattr("core.browser_pinned_proxy.socket.create_connection", fail_connect)
    with pytest.raises(ValueError, match="does not match the Host header"):
        handler._forward_http(
            "GET",
            "http://127.0.0.1:8080/probe",
            "HTTP/1.1",
            [("Host", "127.0.0.1:8081")],
            object(),
            b"",
        )


def test_non_http_absolute_form_is_rejected_before_socket_creation(monkeypatch):
    import socket

    handler = object.__new__(_Handler)
    handler.server = type("Server", (), {
        "owner": type("Owner", (), {"_resolver": _PinnedResolver()})()
    })()

    def fail_connect(*args, **kwargs):
        raise AssertionError("upstream socket creation occurred before scheme validation")

    monkeypatch.setattr("core.browser_pinned_proxy.socket.create_connection", fail_connect)
    with pytest.raises(ValueError, match="Only HTTP absolute-form"):
        handler._forward_http(
            "GET",
            "https://127.0.0.1:8443/probe",
            "HTTP/1.1",
            [("Host", "127.0.0.1:8443")],
            object(),
            b"",
        )


def test_handler_rejects_malformed_header_rows_instead_of_ignoring_them():
    with pytest.raises(ValueError, match="Malformed request header"):
        _Handler._parse_headers(b"Host: 127.0.0.1\r\nBroken-Header\r\n")


def test_upstream_connection_failure_does_not_emit_a_second_response(monkeypatch):
    import socket

    class ClientSocket:
        def __init__(self):
            self.sent = []

        def settimeout(self, _timeout):
            pass

        def recv(self, _size):
            return b""

        def sendall(self, data):
            self.sent.append(data)

    client = ClientSocket()
    monkeypatch.setattr(
        "core.browser_pinned_proxy.socket.create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("connect failed")),
    )
    server = type("Server", (), {
        "owner": type("Owner", (), {"_resolver": _PinnedResolver()})(),
        "register_socket": lambda *_args: None,
        "unregister_socket": lambda *_args: None,
    })()
    handler = object.__new__(_Handler)
    handler.server = server
    handler.request = client
    handler._response_committed = False
    handler._upstream_request_started = False
    handler.handle()
    joined = b"".join(client.sent)
    assert joined.startswith(b"HTTP/1.1 502")
    assert joined.count(b"HTTP/1.1") == 1


def test_absolute_form_remote_http_remains_rejected_before_upstream():
    handler = object.__new__(_Handler)
    handler.server = type("Server", (), {
        "owner": type("Owner", (), {"_resolver": _PinnedResolver()})()
    })()
    with pytest.raises(ValueError, match="plain HTTP only to loopback"):
        handler._forward_http(
            "GET",
            "http://example.test:80/",
            "HTTP/1.1",
            [("Host", "example.test:80")],
            object(),
            b"",
        )
