"""Validation for Brahma's local AI transport endpoints.

Local-provider traffic must remain on the local machine or a literal private/link-local
address. Hostnames other than localhost are rejected to avoid DNS-rebinding surprises.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


def validate_local_endpoint(value: object) -> str:
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        raise ValueError("Local AI endpoint is required.")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or not parsed.hostname:
        raise ValueError("Local AI endpoint must be a credential-free HTTP(S) URL.")
    host = parsed.hostname.strip().lower().rstrip(".")
    if host == "localhost":
        return raw
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError(
            "Local AI endpoint must use localhost or a literal private/link-local IP address."
        ) from exc
    if not (address.is_loopback or address.is_private or address.is_link_local):
        raise ValueError("Local AI endpoint must resolve to a local/private/link-local address.")
    return raw
