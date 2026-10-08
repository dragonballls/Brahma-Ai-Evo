from __future__ import annotations

import json
import os
import stat as _stat
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .authentication import generate_device_id, generate_device_secret, hash_secret, constant_time_equals
from .models import DeviceRecord
from .protocol import now_iso


def _path_has_link_component(path: Path) -> bool:
    try:
        current = Path(path.anchor) if path.anchor else Path(".")
        parts = path.parts[1:] if path.anchor else path.parts
        for part in parts:
            current = current / part
            if current.is_symlink():
                return True
            is_junction = getattr(current, "is_junction", None)
            if is_junction is not None and is_junction():
                return True
            if os.name == "nt":
                attrs = getattr(current.stat(follow_symlinks=False), "st_file_attributes", 0)
                reparse = getattr(_stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                if reparse and attrs & reparse:
                    return True
        return False
    except OSError:
        return True


def _validate_registry_path(path: Path) -> None:
    path = Path(path)
    if _path_has_link_component(path.parent):
        raise RuntimeError("Device registry parent contains a symlink/junction/reparse component.")
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise RuntimeError("Device registry path must not be a symlink/junction.")
    if path.exists() and not path.is_file():
        raise RuntimeError("Device registry path must be a regular file.")


def _read_registry_bytes(path: Path, *, max_bytes: int = 4 * 1024 * 1024) -> bytes:
    if os.name == "nt":
        from core.windows_file_safety import open_safe_file
        fd, _final_path, info = open_safe_file(path, write=False)
        if int(info.nNumberOfLinks) > 1:
            os.close(fd)
            raise OSError("Device registry has multiple hard links.")
    else:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        if os.fstat(fd).st_nlink > 1:
            os.close(fd)
            raise OSError("Device registry has multiple hard links.")
    try:
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(max_bytes + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(raw) > max_bytes:
        raise ValueError("Device registry exceeds the 4 MiB safety limit.")
    return raw


class DeviceManager:
    def __init__(self, registry_path: Path):
        self.registry_path = Path(registry_path)
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        _validate_registry_path(self.registry_path)
        self._lock = threading.RLock()
        self._devices: dict[str, DeviceRecord] = {}
        self.load()

    def _quarantine_corrupt_registry(self, raw_bytes: bytes) -> Path:
        """Atomically move a corrupt registry aside, preserving its exact bytes."""
        _validate_registry_path(self.registry_path)
        backup = self.registry_path.with_name(
            f"{self.registry_path.name}.corrupt-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        )
        try:
            self.registry_path.replace(backup)
        except OSError as exc:
            raise RuntimeError(
                "Device registry is corrupted and could not be quarantined safely."
            ) from exc
        try:
            if backup.read_bytes() != raw_bytes:
                raise RuntimeError(
                    "Device registry quarantine verification failed; refusing recovery."
                )
        except OSError as exc:
            raise RuntimeError(
                "Device registry quarantine could not be verified safely."
            ) from exc
        return backup

    def load(self) -> None:
        with self._lock:
            _validate_registry_path(self.registry_path)
            if not self.registry_path.exists():
                self._devices = {}
                return
            raw_bytes = None
            try:
                raw_bytes = _read_registry_bytes(self.registry_path)
                raw = json.loads(raw_bytes.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                if raw_bytes is None:
                    raise RuntimeError("Device registry could not be read safely; refusing recovery.")
                self._quarantine_corrupt_registry(raw_bytes)
                self._devices = {}
                return
            except OSError:
                # Preserve typed filesystem integrity failures such as hard-link rejection.
                raise
            if not isinstance(raw, dict):
                self._quarantine_corrupt_registry(raw_bytes)
                self._devices = {}
                return
            devices = raw.get("devices", raw)
            if not isinstance(devices, dict):
                self._quarantine_corrupt_registry(raw_bytes)
                self._devices = {}
                return
            loaded: dict[str, DeviceRecord] = {}
            for device_id, item in (devices or {}).items():
                try:
                    if not isinstance(item, dict):
                        raise ValueError("device record is not an object")
                    record = DeviceRecord.from_dict(item)
                    stored_key = str(device_id or "").strip()
                    record_id = str(record.device_id or "").strip()
                    if not stored_key or not record_id or stored_key != record_id:
                        raise ValueError("device record key does not match its embedded identity")
                    if not str(record.secret_hash or "").strip():
                        raise ValueError("device record is missing secret data")
                except Exception:
                    self._quarantine_corrupt_registry(raw_bytes)
                    self._devices = {}
                    return
                key = record_id
                # A persisted online flag cannot represent a live socket after restart.
                # Re-establish online state only after successful authentication.
                record.online = False
                record.connection_id = ""
                if key in loaded:
                    self._quarantine_corrupt_registry(raw_bytes)
                    self._devices = {}
                    return
                loaded[key] = record
            self._devices = loaded

    def save(self) -> None:
        with self._lock:
            _validate_registry_path(self.registry_path)
            payload = {"devices": {device_id: record.to_storage_dict() for device_id, record in self._devices.items()}}
            temp_path = self.registry_path.with_name(
                f".{self.registry_path.name}.{uuid.uuid4().hex}.tmp"
            )
            try:
                temp_path.write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                temp_path.replace(self.registry_path)
            except Exception:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise

    def list_devices(self) -> list[dict[str, Any]]:
        with self._lock:
            return [record.to_dict() for record in sorted(self._devices.values(), key=lambda item: (not item.online, item.name.lower()))]

    def get(self, device_id: str) -> DeviceRecord | None:
        with self._lock:
            return self._devices.get(str(device_id))

    def remove(self, device_id: str) -> bool:
        with self._lock:
            key = str(device_id)
            removed = self._devices.pop(key, None)
            if removed is None:
                return False
            try:
                self.save()
            except Exception:
                self._devices[key] = removed
                return False
            return True

    def rename(self, device_id: str, new_name: str) -> DeviceRecord | None:
        with self._lock:
            key = str(device_id)
            record = self._devices.get(key)
            if record is None:
                return None
            previous = DeviceRecord.from_dict(record.to_storage_dict())
            record.name = str(new_name or "").strip() or record.name
            try:
                self.save()
            except Exception:
                self._devices[key] = previous
                raise
            return record

    def revoke(self, device_id: str) -> bool:
        with self._lock:
            key = str(device_id)
            record = self._devices.get(key)
            if record is None:
                return False
            previous = DeviceRecord.from_dict(record.to_storage_dict())
            record.revoked = True
            record.online = False
            record.last_seen = now_iso()
            try:
                self.save()
            except Exception:
                self._devices[key] = previous
                return False
            return True

    def create_from_pairing(
        self,
        *,
        name: str,
        platform: str,
        os_version: str = "",
        agent_version: str = "",
        ip: str = "",
        battery: int | None = None,
        capabilities: list[str] | None = None,
        permissions: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[DeviceRecord, str]:
        with self._lock:
            device_id = ""
            for _ in range(20):
                candidate = generate_device_id(platform, name)
                if candidate not in self._devices:
                    device_id = candidate
                    break
            if not device_id:
                raise RuntimeError("Unable to allocate a unique device id; try again.")
            secret = generate_device_secret()
            record = DeviceRecord(
                device_id=device_id,
                name=name or "Unknown Device",
                platform=(platform or "unknown").lower(),
                os_version=os_version,
                agent_version=agent_version,
                ip=ip,
                online=False,
                last_seen=now_iso(),
                battery=battery,
                capabilities=list(capabilities or []),
                permissions=list(permissions or []),
                paired_at=now_iso(),
                secret_hash=hash_secret(secret),
                revoked=False,
                identity_fingerprint=hash_secret(f"{name}:{platform}:{os_version}:{secret}")[:16],
                metadata=dict(metadata or {}),
            )
            self._devices[device_id] = record
            try:
                self.save()
            except Exception:
                self._devices.pop(device_id, None)
                raise
            return record, secret

    def authenticate(self, device_id: str, secret: str, *, ip: str = "", connection_id: str = "") -> DeviceRecord | None:
        if not str(secret or "").strip():
            return None
        with self._lock:
            record = self._devices.get(str(device_id))
            if record is None or record.revoked:
                return None
            if not constant_time_equals(record.secret_hash, hash_secret(secret)):
                return None
            key = str(device_id)
            previous = DeviceRecord.from_dict(record.to_storage_dict())
            record.online = True
            record.last_seen = now_iso()
            record.ip = ip or record.ip
            record.connection_id = connection_id or record.connection_id
            try:
                self.save()
            except Exception:
                self._devices[key] = previous
                return None
            return record

    def mark_offline(self, device_id: str) -> None:
        with self._lock:
            key = str(device_id)
            record = self._devices.get(key)
            if record is None:
                return
            previous = DeviceRecord.from_dict(record.to_storage_dict())
            record.online = False
            record.last_seen = now_iso()
            record.connection_id = ""
            try:
                self.save()
            except Exception:
                self._devices[key] = previous
                raise

    def touch(self, device_id: str, *, ip: str = "") -> None:
        with self._lock:
            key = str(device_id)
            record = self._devices.get(key)
            if record is None:
                return
            previous = DeviceRecord.from_dict(record.to_storage_dict())
            record.last_seen = now_iso()
            record.online = True
            if ip:
                record.ip = ip
            try:
                self.save()
            except Exception:
                self._devices[key] = previous
                raise

    def update_capabilities(self, device_id: str, capabilities: list[str], permissions: list[str] | None = None) -> DeviceRecord | None:
        with self._lock:
            record = self._devices.get(str(device_id))
            if record is None:
                return None
            key = str(device_id)
            previous = DeviceRecord.from_dict(record.to_storage_dict())
            record.capabilities = list(dict.fromkeys(capabilities or []))
            if permissions is not None:
                record.permissions = list(dict.fromkeys(permissions or []))
            record.last_seen = now_iso()
            try:
                self.save()
            except Exception:
                self._devices[key] = previous
                raise
            return record

    def resolve(self, query: str) -> list[DeviceRecord]:
        normalized = " ".join(str(query or "").strip().lower().split())
        if not normalized:
            return []
        with self._lock:
            matches = []
            for record in self._devices.values():
                name = " ".join(str(record.name or "").lower().split())
                device_id = str(record.device_id or "").lower().strip()
                platform = str(record.platform or "").lower().strip()
                aliases = {alias for alias in (name, device_id, platform) if alias}
                exact_or_word_match = any(
                    normalized == alias
                    or bool(re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", normalized))
                    for alias in aliases
                )
                if exact_or_word_match:
                    matches.append(record)
                    continue
                category_words = set(re.findall(r"(?<!\w)(phone|tablet|pc|laptop|computer|windows)(?!\w)", normalized))
                if "phone" in category_words and platform in {"android", "ios"}:
                    matches.append(record)
                elif "tablet" in category_words and "tablet" in name:
                    matches.append(record)
                elif category_words.intersection({"pc", "laptop", "computer", "windows"}) and platform in {"windows", "desktop", "pc"}:
                    matches.append(record)
            return matches
