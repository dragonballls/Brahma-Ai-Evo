"""Small local TLS helper for Brahma's self-hosted services.

Certificates are generated locally and used with exact certificate pinning by the
Android bridge. No global certificate-verification bypass is provided.
"""
from __future__ import annotations

import datetime
import hashlib
import ipaddress
from pathlib import Path
from typing import Iterable

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def ensure_local_certificate(
    cert_dir: Path,
    *,
    key_name: str,
    cert_name: str,
    common_name: str,
    hosts: Iterable[str],
) -> tuple[Path, Path]:
    cert_dir = Path(cert_dir)
    key_path = cert_dir / key_name
    cert_path = cert_dir / cert_name

    if key_path.is_file() and cert_path.is_file():
        return key_path, cert_path

    cert_dir.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ])

    names: list[x509.GeneralName] = []
    for raw in hosts:
        host = str(raw or "").strip()
        if not host:
            continue
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            names.append(x509.DNSName(host))
    if not names:
        names = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .sign(key, hashes.SHA256())
    )

    key_tmp = key_path.with_suffix(key_path.suffix + ".tmp")
    cert_tmp = cert_path.with_suffix(cert_path.suffix + ".tmp")
    key_tmp.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_tmp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_tmp.replace(key_path)
    cert_tmp.replace(cert_path)
    return key_path, cert_path


def certificate_sha256(cert_path: Path) -> str:
    return hashlib.sha256(Path(cert_path).read_bytes()).hexdigest().lower()
