import http.client
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core.browser_pinned_proxy import PinnedBrowserProxy, _Handler, _PinnedResolver


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


def test_connect_failure_after_200_does_not_emit_a_second_http_response(monkeypatch):
    class ClientSocket:
        def __init__(self):
            self.sent = []

        def settimeout(self, _timeout):
            pass

        def recv(self, _size):
            request = (
                b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n"
                b"Host: 127.0.0.1:443\r\n\r\n"
            )
            if not self.sent and not getattr(self, "_received", False):
                self._received = True
                return request
            return b""

        def sendall(self, data):
            self.sent.append(data)

    client = ClientSocket()

    class Upstream:
        def sendall(self, _data):
            pass

        def close(self):
            pass

    handler = object.__new__(_Handler)
    handler.request = client
    handler.server = type("Server", (), {
        "owner": type("Owner", (), {"_resolver": _PinnedResolver()})(),
        "register_socket": lambda *_args: None,
        "unregister_socket": lambda *_args: None,
    })()
    handler._response_committed = False
    handler._upstream_request_started = False

    monkeypatch.setattr(
        "core.browser_pinned_proxy.socket.create_connection",
        lambda *args, **kwargs: Upstream(),
    )
    def fail_relay(*args, **kwargs):
        raise OSError("peer reset")
    monkeypatch.setattr(_Handler, "_relay", fail_relay)

    handler.handle()
    joined = b"".join(client.sent)
    assert joined.startswith(b"HTTP/1.1 200 Connection Established")
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


def test_request_header_limit_is_strict():
    class FakeSocket:
        def __init__(self):
            self.remaining = 65536 + 1

        def recv(self, size):
            count = min(size, self.remaining)
            self.remaining -= count
            return b"a" * count

    with pytest.raises(ValueError, match="headers exceed"):
        _Handler._read_headers(object.__new__(_Handler), FakeSocket())


def test_upstream_oversized_response_headers_are_rejected_before_exposure():
    oversized = b"HTTP/1.1 200 OK\r\nX-Fill: " + (b"a" * 65520) + b"\r\n\r\nsecret"
    with pytest.raises(ValueError, match="response headers exceed"):
        _Handler._validate_response_headers(oversized[: oversized.find(b"\r\n\r\n") + 4])


@pytest.mark.parametrize(
    "literal",
    (
        "0.0.0.0",
        "10.0.0.8",
        "100.64.0.8",
        "169.254.1.8",
        "192.168.1.8",
        "224.0.0.1",
        "255.255.255.255",
        "::",
        "fc00::1",
        "fe80::1",
        "ff02::1",
    ),
)
def test_resolver_rejects_non_global_destination_literals(literal):
    resolver = _PinnedResolver()
    with pytest.raises(ValueError):
        resolver.resolve(literal, 443, allow_loopback=False)


def test_resolver_allows_explicit_loopback_literals_only_when_requested():
    resolver = _PinnedResolver()
    assert resolver.resolve("127.0.0.1", 443, allow_loopback=True) == "127.0.0.1"
    assert resolver.resolve("::1", 443, allow_loopback=True) == "::1"


def test_loopback_websocket_upgrade_is_forwarded_safely():
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["upgrade"] = self.headers.get("Upgrade")
            seen["connection"] = self.headers.get("Connection")
            seen["proxy_auth"] = self.headers.get("Proxy-Authorization")
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.end_headers()

        def log_message(self, *_args):
            pass

    target = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    target_thread = threading.Thread(target=target.serve_forever, daemon=True)
    target_thread.start()
    proxy = PinnedBrowserProxy()
    try:
        proxy.start()
        proxy_port = int(proxy.server_url.rsplit(":", 1)[1])
        conn = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5)
        conn.request(
            "GET",
            f"http://127.0.0.1:{target.server_address[1]}/socket",
            headers={
                "Host": f"127.0.0.1:{target.server_address[1]}",
                "Connection": "Upgrade",
                "Upgrade": "websocket",
                "Proxy-Authorization": "not-forwarded",
            },
        )
        response = conn.getresponse()
        assert response.status == 101
        assert seen["upgrade"] == "websocket"
        assert "upgrade" in seen["connection"].lower()
        assert seen["proxy_auth"] is None
        conn.close()
    finally:
        proxy.close()
        target.shutdown()
        target.server_close()
        target_thread.join(timeout=2)


def test_malformed_upstream_status_line_is_not_exposed():
    class Handler(BaseHTTPRequestHandler):
        def handle_one_request(self):
            self.raw_requestline = self.rfile.readline(65537)
            if self.raw_requestline:
                self.wfile.write(b"NOT-HTTP-RESPONSE\r\nX-Bad: yes\r\n\r\nsecret")
                self.wfile.flush()

    target = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    target_thread = threading.Thread(target=target.serve_forever, daemon=True)
    target_thread.start()
    proxy = PinnedBrowserProxy()
    try:
        proxy.start()
        proxy_port = int(proxy.server_url.rsplit(":", 1)[1])
        raw = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
        raw.sendall(
            f"GET http://127.0.0.1:{target.server_address[1]}/ HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{target.server_address[1]}\r\n\r\n".encode("ascii")
        )
        raw.settimeout(5)
        data = raw.recv(4096)
        assert b"NOT-HTTP-RESPONSE" not in data
        raw.close()
    finally:
        proxy.close()
        target.shutdown()
        target.server_close()
        target_thread.join(timeout=2)


def test_close_releases_active_tunnel_sockets():
    connected = threading.Event()
    peer_closed = threading.Event()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            connected.set()
            try:
                while self.request.recv(1024):
                    pass
            except OSError:
                pass
            finally:
                peer_closed.set()

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
            f"Host: 127.0.0.1:{target_port}\r\n\r\n".encode("ascii")
        )
        assert raw.recv(4096).startswith(b"HTTP/1.1 200")
        assert connected.wait(2)
        proxy.close()
        assert peer_closed.wait(2)
    finally:
        if raw is not None:
            raw.close()
        proxy.close()
        target.shutdown()
        target.server_close()
        target_thread.join(timeout=2)
