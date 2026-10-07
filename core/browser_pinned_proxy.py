"""Local HTTP forward proxy that pins browser hostnames to validated IPs."""
from __future__ import annotations

import ipaddress
import re
import select
import socket
import socketserver
import threading
from urllib.parse import urlsplit


_MAX_HEADER_BYTES = 64 * 1024
_MAX_HEADER_LINE_BYTES = 16 * 1024
_MAX_RELAY_BUFFER = 64 * 1024
_HEADER_TOKEN_EXTRA = frozenset("!#$%&'*+-.^_|~") | frozenset({chr(96)})
_STATUS_LINE_RE = re.compile(r"^HTTP/\d\.\d [1-5]\d\d(?: [^\r\n]*)?\r\n$")


def _is_loopback_host(host: str) -> bool:
    host = str(host or "").rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_header_name(value: str) -> bool:
    return bool(value) and all(
        char.isascii() and (char.isalnum() or char in _HEADER_TOKEN_EXTRA)
        for char in value
    )


def _has_forbidden_header_value_controls(value: str) -> bool:
    return any(
        (ord(char) < 0x20 and char != "\t") or ord(char) == 0x7F
        for char in value
    )


def _split_connection_tokens(headers: list[tuple[str, str]]) -> set[str]:
    tokens: set[str] = set()
    for key, value in headers:
        if key.lower() != "connection":
            continue
        for item in value.split(","):
            item = item.strip().lower()
            if item:
                tokens.add(item)
    return tokens


def _normalize_host(host: str) -> str:
    raw = str(host or "").rstrip(".").lower()
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return raw


def _validate_port(port: int) -> int:
    value = int(port)
    if not 1 <= value <= 65535:
        raise ValueError("Proxy destination port is outside the valid TCP range.")
    return value


def _parse_authority(
    value: str,
    *,
    default_port: int | None,
    require_port: bool = False,
) -> tuple[str, int]:
    authority = str(value or "").strip()
    if not authority or any(
        ord(char) < 0x20 or ord(char) == 0x7F or char.isspace()
        for char in authority
    ):
        raise ValueError("Proxy authority contains invalid whitespace or control characters.")
    try:
        parsed = urlsplit(f"http://{authority}")
        if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("Proxy authority contains an unexpected URL component.")
        host = parsed.hostname or ""
        parsed_port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("Proxy authority is malformed.") from exc
    if not host:
        raise ValueError("Proxy authority has no host.")
    if require_port and parsed_port is None:
        raise ValueError("CONNECT destination must include a port.")
    port = parsed_port if parsed_port is not None else default_port
    if port is None:
        raise ValueError("Proxy authority has no usable port.")
    return _normalize_host(host), _validate_port(port)


def _hosts_match(left: str, right: str) -> bool:
    return _normalize_host(left) == _normalize_host(right)


def _format_authority(host: str, port: int, *, default_port: int) -> str:
    normalized = _normalize_host(host)
    try:
        ip = ipaddress.ip_address(normalized)
    except ValueError:
        rendered = normalized
    else:
        rendered = f"[{normalized}]" if ip.version == 6 else normalized
    return rendered if port == default_port else f"{rendered}:{port}"


class _ProxyRequestError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class _ProxyUpstreamError(RuntimeError):
    pass


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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.owner = None
        self._active_lock = threading.Lock()
        self._active_sockets: set[socket.socket] = set()

    def register_socket(self, sock: socket.socket) -> None:
        with self._active_lock:
            self._active_sockets.add(sock)

    def unregister_socket(self, sock: socket.socket) -> None:
        with self._active_lock:
            self._active_sockets.discard(sock)

    def close_active_sockets(self) -> None:
        with self._active_lock:
            sockets = list(self._active_sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass


class _Handler(socketserver.BaseRequestHandler):
    server: _ThreadedServer

    def setup(self) -> None:
        super().setup()
        self._response_committed = False
        self._upstream_request_started = False
        self.server.register_socket(self.request)

    def finish(self) -> None:
        self.server.unregister_socket(self.request)
        super().finish()

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
                raise _ProxyRequestError(400, "Malformed request line.")
            method, target, version = parts
            if not _is_header_name(method):
                raise _ProxyRequestError(400, "Malformed HTTP method.")
            if version not in {"HTTP/1.0", "HTTP/1.1"}:
                raise _ProxyRequestError(400, "Unsupported HTTP version.")

            method = method.upper()
            headers = self._parse_headers(raw_headers)
            if method == "CONNECT":
                self._connect_tunnel(target, headers, sock, initial)
                return
            self._forward_http(method, target, version, headers, sock, initial)
        except _ProxyRequestError as exc:
            if not self._response_committed and not self._upstream_request_started:
                try:
                    self._error(exc.status, exc.message)
                except OSError:
                    pass
        except (_ProxyUpstreamError, socket.timeout, TimeoutError):
            if not self._response_committed and not self._upstream_request_started:
                try:
                    self._error(502, "Upstream connection rejected.")
                except OSError:
                    pass
        except Exception:
            if not self._response_committed and not self._upstream_request_started:
                try:
                    self._error(502, "Proxy connection rejected.")
                except OSError:
                    pass

    def _read_headers(self, sock: socket.socket) -> tuple[bytes, bytes]:
        data = b""
        while True:
            remaining = _MAX_HEADER_BYTES + 1 - len(data)
            if remaining <= 0:
                raise _ProxyRequestError(431, "Request headers exceed the safety limit.")
            chunk = sock.recv(min(4096, remaining))
            if not chunk:
                return b"", b""
            data += chunk
            marker = data.find(b"\r\n\r\n")
            if marker >= 0:
                end = marker + 4
                if end > _MAX_HEADER_BYTES:
                    raise _ProxyRequestError(431, "Request headers exceed the safety limit.")
                return data[:end], data[end:]

    @staticmethod
    def _split_header(header: bytes) -> tuple[str, bytes]:
        raw = header[:-4]
        line, separator, rest = raw.partition(b"\r\n")
        if not separator:
            raise _ProxyRequestError(400, "Malformed request header block.")
        return line.decode("iso-8859-1"), rest

    @staticmethod
    def _parse_headers(raw: bytes) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        singleton = {"host", "content-length", "transfer-encoding"}
        rows = raw.split(b"\r\n")
        if rows and rows[-1] == b"":
            rows.pop()
        seen: set[str] = set()
        for row in rows:
            if not row:
                raise _ProxyRequestError(400, "Malformed request header block.")
            if len(row) > _MAX_HEADER_LINE_BYTES:
                raise _ProxyRequestError(431, "A request header line exceeds the safety limit.")
            if b":" not in row:
                raise _ProxyRequestError(400, "Malformed request header.")
            key_bytes, value_bytes = row.split(b":", 1)
            key = key_bytes.decode("iso-8859-1").strip()
            value = value_bytes.decode("iso-8859-1").strip()
            if not _is_header_name(key):
                raise _ProxyRequestError(400, "Malformed request header name.")
            if _has_forbidden_header_value_controls(value):
                raise _ProxyRequestError(400, "Malformed request header value.")
            lowered = key.lower()
            if lowered in singleton and lowered in seen:
                raise _ProxyRequestError(400, f"Duplicate {key} header is not allowed.")
            seen.add(lowered)
            result.append((key, value))
        return result

    @staticmethod
    def _header_values(headers: list[tuple[str, str]], name: str) -> list[str]:
        return [value for key, value in headers if key.lower() == name.lower()]

    def _resolve_destination(self, host: str, port: int, *, scheme: str) -> tuple[str, int, str]:
        host = _normalize_host(host)
        port = _validate_port(port)
        loopback = _is_loopback_host(host)
        try:
            ip = self.server.owner._resolver.resolve(host, port, allow_loopback=loopback)
        except ValueError as exc:
            raise _ProxyRequestError(400, str(exc)) from exc
        if scheme == "http" and not loopback:
            raise _ProxyRequestError(400, "Proxy allows plain HTTP only to loopback destinations.")
        return host, port, ip

    def _parse_connect_target(self, target: str, headers: list[tuple[str, str]]) -> tuple[str, int, str]:
        try:
            host, port = _parse_authority(target, default_port=None, require_port=True)
        except ValueError as exc:
            raise _ProxyRequestError(400, str(exc)) from exc

        host_values = self._header_values(headers, "Host")
        if len(host_values) > 1:
            raise _ProxyRequestError(400, "CONNECT request contains duplicate Host headers.")
        if host_values:
            try:
                header_host, header_port = _parse_authority(host_values[0], default_port=port)
            except ValueError as exc:
                raise _ProxyRequestError(400, str(exc)) from exc
            if not _hosts_match(host, header_host) or port != header_port:
                raise _ProxyRequestError(400, "CONNECT target does not match the Host header.")

        try:
            ip = self.server.owner._resolver.resolve(
                host,
                port,
                allow_loopback=_is_loopback_host(host),
            )
        except ValueError as exc:
            raise _ProxyRequestError(400, str(exc)) from exc
        return host, port, ip

    def _connect_tunnel(
        self,
        target: str,
        headers: list[tuple[str, str]],
        sock: socket.socket,
        initial: bytes,
    ) -> None:
        host, port, ip = self._parse_connect_target(target, headers)
        try:
            upstream = socket.create_connection((ip, port), timeout=15)
        except OSError as exc:
            raise _ProxyUpstreamError() from exc
        try:
            sock.sendall(
                b"HTTP/1.1 200 Connection Established\r\n"
                b"Proxy-Agent: Brahma-Pinned\r\n\r\n"
            )
            self._response_committed = True
            if initial:
                upstream.sendall(initial)
            self._relay(sock, upstream)
        finally:
            try:
                upstream.close()
            except OSError:
                pass

    def _forward_http(
        self,
        method: str,
        target: str,
        version: str,
        headers: list[tuple[str, str]],
        sock: socket.socket,
        initial: bytes,
    ) -> None:
        host_values = self._header_values(headers, "Host")
        if len(host_values) != 1:
            raise _ProxyRequestError(400, "HTTP/1.x forward requests require exactly one Host header.")
        try:
            header_host, header_port = _parse_authority(host_values[0], default_port=80)
        except ValueError as exc:
            raise _ProxyRequestError(400, str(exc)) from exc

        try:
            if "://" in target:
                parsed = urlsplit(target)
                if parsed.scheme.lower() != "http":
                    raise _ProxyRequestError(400, "Only HTTP absolute-form targets may use the forward path.")
                if parsed.username or parsed.password or parsed.fragment or not parsed.hostname:
                    raise _ProxyRequestError(400, "Absolute-form target is malformed.")
                target_host = _normalize_host(parsed.hostname)
                target_port = _validate_port(parsed.port or 80)
                if not _hosts_match(target_host, header_host) or target_port != header_port:
                    raise _ProxyRequestError(400, "Absolute-form target does not match the Host header.")
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
            else:
                if not target.startswith("/") or target.startswith("//"):
                    raise _ProxyRequestError(400, "Origin-form request target is malformed.")
                parsed = urlsplit(target)
                if parsed.fragment:
                    raise _ProxyRequestError(400, "Origin-form request target may not contain a fragment.")
                target_host = header_host
                target_port = header_port
                path = target or "/"
        except _ProxyRequestError:
            raise
        except ValueError as exc:
            raise _ProxyRequestError(400, "Request target is malformed.") from exc

        host, port, ip = self._resolve_destination(target_host, target_port, scheme="http")

        connection_tokens = _split_connection_tokens(headers)
        upgrade_values = [
            value.strip().lower()
            for value in self._header_values(headers, "Upgrade")
            if value.strip()
        ]
        if upgrade_values:
            if (
                upgrade_values != ["websocket"]
                or "upgrade" not in connection_tokens
                or not _is_loopback_host(host)
            ):
                raise _ProxyRequestError(
                    400,
                    "WebSocket upgrade is only supported for loopback HTTP destinations.",
                )
            websocket_upgrade = True
        else:
            if "upgrade" in connection_tokens:
                raise _ProxyRequestError(400, "Unsupported HTTP connection upgrade.")
            websocket_upgrade = False

        hop_by_hop = {
            "proxy-connection",
            "proxy-authenticate",
            "proxy-authorization",
            "connection",
            "keep-alive",
            "te",
            "trailer",
        } | connection_tokens
        if not websocket_upgrade:
            hop_by_hop.add("upgrade")

        upstream_headers: list[str] = []
        canonical_host = _format_authority(host, port, default_port=80)
        for key, value in headers:
            lowered = key.lower()
            if lowered in hop_by_hop or lowered == "host":
                continue
            upstream_headers.append(f"{key}: {value}")
        upstream_headers.append(f"Host: {canonical_host}")
        if websocket_upgrade:
            upstream_headers.append("Connection: Upgrade")
            upstream_headers.append("Upgrade: websocket")
        else:
            upstream_headers.append("Connection: close")

        request_bytes = (
            f"{method} {path} {version}\r\n"
            + "\r\n".join(upstream_headers)
            + "\r\n\r\n"
        ).encode("iso-8859-1")

        try:
            upstream = socket.create_connection((ip, port), timeout=15)
        except OSError as exc:
            raise _ProxyUpstreamError() from exc
        try:
            upstream.sendall(request_bytes)
            self._upstream_request_started = True
            if initial:
                upstream.sendall(initial)
            self._relay(sock, upstream, validate_http_response=True)
        finally:
            try:
                upstream.close()
            except OSError:
                pass

    @staticmethod
    def _validate_response_headers(header_block: bytes) -> int:
        if not header_block.endswith(b"\r\n\r\n"):
            raise _ProxyUpstreamError("Malformed upstream response header block.")
        rows = header_block[:-4].split(b"\r\n")
        if not rows or not rows[0]:
            raise _ProxyUpstreamError("Malformed upstream response.")
        status_line = rows[0].decode("iso-8859-1") + "\r\n"
        if not _STATUS_LINE_RE.fullmatch(status_line):
            raise _ProxyUpstreamError("Malformed upstream status line.")
        status = int(status_line.split(" ", 2)[1])
        singleton = {"content-length"}
        seen: set[str] = set()
        for row in rows[1:]:
            if not row or b":" not in row:
                raise _ProxyUpstreamError("Malformed upstream response header.")
            if len(row) > _MAX_HEADER_LINE_BYTES:
                raise _ProxyUpstreamError("Upstream response header line exceeds the safety limit.")
            key_bytes, value_bytes = row.split(b":", 1)
            key = key_bytes.decode("iso-8859-1").strip()
            value = value_bytes.decode("iso-8859-1").strip()
            if not _is_header_name(key) or _has_forbidden_header_value_controls(value):
                raise _ProxyUpstreamError("Malformed upstream response header.")
            lowered = key.lower()
            if lowered in singleton and lowered in seen:
                raise _ProxyUpstreamError("Duplicate upstream Content-Length header.")
            seen.add(lowered)
        return status

    @classmethod
    def _relay(
        cls,
        left: socket.socket,
        right: socket.socket,
        *,
        validate_http_response: bool = False,
    ) -> None:
        sockets = [left, right]
        response_buffer = b""
        response_validated = not validate_http_response
        while sockets:
            if left not in sockets:
                return
            readable, _, _ = select.select(sockets, [], [], 15)
            if not readable:
                return

            for source in readable:
                if source not in sockets:
                    continue
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
                    if source is right and validate_http_response and not response_validated:
                        raise _ProxyUpstreamError(
                            "Upstream response ended before a valid response header."
                        )
                    continue

                if source is right and not response_validated:
                    response_buffer += data
                    if len(response_buffer) > _MAX_HEADER_BYTES:
                        raise _ProxyUpstreamError("Upstream response headers exceed the safety limit.")
                    while True:
                        marker = response_buffer.find(b"\r\n\r\n")
                        if marker < 0:
                            break
                        end = marker + 4
                        status = cls._validate_response_headers(response_buffer[:end])
                        left.sendall(response_buffer[:end])
                        response_buffer = response_buffer[end:]
                        if status >= 200 or status == 101:
                            response_validated = True
                            if response_buffer and left in sockets:
                                left.sendall(response_buffer)
                                response_buffer = b""
                            break
                    continue

                if dest not in sockets:
                    continue
                dest.sendall(data)


class PinnedBrowserProxy:
    """Lifecycle-managed loopback HTTP proxy for browser DNS pinning."""

    def __init__(self):
        self._resolver = _PinnedResolver()
        self._server: _ThreadedServer | None = None
        self._thread: threading.Thread | None = None
        self._started = False

    def _make_server(self) -> _ThreadedServer:
        server = _ThreadedServer(("127.0.0.1", 0), _Handler)
        server.owner = self
        return server

    @property
    def server_url(self) -> str:
        if self._server is None:
            raise RuntimeError("Pinned browser proxy is not started.")
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> str:
        if self._thread and self._thread.is_alive():
            return self.server_url
        if self._server is None:
            self._server = self._make_server()
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="BrahmaBrowserPinnedProxy",
            daemon=True,
        )
        self._started = True
        self._thread.start()
        return self.server_url

    def close(self) -> None:
        server = self._server
        thread = self._thread
        if server is None:
            return
        try:
            if self._started:
                server.shutdown()
            server.close_active_sockets()
            if thread and thread is not threading.current_thread():
                thread.join(timeout=2)
        finally:
            try:
                server.server_close()
            except OSError:
                pass
            self._server = None
            self._started = False
            self._thread = None
