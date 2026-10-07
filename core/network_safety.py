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


def read_bounded(response, max_bytes: int) -> bytes:
    raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"Network response exceeded the {max_bytes} byte safety limit.")
    return raw
