"""Local HTTP forward proxy that pins browser hostnames to validated IPs."""
from __future__ import annotations

import ipaddress
import select
import socket
import socketserver
import threading
from urllib.parse import urlsplit


_MAX_HEADER_BYTES = 64 * 1024
_MAX_RELAY_BUFFER = 64 * 1024


def _is_loopback_host(host: str) -> bool:
    host = str(host or "").rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _PinnedResolver:
    """Resolve each hostname once and retain the selected safe address."""
    def __init__(self):
        self._lock = threading.Lock()
        self._pins: dict[str, str] = {}

    def resolve(self, host: str, port: int, *, allow_loopback: bool) -> str:
        normalized = str(host or "").rstrip(".").lower()
        if not normalized:
            raise ValueError("Proxy request has no destination host.")
        try:
            literal = ipaddress.ip_address(normalized)
        except ValueError:
            literal = None
        if literal is not None:
            if literal.is_loopback:
                if not allow_loopback:
                    raise ValueError("Proxy loopback destination is not allowed.")
                return normalized
            if not literal.is_global:
                raise ValueError("Proxy destination resolves to a non-global address.")
            return normalized

        with self._lock:
            pinned = self._pins.get(normalized)
            if pinned:
                return pinned
            try:
                entries = socket.getaddrinfo(normalized, port, type=socket.SOCK_STREAM)
            except OSError as exc:
                raise ValueError("Proxy destination could not be resolved safely.") from exc
            addresses = []
            local_only = allow_loopback
            for entry in entries:
                try:
                    address = ipaddress.ip_address(entry[4][0])
                except ValueError:
                    continue
                if local_only:
                    if not address.is_loopback:
                        raise ValueError("Proxy local destination resolved outside loopback.")
                    addresses.append(str(address))
                elif not address.is_global:
                    raise ValueError("Proxy destination resolved to a non-global address.")
                else:
                    addresses.append(str(address))
            if not addresses:
                raise ValueError("Proxy destination had no usable global address.")
            pinned = addresses[0]
            self._pins[normalized] = pinned
            return pinned


class _ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _Handler(socketserver.BaseRequestHandler):
    server: _ThreadedServer

    def handle(self) -> None:
        sock = self.request
        sock.settimeout(15)
        try:
            header, initial = self._read_headers(sock)
            if not header:
                return
            line, raw_headers = self._split_header(header)
            parts = line.split(" ", 2)
            if len(parts) != 3:
                self._error(400, "Bad request")
                return
            method, target, version = parts
            method = method.upper()
            headers = self._parse_headers(raw_headers)
            if method == "CONNECT":
                self._connect_tunnel(target, sock, initial)
                return
            self._forward_http(method, target, version, headers, header, initial, sock)
        except Exception:
            try:
                self._error(502, "Proxy connection rejected")
            except Exception:
                pass

    def _read_headers(self, sock: socket.socket) -> tuple[bytes, bytes]:
        data = b""
        while len(data) <= _MAX_HEADER_BYTES:
            chunk = sock.recv(4096)
            if not chunk:
                return b"", b""
            data += chunk
            marker = data.find(b"\r\n\r\n")
            if marker >= 0:
                end = marker + 4
                return data[:end], data[end:]
        raise ValueError("Proxy request headers exceed the safety limit.")

    @staticmethod
    def _split_header(header: bytes) -> tuple[str, bytes]:
        raw = header[:-4]
        line, _, rest = raw.partition(b"\r\n")
        return line.decode("iso-8859-1"), rest

    @staticmethod
    def _parse_headers(raw: bytes) -> list[tuple[str, str]]:
        result = []
        for row in raw.split(b"\r\n"):
            if not row or b":" not in row:
                continue
            key, value = row.split(b":", 1)
            result.append((key.decode("iso-8859-1").strip(), value.decode("iso-8859-1").strip()))
        return result

    def _destination(self, url: str, headers: list[tuple[str, str]], *, scheme: str):
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if scheme == "https" else 80)
        if parsed.username or parsed.password or not host:
            raise ValueError("Proxy destination contains invalid credentials or host.")
        loopback = _is_loopback_host(host)
        ip = self.server.owner._resolver.resolve(host, port, allow_loopback=loopback)
        if scheme == "http" and not loopback:
            raise ValueError("Proxy allows plain HTTP only to loopback destinations.")
        return parsed, host, port, ip

    def _connect_tunnel(self, target: str, sock: socket.socket, initial: bytes) -> None:
        host, sep, port_text = target.rpartition(":")
        if not sep:
            raise ValueError("CONNECT destination must include a port.")
        port = int(port_text)
        scheme = "https"
        parsed, host, _port, ip = self._destination(f"{scheme}://{target}", [], scheme=scheme)
        upstream = socket.create_connection((ip, port), timeout=15)
        try:
            sock.sendall(b"HTTP/1.1 200 Connection Established\r\nProxy-Agent: Brahma-Pinned\r\n\r\n")
            if initial:
                upstream.sendall(initial)
            self._relay(sock, upstream)
        finally:
            upstream.close()

    def _forward_http(self, method, target, version, headers, header_bytes, initial, sock) -> None:
        host_header = next((v for k, v in headers if k.lower() == "host"), "")
        url = target if "://" in target else f"http://{host_header}{target}"
        parsed, host, port, ip = self._destination(url, headers, scheme="http")
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        lines = [f"{method} {path} {version}"]
        for key, value in headers:
            if key.lower() in {"proxy-connection", "proxy-authorization", "connection"}:
                continue
            lines.append(f"{key}: {value}")
        lines.append("Connection: close")
        upstream = socket.create_connection((ip, port), timeout=15)
        try:
            upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1"))
            if initial:
                upstream.sendall(initial)
            self._relay(sock, upstream)
        finally:
            upstream.close()

    @staticmethod
    def _relay(left: socket.socket, right: socket.socket) -> None:
        sockets = [left, right]
        while sockets:
            readable, _, _ = select.select(sockets, [], [], 15)
            if not readable:
                break
            for source in readable:
                dest = right if source is left else left
                data = source.recv(_MAX_RELAY_BUFFER)
                if not data:
                    try:
                        sockets.remove(source)
                    except ValueError:
                        pass
                    try:
                        dest.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                dest.sendall(data)

    def _error(self, status: int, message: str) -> None:
        body = (message + "\n").encode("utf-8")
        self.request.sendall((
            f"HTTP/1.1 {status} Error\r\nContent-Length: {len(body)}\r\n"
            "Connection: close\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        ).encode("ascii") + body)


class PinnedBrowserProxy:
    """Lifecycle-managed loopback HTTP proxy for browser DNS pinning."""
    def __init__(self):
        self._resolver = _PinnedResolver()
        self._server = _ThreadedServer(("127.0.0.1", 0), _Handler)
        self._server.owner = self
        self._thread: threading.Thread | None = None

    @property
    def server_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> str:
        if self._thread and self._thread.is_alive():
            return self.server_url
        self._thread = threading.Thread(target=self._server.serve_forever, name="BrahmaBrowserPinnedProxy", daemon=True)
        self._thread.start()
        return self.server_url

    def close(self) -> None:
        try:
            self._server.shutdown()
        finally:
            self._server.server_close()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._thread = None