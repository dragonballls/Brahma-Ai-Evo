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