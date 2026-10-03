from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import struct
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.user_paths import get_user_data_dir
from .integrations import integrations


_DEVICE_FILE = get_user_data_dir() / "config" / "device_network.json"


def _now() -> float:
    return time.time()


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
    tmp.write_text(data, encoding="utf-8")
    try:
        with tmp.open("r+", encoding="utf-8") as handle:
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        pass
    os.replace(tmp, path)


def _safe_id(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._:-]+", "-", str(value or "").strip())
    return value.strip("-")[:180] or uuid.uuid4().hex


def _android_name(serial: str, metadata: str) -> str:
    match = re.search(r"\bmodel:([^\s]+)", metadata or "", re.I)
    if match:
        return match.group(1).replace("_", " ").strip()
    if ":" in serial:
        return f"Android • {serial.split(':', 1)[0]}"
    return f"Android • {serial}"


def _android_kind(metadata: str) -> str:
    lower = (metadata or "").lower()
    if "tablet" in lower:
        return "tablet"
    if "tv" in lower or "google_tv" in lower or "android_tv" in lower:
        return "tv"
    return "phone"


def _normalize_mac(mac: str) -> str:
    compact = re.sub(r"[^0-9a-fA-F]", "", str(mac or ""))
    if len(compact) != 12:
        return ""
    return ":".join(compact[i:i + 2] for i in range(0, 12, 2)).upper()


def _is_ip_endpoint(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    host = text.rsplit(":", 1)[0] if ":" in text else text
    try:
        socket.inet_aton(host)
        return True
    except OSError:
        return False


@dataclass
class DeviceRecord:
    device_id: str
    name: str
    device_type: str
    backend: str
    status: str = "Offline"
    address: str = ""
    serial: str = ""
    mac: str = ""
    control_url: str = ""
    wake_method: str = ""
    mode: str = "background"
    auto_reconnect: bool = True
    capabilities: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_seen: float = 0.0
    added_at: float = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeviceRecord":
        allowed = {
            key: data[key]
            for key in cls.__dataclass_fields__
            if key in data
        }
        allowed["device_id"] = str(allowed.get("device_id") or uuid.uuid4().hex)
        allowed["name"] = str(allowed.get("name") or "Device")
        allowed["device_type"] = str(allowed.get("device_type") or "device")
        allowed["backend"] = str(allowed.get("backend") or "unknown")
        allowed["capabilities"] = list(allowed.get("capabilities") or [])
        allowed["metadata"] = dict(allowed.get("metadata") or {})
        return cls(**allowed)


class DeviceManager:
    """Unified wireless device manager for Brahma.

    The manager intentionally normalizes disparate integrations without forcing
    every device type into the same transport. Android gets ADB/scrcpy control,
    Apple TV gets pyatv discovery/control, Matter uses chip-tool capability
    detection, and generic PCs/TVs can be paired with an explicit control URL
    and/or Wake-on-LAN credentials.

    Screen mirroring is only claimed when a backend can actually provide it.
    """

    LIVE_CACHE_TTL = 8.0
    RECONNECT_INTERVAL = 15.0

    def __init__(self, path: Path | None = None):
        self.path = Path(path or _DEVICE_FILE)
        self._devices: dict[str, DeviceRecord] = {}
        self._loaded = False
        self._last_live_scan = 0.0

    # ---------- persistence ----------
    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            records = raw.get("devices", []) if isinstance(raw, dict) else []
            for item in records:
                if isinstance(item, dict):
                    record = DeviceRecord.from_dict(item)
                    self._devices[record.device_id] = record
        except Exception:
            self._devices = {}

    def _save(self) -> None:
        _atomic_json_write(
            self.path,
            {"version": 1, "devices": [item.to_dict() for item in self._devices.values()]},
        )

    def _upsert(self, record: DeviceRecord, *, save: bool = True) -> DeviceRecord:
        self._ensure_loaded()
        existing = self._devices.get(record.device_id)
        if existing is not None:
            record.added_at = existing.added_at
            if not record.name or record.name == "Device":
                record.name = existing.name
        self._devices[record.device_id] = record
        if save:
            self._save()
        return record

    # ---------- normalization / discovery ----------
    def _merge_android(self, devices: list[dict[str, str]]) -> list[DeviceRecord]:
        discovered: list[DeviceRecord] = []
        for item in devices:
            serial = str(item.get("serial") or "").strip()
            if not serial:
                continue
            metadata = str(item.get("metadata") or "")
            existing_id = next(
                (
                    key for key, record in self._devices.items()
                    if record.backend == "adb" and (record.serial == serial or record.address == serial)
                ),
                "",
            )
            device_id = existing_id or f"android:{_safe_id(serial)}"
            record = self._devices.get(device_id)
            device_type = _android_kind(metadata)
            record = DeviceRecord(
                device_id=device_id,
                name=record.name if record else _android_name(serial, metadata),
                device_type=device_type,
                backend="adb",
                status="Connected",
                address=(
                    serial if _is_ip_endpoint(serial) else (record.address if record else "")
                ),
                serial=serial,
                control_url=record.control_url if record else "",
                wake_method=record.wake_method if record else "",
                mode=record.mode if record else "background",
                auto_reconnect=record.auto_reconnect if record else True,
                capabilities=sorted(
                    set(
                        (record.capabilities if record else [])
                        + [
                            "screen",
                            "touch",
                            "keyboard",
                            "clipboard",
                            "media",
                            "background_connection",
                        ]
                    )
                ),
                metadata={**(record.metadata if record else {}), "adb": metadata},
                last_seen=_now(),
                added_at=record.added_at if record else _now(),
            )
            self._devices[device_id] = record
            discovered.append(record)
        return discovered

    def _merge_appletv(self, devices: list[dict[str, Any]]) -> list[DeviceRecord]:
        discovered: list[DeviceRecord] = []
        for item in devices:
            identifier = str(item.get("identifier") or item.get("address") or item.get("name") or "").strip()
            if not identifier:
                continue
            device_id = f"appletv:{_safe_id(identifier)}"
            existing = self._devices.get(device_id)
            record = DeviceRecord(
                device_id=device_id,
                name=existing.name if existing else str(item.get("name") or "Apple TV"),
                device_type="tv",
                backend="pyatv",
                status="Connected",
                address=str(item.get("address") or ""),
                serial=str(item.get("identifier") or ""),
                control_url=existing.control_url if existing else "",
                wake_method=existing.wake_method if existing else "",
                mode=existing.mode if existing else "background",
                auto_reconnect=existing.auto_reconnect if existing else True,
                capabilities=sorted(
                    set(
                        (existing.capabilities if existing else [])
                        + ["remote", "media", "keyboard", "background_connection"]
                    )
                ),
                metadata={**(existing.metadata if existing else {}), "device_info": item.get("device_info", "")},
                last_seen=_now(),
                added_at=existing.added_at if existing else _now(),
            )
            self._devices[device_id] = record
            discovered.append(record)
        return discovered

    def scan(self, *, include_android: bool = True, include_appletv: bool = True) -> list[dict[str, Any]]:
        self._ensure_loaded()
        if include_android:
            try:
                self._merge_android(integrations.android_devices())
            except Exception:
                pass
        if include_appletv:
            try:
                self._merge_appletv(integrations.apple_tv_scan())
            except Exception:
                pass

        live_ids = {
            key for key, record in self._devices.items()
            if record.last_seen > 0 and _now() - record.last_seen <= self.LIVE_CACHE_TTL
        }
        now = _now()
        for key, record in self._devices.items():
            if key in live_ids:
                continue
            if record.status == "Waking":
                continue
            record.status = "Standby" if record.wake_method else "Offline"
            record.metadata = dict(record.metadata)
        self._last_live_scan = now
        self._save()
        return self.list_devices()

    def list_devices(self) -> list[dict[str, Any]]:
        self._ensure_loaded()
        return [
            record.to_dict()
            for record in sorted(self._devices.values(), key=lambda x: (x.device_type, x.name.lower()))
        ]

    def get(self, device_id: str) -> DeviceRecord | None:
        self._ensure_loaded()
        return self._devices.get(str(device_id or "").strip())

    def status(self, *, refresh: bool = False) -> dict[str, Any]:
        self._ensure_loaded()
        if refresh or _now() - self._last_live_scan >= self.LIVE_CACHE_TTL:
            self.scan()
        data = self.list_devices()
        return {
            "devices": data,
            "counts": {
                "total": len(data),
                "connected": sum(1 for x in data if x["status"] == "Connected"),
                "standby": sum(1 for x in data if x["status"] == "Standby"),
                "waking": sum(1 for x in data if x["status"] == "Waking"),
                "offline": sum(1 for x in data if x["status"] == "Offline"),
            },
            "backends": integrations.status(),
            "last_scan": self._last_live_scan,
        }

    # ---------- pairing / management ----------
    def pair(
        self,
        *,
        name: str,
        device_type: str,
        address: str = "",
        serial: str = "",
        mac: str = "",
        backend: str = "manual",
        control_url: str = "",
        wake_method: str = "",
        capabilities: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        auto_reconnect: bool = True,
    ) -> dict[str, Any]:
        self._ensure_loaded()
        name = str(name or "Device").strip() or "Device"
        device_type = str(device_type or "device").strip().lower()
        address = str(address or "").strip()
        serial = str(serial or "").strip()
        normalized_mac = _normalize_mac(mac)
        if not wake_method and normalized_mac:
            wake_method = "wol"
        base = serial or address or normalized_mac or name
        device_id = f"{device_type}:{_safe_id(base)}"
        existing = self._devices.get(device_id)
        requested_caps = list(capabilities or [])
        if backend == "adb" and not requested_caps:
            requested_caps = [
                "screen", "touch", "keyboard", "clipboard",
                "media", "background_connection",
            ]
        elif backend == "pyatv" and not requested_caps:
            requested_caps = ["remote", "media", "keyboard", "background_connection"]
        record = DeviceRecord(
            device_id=device_id,
            name=name,
            device_type=device_type,
            backend=str(backend or "manual"),
            status="Standby" if wake_method else "Offline",
            address=address,
            serial=serial,
            mac=normalized_mac,
            control_url=str(control_url or "").strip(),
            wake_method=str(wake_method or "").strip().lower(),
            mode=existing.mode if existing else "background",
            auto_reconnect=bool(auto_reconnect),
            capabilities=sorted(set(requested_caps or (existing.capabilities if existing else []))),
            metadata=dict(metadata or (existing.metadata if existing else {})),
            last_seen=existing.last_seen if existing else 0.0,
            added_at=existing.added_at if existing else _now(),
        )
        self._upsert(record)
        return record.to_dict()

    def rename(self, device_id: str, name: str) -> dict[str, Any]:
        record = self.get(device_id)
        if record is None:
            raise ValueError("Device not found.")
        clean = str(name or "").strip()
        if not clean:
            raise ValueError("Device name cannot be empty.")
        record.name = clean[:120]
        self._save()
        return record.to_dict()

    def forget(self, device_id: str) -> bool:
        self._ensure_loaded()
        existed = self._devices.pop(str(device_id or "").strip(), None) is not None
        if existed:
            self._save()
        return existed

    def set_mode(self, device_id: str, mode: str) -> dict[str, Any]:
        record = self.get(device_id)
        if record is None:
            raise ValueError("Device not found.")
        normalized = str(mode or "").strip().lower()
        if normalized not in {"visible", "background"}:
            raise ValueError("Mode must be visible or background.")
        record.mode = normalized
        self._save()
        return record.to_dict()

    def disconnect(self, device_id: str) -> dict[str, Any]:
        record = self.get(device_id)
        if record is None:
            raise ValueError("Device not found.")
        result: dict[str, Any] = {"ok": True}
        if record.backend == "adb":
            adb = integrations.info("adb")
            endpoint = record.address or record.serial
            if adb.installed and adb.path and endpoint and _is_ip_endpoint(endpoint):
                result = _run_adb(adb.path, ["disconnect", endpoint], timeout=6.0)
            elif adb.installed and adb.path and record.serial and record.address:
                result = _run_adb(adb.path, ["disconnect", record.serial], timeout=6.0)
        record.status = "Offline"
        record.mode = "background"
        record.last_seen = 0.0
        self._save()
        result["device"] = record.to_dict()
        return result

    # ---------- Android ----------
    def pair_android(self, address: str, pairing_code: str = "") -> dict[str, Any]:
        addr = str(address or "").strip()
        if not addr:
            raise ValueError("Android pairing address is required.")
        adb = integrations.info("adb")
        if not adb.installed or not adb.path:
            return {"ok": False, "error": "ADB is not installed."}

        if pairing_code:
            paired = _run_adb(adb.path, ["pair", addr, str(pairing_code).strip()], timeout=12.0)
            if not paired["ok"]:
                return paired

        connected = _run_adb(adb.path, ["connect", addr], timeout=8.0)
        if not connected["ok"]:
            return connected

        devices = integrations.android_devices()
        found = next((item for item in devices if item.get("serial") == addr), None)
        if not found:
            return {
                "ok": False,
                "error": f"ADB accepted the connection but '{addr}' is not listed as connected.",
                "output": connected.get("output", ""),
            }
        merged = self._merge_android([found])
        self._save()
        return {"ok": True, "device": merged[0].to_dict() if merged else found}

    def connect(self, device_id: str) -> dict[str, Any]:
        record = self.get(device_id)
        if record is None:
            raise ValueError("Device not found.")

        if record.backend == "adb":
            adb = integrations.info("adb")
            endpoint = record.address or record.serial
            if adb.installed and adb.path and endpoint and _is_ip_endpoint(endpoint):
                response = _run_adb(adb.path, ["connect", endpoint], timeout=8.0)
                if not response["ok"]:
                    record.status = "Offline"
                    self._save()
                    return response
            live = integrations.android_devices()
            found = next(
                (item for item in live if item.get("serial") in {record.serial, record.address}),
                None,
            )
            if found:
                self._merge_android([found])
                self._save()
                return {"ok": True, "device": self.get(device_id).to_dict()}

        elif record.backend == "pyatv":
            live = integrations.apple_tv_scan()
            found = next(
                (
                    item for item in live
                    if str(item.get("identifier") or "") == record.serial
                    or str(item.get("address") or "") == record.address
                    or str(item.get("name") or "").lower() == record.name.lower()
                ),
                None,
            )
            if found:
                self._merge_appletv([found])
                self._save()
                return {"ok": True, "device": self.get(device_id).to_dict()}

        elif record.control_url:
            record.status = "Connected"
            record.last_seen = _now()
            self._save()
            return {"ok": True, "device": record.to_dict()}

        return {"ok": False, "error": f"Unable to connect to {record.name} using {record.backend}."}

    def show_spec(self, device_id: str) -> dict[str, Any]:
        record = self.get(device_id)
        if record is None:
            raise ValueError("Device not found.")
        if "screen" in record.capabilities and record.backend == "adb":
            record.mode = "visible"
            self._save()
            return {
                "ok": True,
                "backend": "scrcpy",
                "serial": record.serial,
                "window_title": f"Brahma • {record.name}",
                "device": record.to_dict(),
            }
        if record.control_url:
            record.mode = "visible"
            self._save()
            return {"ok": True, "backend": "web", "url": record.control_url, "device": record.to_dict()}
        return {
            "ok": False,
            "screen_available": False,
            "device": record.to_dict(),
            "error": "This device adapter provides control/status but no embeddable screen stream.",
        }

    def background(self, device_id: str) -> dict[str, Any]:
        record = self.get(device_id)
        if record is None:
            raise ValueError("Device not found.")
        record.mode = "background"
        self._save()
        if record.backend == "adb":
            result = self.connect(device_id)
            result["mode"] = "background"
            return result
        return {"ok": True, "mode": "background", "device": record.to_dict()}

    # ---------- generic commands ----------
    def command(self, device_id: str, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        record = self.get(device_id)
        if record is None:
            raise ValueError("Device not found.")
        payload = dict(payload or {})
        action = str(action or "").strip().lower()

        if record.backend == "adb":
            return self._android_command(record, action, payload)
        if record.backend == "pyatv":
            return self._appletv_command(record, action, payload)

        if action in {"open", "show"}:
            return self.show_spec(device_id)
        if action in {"background", "hide"}:
            return self.background(device_id)
        if action == "wake":
            return self.wake(device_id)
        return {
            "ok": False,
            "device": record.to_dict(),
            "error": f"Command '{action}' is not supported by backend '{record.backend}'.",
        }

    def _android_command(self, record: DeviceRecord, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        adb = integrations.info("adb")
        if not adb.installed or not adb.path:
            return {"ok": False, "error": "ADB is not installed."}
        serial = record.serial
        keymap = {
            "home": "KEYCODE_HOME",
            "back": "KEYCODE_BACK",
            "recent": "KEYCODE_APP_SWITCH",
            "play": "KEYCODE_MEDIA_PLAY",
            "pause": "KEYCODE_MEDIA_PAUSE",
            "volume_up": "KEYCODE_VOLUME_UP",
            "volume_down": "KEYCODE_VOLUME_DOWN",
            "mute": "KEYCODE_VOLUME_MUTE",
            "power": "KEYCODE_POWER",
        }
        if action in keymap:
            return _run_adb(adb.path, ["-s", serial, "shell", "input", "keyevent", keymap[action]], timeout=4.0)

        if action in {"tap", "click"}:
            x = int(payload.get("x", -1))
            y = int(payload.get("y", -1))
            if x < 0 or y < 0:
                return {"ok": False, "error": "tap/click requires non-negative x and y."}
            return _run_adb(adb.path, ["-s", serial, "shell", "input", "tap", str(x), str(y)], timeout=4.0)

        if action == "swipe":
            values = [payload.get(k) for k in ("x1", "y1", "x2", "y2")]
            if any(v is None for v in values):
                return {"ok": False, "error": "swipe requires x1,y1,x2,y2."}
            duration = max(50, int(payload.get("duration_ms", 300)))
            return _run_adb(
                adb.path,
                ["-s", serial, "shell", "input", "swipe", *(str(int(v)) for v in values), str(duration)],
                timeout=4.0,
            )

        if action == "text":
            text_value = str(payload.get("text") or "")
            if not text_value:
                return {"ok": False, "error": "text requires a non-empty text value."}
            # ADB input text treats spaces specially.
            encoded = text_value.replace("%", "%25").replace(" ", "%s").replace("'", "%27").replace('"', '%22')
            return _run_adb(adb.path, ["-s", serial, "shell", "input", "text", encoded], timeout=4.0)

        if action == "clipboard":
            # scrcpy handles bidirectional clipboard when available. The manager
            # deliberately does not emulate clipboard writes through shell hacks.
            return {
                "ok": False,
                "error": "Use the embedded scrcpy surface for bidirectional clipboard support.",
            }

        return {"ok": False, "error": f"Android command '{action}' is not supported."}

    def _appletv_command(self, record: DeviceRecord, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action in {"show", "open"}:
            return self.show_spec(record.device_id)
        if action in {"background", "hide"}:
            return self.background(record.device_id)
        if action == "wake":
            return self.wake(record.device_id)
        if __import__("importlib").util.find_spec("pyatv") is None:
            return {"ok": False, "error": "pyatv is not installed."}

        async def _run() -> dict[str, Any]:
            import pyatv

            configs = await pyatv.scan(asyncio.get_running_loop())
            target = next(
                (
                    config for config in configs
                    if str(getattr(config, "identifier", "") or "") == record.serial
                    or str(getattr(config, "address", "") or "") == record.address
                    or str(getattr(config, "name", "") or "").lower() == record.name.lower()
                ),
                None,
            )
            if target is None:
                return {"ok": False, "error": f"{record.name} is not currently discoverable."}

            atv = await pyatv.connect(target)
            try:
                remote = getattr(atv, "remote_control", None)
                if remote is None:
                    return {"ok": False, "error": "Apple TV remote control is unavailable."}
                mapping = {
                    "up": "up",
                    "down": "down",
                    "left": "left",
                    "right": "right",
                    "select": "select",
                    "menu": "menu",
                    "home": "home",
                    "play": "play",
                    "pause": "pause",
                    "stop": "stop",
                    "next": "skip_forward",
                    "previous": "skip_backward",
                    "volume_up": "volume_up",
                    "volume_down": "volume_down",
                }
                method_name = mapping.get(action)
                if not method_name or not hasattr(remote, method_name):
                    return {"ok": False, "error": f"Apple TV command '{action}' is unavailable."}
                await getattr(remote, method_name)()
                record.status = "Connected"
                record.last_seen = _now()
                self._save()
                return {"ok": True, "device": record.to_dict(), "command": action}
            finally:
                try:
                    atv.close()
                except Exception:
                    pass

        try:
            return asyncio.run(_run())
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ---------- wake / reconnect ----------
    def wake(self, device_id: str) -> dict[str, Any]:
        record = self.get(device_id)
        if record is None:
            raise ValueError("Device not found.")
        if record.wake_method != "wol" or not record.mac:
            return {
                "ok": False,
                "device": record.to_dict(),
                "error": "No supported wake method is configured for this device.",
            }

        mac = _normalize_mac(record.mac)
        if not mac:
            return {"ok": False, "error": "Invalid Wake-on-LAN MAC address."}

        try:
            packet = b"\xff" * 6 + bytes.fromhex(mac.replace(":", "")) * 16
            broadcast = str(record.metadata.get("broadcast") or "255.255.255.255")
            port = int(record.metadata.get("wake_port") or 9)
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.sendto(packet, (broadcast, port))
            finally:
                sock.close()
            record.status = "Waking"
            record.last_seen = 0.0
            self._save()
            return {"ok": True, "device": record.to_dict(), "wake_method": "wol"}
        except Exception as exc:
            return {"ok": False, "device": record.to_dict(), "error": str(exc)}

    def tick_reconnect(self) -> dict[str, Any]:
        self._ensure_loaded()
        attempted = 0
        reconnected = 0
        now = _now()
        for record in self._devices.values():
            if not record.auto_reconnect or record.backend != "adb":
                continue
            if now - record.last_seen < self.RECONNECT_INTERVAL:
                continue
            endpoint = record.address
            if not endpoint or not _is_ip_endpoint(endpoint):
                continue
            attempted += 1
            result = self.connect(record.device_id)
            if result.get("ok"):
                reconnected += 1
        return {"attempted": attempted, "reconnected": reconnected}

    # ---------- integration information ----------
    def capabilities(self) -> dict[str, Any]:
        data = integrations.status()
        return {
            "android": {
                "adb": bool(data["integrations"]["adb"]["installed"]),
                "scrcpy": bool(data["integrations"]["scrcpy"]["installed"]),
                "wireless": True,
            },
            "apple_tv": {
                "pyatv": bool(data["integrations"]["pyatv"]["python_module"]),
                "screen_embedding": False,
                "remote_control": True,
            },
            "matter": {
                "chip_tool": bool(data["integrations"]["matter"]["installed"]),
            },
            "generic": {
                "wake_on_lan": True,
                "web_control_panel": True,
            },
        }


def _run_adb(adb_path: str, args: list[str], *, timeout: float = 4.0) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [adb_path, *args],
            capture_output=True,
            text=True,
            timeout=max(0.5, timeout),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "output": output[-3000:],
    }


device_manager = DeviceManager()
