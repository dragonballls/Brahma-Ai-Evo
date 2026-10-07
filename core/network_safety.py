from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.URLError("HTTP redirects are disabled by Brahma network policy")


def validate_fixed_https_url(url: str, allowed_hosts: Iterable[str]) -> str:
    parsed = urllib.parse.urlsplit(str(url or ""))
    host = (parsed.hostname or "").rstrip(".").lower()
    allowed = {str(item).rstrip(".").lower() for item in allowed_hosts}
    if parsed.scheme.lower() != "https" or host not in allowed:
        raise ValueError("Network destination is outside the fixed HTTPS host policy.")
    if parsed.username or parsed.password:
        raise ValueError("Network URL may not contain embedded credentials.")
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("Network destination could not be resolved safely.") from exc
    if not addresses:
        raise ValueError("Network destination did not resolve.")
    for entry in addresses:
        try:
            address = ipaddress.ip_address(entry[4][0])
        except ValueError as exc:
            raise ValueError("Network destination returned an invalid address.") from exc
        if not address.is_global:
            raise ValueError("Network destination resolved to a non-global address.")
    return url


def open_fixed_https(url: str, *, allowed_hosts: Iterable[str], timeout: float, headers: dict[str, str] | None = None):
    safe_url = validate_fixed_https_url(url, allowed_hosts)
    request = urllib.request.Request(safe_url, headers=headers or {})
    opener = urllib.request.build_opener(NoRedirectHandler())
    return opener.open(request, timeout=timeout)




class _PinnedHTTPConnection:
    def __init__(self, host: str, port: int, ip_address: str, timeout: float):
        self.host = host
        self.port = port
        self.ip_address = ip_address
        self.timeout = timeout
        self.sock = None

    def connect(self) -> None:
        self.sock = socket.create_connection((self.ip_address, self.port), timeout=self.timeout)


class _PinnedHTTPSConnection:
    def __init__(self, host: str, port: int, ip_address: str, timeout: float):
        import http.client
        self.host = host
        self.port = port
        self.ip_address = ip_address
        self.timeout = timeout
        self._conn = http.client.HTTPSConnection(host, port=port, timeout=timeout)

    def request(self, method: str, path: str, headers: dict[str, str] | None = None) -> None:
        import socket as _socket
        self._conn.close()
        raw = _socket.create_connection((self.ip_address, self.port), timeout=self.timeout)
        try:
            self._conn.sock = self._conn._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise
        self._conn.request(method, path, headers=headers or {})

    def getresponse(self):
        return self._conn.getresponse()

    def close(self) -> None:
        self._conn.close()


class _PinnedHTTPWrapper:
    def __init__(self, conn, response):
        self.conn = conn
        self.response = response

    def __enter__(self):
        return self.response

    def __exit__(self, exc_type, exc, tb):
        try:
            self.response.close()
        finally:
            self.conn.close()
        return False


def fetch_public_bytes(
    url: str,
    *,
    timeout: float = 5.0,
    max_response_bytes: int = 64 * 1024,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    """GET a public HTTP(S) URL using one DNS resolution and no redirects."""
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        raise ValueError("Public URL must use an absolute HTTP(S) URL.")
    if parsed.username or parsed.password:
        raise ValueError("Public URL may not contain embedded credentials.")
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        entries = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("Public URL could not be resolved safely.") from exc
    candidates = []
    for entry in entries:
        address = ipaddress.ip_address(entry[4][0])
        if address.is_global and entry[4][0] not in candidates:
            candidates.append(entry[4][0])
    if not candidates:
        raise ValueError("Public URL resolved only to non-global addresses.")

    request_path = parsed.path or "/"
    if parsed.query:
        request_path += "?" + parsed.query
    request_headers = dict(headers or {})
    request_headers.setdefault("Connection", "close")
    request_headers.setdefault("User-Agent", "Brahma-Ai-Evo-monitor/1")
    ip_address = candidates[0]

    if parsed.scheme.lower() == "https":
        conn = _PinnedHTTPSConnection(host, port, ip_address, timeout)
    else:
        import http.client
        conn = http.client.HTTPConnection(host, port=port, timeout=timeout)
        def _pinned_connect() -> None:
            conn.sock = socket.create_connection((ip_address, port), timeout=timeout)
        conn.connect = _pinned_connect

    response = None
    try:
        conn.request("GET", request_path, headers=request_headers)
        response = conn.getresponse()
        raw = response.read(max_response_bytes + 1)
        if len(raw) > max_response_bytes:
            raise ValueError(f"Public URL response exceeded the {max_response_bytes} byte safety limit.")
        return int(response.status), raw
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        conn.close()


def download_public_to_file(
    url: str,
    output,
    *,
    timeout: float = 30.0,
    max_response_bytes: int = 64 * 1024,
    headers: dict[str, str] | None = None,
    allowed_redirect_hosts: Iterable[str] | None = None,
    max_redirects: int = 3,
) -> int:
    """Stream a pinned HTTP(S) response into a caller-owned file object."""
    current = str(url or "").strip()
    redirect_hosts = {str(item).rstrip(".").lower() for item in (allowed_redirect_hosts or ())}
    request_headers = dict(headers or {})
    pins: dict[str, str] = {}

    for redirect_count in range(max_redirects + 1):
        parsed = urllib.parse.urlsplit(current)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").rstrip(".").lower()
        if scheme not in {"http", "https"} or not host:
            raise ValueError("Public download URL must be an absolute HTTP(S) URL.")
        if parsed.username or parsed.password:
            raise ValueError("Public download URL may not contain embedded credentials.")
        port = parsed.port or (443 if scheme == "https" else 80)

        ip_address = pins.get(host)
        if ip_address is None:
            try:
                entries = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            except OSError as exc:
                raise ValueError("Public download host could not be resolved safely.") from exc
            candidates = []
            for entry in entries:
                address = ipaddress.ip_address(entry[4][0])
                if not address.is_global:
                    raise ValueError("Public download host resolved to a non-global address.")
                if entry[4][0] not in candidates:
                    candidates.append(entry[4][0])
            if not candidates:
                raise ValueError("Public download host did not resolve to a global address.")
            ip_address = candidates[0]
            pins[host] = ip_address

        request_path = parsed.path or "/"
        if parsed.query:
            request_path += "?" + parsed.query
        if scheme == "https":
            conn = _PinnedHTTPSConnection(host, port, ip_address, timeout)
        else:
            import http.client
            conn = http.client.HTTPConnection(host, port=port, timeout=timeout)
            def _pinned_connect() -> None:
                conn.sock = socket.create_connection((ip_address, port), timeout=timeout)
            conn.connect = _pinned_connect

        response = None
        try:
            conn.request("GET", request_path, headers=request_headers)
            response = conn.getresponse()
            status = int(response.status)
            if 300 <= status < 400:
                if redirect_count >= max_redirects:
                    raise ValueError("Public download exceeded the redirect limit.")
                location = response.getheader("Location")
                if not location:
                    raise ValueError("Public download redirect omitted a destination.")
                next_url = urllib.parse.urljoin(current, location)
                next_host = (urllib.parse.urlsplit(next_url).hostname or "").rstrip(".").lower()
                if not next_host or next_host not in redirect_hosts:
                    raise ValueError("Public download redirect destination is outside the approved host policy.")
                next_scheme = urllib.parse.urlsplit(next_url).scheme.lower()
                if next_host != host or next_scheme != scheme:
                    request_headers.pop("Proxy-Authorization", None)
                current = next_url
                continue
            if status >= 400:
                raise RuntimeError(f"Public download returned HTTP {status}.")
            declared = response.getheader("Content-Length")
            if declared and int(declared) > max_response_bytes:
                raise ValueError(f"Public download exceeded the {max_response_bytes} byte safety limit.")
            total = 0
            while True:
                chunk = response.read(min(1024 * 1024, max_response_bytes - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_response_bytes:
                    raise ValueError(f"Public download exceeded the {max_response_bytes} byte safety limit.")
                output.write(chunk)
            return total
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            conn.close()


def fetch_public_url_status(
    url: str,
    *,
    timeout: float = 5.0,
    max_response_bytes: int = 64 * 1024,
    headers: dict[str, str] | None = None,
) -> int:
    status, _ = fetch_public_bytes(
        url,
        timeout=timeout,
        max_response_bytes=max_response_bytes,
        headers=headers,
    )
    return status

def read_bounded(response, max_bytes: int) -> bytes:
    raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"Network response exceeded the {max_bytes} byte safety limit.")
    return raw
