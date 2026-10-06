"""Selected advanced Brahma capabilities.

These are deliberately lightweight, opt-in runtime services. They reuse the
existing Brahma Connect, learned-rules, God’s Eye, and low-power architecture.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import platform
import socket
import statistics
import subprocess
import threading
import time
from typing import Any, Iterable
from urllib import error as urlerror
from urllib import request as urlrequest
from uuid import uuid4

from core.user_paths import get_user_data_dir


ROOT = get_user_data_dir() / "selected_capabilities"
_STATE_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_load(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(default, (dict, list)) and not isinstance(payload, type(default)):
            raise ValueError("Selected capability state has an unexpected root schema.")
        return payload
    except (UnicodeError, json.JSONDecodeError, ValueError):
        quarantine = path.with_name(
            f"{path.name}.corrupt-{int(time.time())}-{uuid4().hex[:8]}"
        )
        try:
            path.replace(quarantine)
        except OSError:
            pass
        return default
    except OSError as exc:
        raise RuntimeError(f"Unable to read selected capability state: {path}") from exc


def _json_save(path: Path, value: Any) -> None:
    with _STATE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}-{uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


class LearningEngine:
    """Persistent, compact learning from explicit outcomes and user feedback."""

    PATH = ROOT / "learning.json"
    MAX_EVENTS = 1000

    @classmethod
    def record(
        cls,
        signal: str,
        *,
        outcome: str = "",
        score: float | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        signal = str(signal or "").strip()
        if not signal:
            return {"success": False, "error": "signal is required"}
        with _STATE_LOCK:
            payload = _json_load(cls.PATH, {"events": []})
            events = payload.get("events", []) if isinstance(payload, dict) else []
            fingerprint = hashlib.sha256(
                json.dumps(
                    {"signal": signal, "outcome": outcome, "context": context or {}},
                    sort_keys=True,
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()[:16]
            event = {
                "id": uuid4().hex[:12],
                "fingerprint": fingerprint,
                "timestamp": _now(),
                "signal": signal[:500],
                "outcome": str(outcome or "")[:500],
                "score": float(score) if score is not None else None,
                "context": dict(context or {}),
            }
            events = [e for e in events if isinstance(e, dict) and e.get("fingerprint") != fingerprint]
            events.append(event)
            _json_save(cls.PATH, {"schema_version": 1, "events": events[-cls.MAX_EVENTS:]})
            return {"success": True, "event": event}

    @classmethod
    def recent(cls, limit: int = 25) -> list[dict[str, Any]]:
        payload = _json_load(cls.PATH, {"events": []})
        events = payload.get("events", []) if isinstance(payload, dict) else []
        return [e for e in events if isinstance(e, dict)][-max(1, min(int(limit), 100)):]

    @classmethod
    def feedback(cls, rule: str, category: str = "general") -> dict[str, Any]:
        from core.learned_rules import LearnedRulesEngine

        return LearnedRulesEngine.add_rule(
            str(rule or "").strip(),
            category=str(category or "general").strip() or "general",
            origin="continuous_learning",
        )


class AdvancedAnalyzer:
    """Statistics and trend analysis with no model call and no network cost."""

    @staticmethod
    def analyze(values: Iterable[float | int]) -> dict[str, Any]:
        data = []
        for value in values:
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                data.append(number)
        if not data:
            return {"success": False, "error": "No finite numeric samples."}

        ordered = sorted(data)
        q1 = statistics.quantiles(ordered, n=4, method="inclusive")[0] if len(data) >= 2 else ordered[0]
        q3 = statistics.quantiles(ordered, n=4, method="inclusive")[2] if len(data) >= 2 else ordered[0]
        iqr = q3 - q1
        low = q1 - 1.5 * iqr
        high = q3 + 1.5 * iqr
        outliers = [x for x in data if x < low or x > high]

        slope = 0.0
        if len(data) >= 2:
            mean_x = (len(data) - 1) / 2
            mean_y = statistics.mean(data)
            denom = sum((i - mean_x) ** 2 for i in range(len(data)))
            if denom:
                slope = sum((i - mean_x) * (y - mean_y) for i, y in enumerate(data)) / denom

        p95 = ordered[max(0, min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1))]
        return {
            "success": True,
            "count": len(data),
            "min": ordered[0],
            "max": ordered[-1],
            "mean": statistics.mean(data),
            "median": statistics.median(data),
            "stdev": statistics.stdev(data) if len(data) >= 2 else 0.0,
            "p95": p95,
            "slope_per_sample": slope,
            "delta": data[-1] - data[0],
            "outliers": outliers[-50:],
        }

    @classmethod
    def analyze_events(cls, events: Iterable[dict[str, Any]], key: str) -> dict[str, Any]:
        return cls.analyze(
            event.get(key)
            for event in events
            if isinstance(event, dict) and event.get(key) is not None
        )


class SpatialAudioEngine:
    """Small equal-power stereo spatializer.

    It can spatialize already-generated audio data or play a tiny diagnostic tone.
    Audio output is opt-in; mixing math itself has no audio-device side effects.
    """

    @staticmethod
    def gains(azimuth_deg: float, distance: float = 1.0) -> tuple[float, float]:
        az = max(-180.0, min(180.0, float(azimuth_deg)))
        d = max(0.05, float(distance))
        pan = max(-1.0, min(1.0, math.sin(math.radians(az))))
        attenuation = min(1.0, 1.0 / d)
        left = math.sqrt((1.0 - pan) * 0.5) * attenuation
        right = math.sqrt((1.0 + pan) * 0.5) * attenuation
        return left, right

    @classmethod
    def spatialize(cls, mono: Iterable[float], *, azimuth_deg: float, distance: float = 1.0) -> list[tuple[float, float]]:
        left, right = cls.gains(azimuth_deg, distance)
        return [(float(sample) * left, float(sample) * right) for sample in mono]

    @classmethod
    def play_tone(
        cls,
        *,
        frequency_hz: float = 440.0,
        duration_s: float = 0.16,
        azimuth_deg: float = 0.0,
        distance: float = 1.0,
        sample_rate: int = 22050,
    ) -> dict[str, Any]:
        try:
            import numpy as np
            import sounddevice as sd
        except Exception as exc:
            return {"success": False, "error": f"Optional audio backend unavailable: {exc}"}

        duration_s = max(0.03, min(float(duration_s), 1.5))
        frequency_hz = max(40.0, min(float(frequency_hz), 4000.0))
        count = max(1, int(sample_rate * duration_s))
        t = np.arange(count, dtype=np.float32) / float(sample_rate)
        envelope = np.minimum(1.0, np.minimum(t * 30.0, (duration_s - t) * 30.0))
        mono = np.sin(2.0 * np.pi * frequency_hz * t) * envelope * 0.12
        left, right = cls.gains(azimuth_deg, distance)
        stereo = np.column_stack((mono * left, mono * right)).astype(np.float32)
        try:
            sd.play(stereo, sample_rate, blocking=False)
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        return {"success": True, "azimuth_deg": float(azimuth_deg), "distance": float(distance)}


class OptimizationLearner:
    """Learns low-power preferences from telemetry without changing system settings."""

    PATH = ROOT / "optimization.json"
    MAX_SAMPLES = 720

    @classmethod
    def observe(cls, **telemetry: float | int | None) -> dict[str, Any]:
        with _STATE_LOCK:
            payload = _json_load(cls.PATH, {"samples": [], "last_sample": 0.0})
            now = time.monotonic()
            last = float(payload.get("last_sample", 0.0) or 0.0)

            # Adaptive sampling: 30s normally, 10s when load is materially changing.
            cpu = float(telemetry.get("cpu_percent") or 0.0)
            memory = float(telemetry.get("memory_percent") or 0.0)
            interval = 10.0 if cpu >= 75.0 or memory >= 85.0 else 30.0
            if last and now - last < interval:
                return {"success": True, "sampled": False, "next_sample_in_s": round(interval - (now - last), 2)}

            sample = {
                "timestamp": _now(),
                "cpu_percent": cpu,
                "memory_percent": memory,
                "battery_percent": telemetry.get("battery_percent"),
                "gpu_percent": telemetry.get("gpu_percent"),
            }
            samples = payload.get("samples", [])
            samples = [s for s in samples if isinstance(s, dict)]
            samples.append(sample)
            _json_save(cls.PATH, {"schema_version": 1, "samples": samples[-cls.MAX_SAMPLES:], "last_sample": now})
            return {"success": True, "sampled": True, "sample": sample, "recommendations": cls.recommendations(samples)}

    @staticmethod
    def recommendations(samples: list[dict[str, Any]]) -> list[str]:
        if not samples:
            return []
        recent = samples[-20:]
        cpu = statistics.mean(float(s.get("cpu_percent") or 0.0) for s in recent)
        mem = statistics.mean(float(s.get("memory_percent") or 0.0) for s in recent)
        latest = recent[-1]
        latest_cpu = float(latest.get("cpu_percent") or 0.0)
        latest_mem = float(latest.get("memory_percent") or 0.0)
        recommendations: list[str] = []
        if latest_cpu >= 80 or cpu >= 80:
            recommendations.append("Prefer lazy/on-demand background work.")
        if latest_mem >= 88 or mem >= 88:
            recommendations.append("Reduce retained caches and defer nonessential indexing.")
        if not recommendations and cpu < 25 and mem < 65 and latest_cpu < 25 and latest_mem < 65:
            recommendations.append("Current profile is comfortable for background learning.")
        return recommendations


class TimeMachine:
    """Persistent snapshots and deterministic state diffs."""

    ROOT = ROOT / "time_machine"

    @classmethod
    def save(cls, name: str, state: dict[str, Any], tags: Iterable[str] = ()) -> dict[str, Any]:
        clean = str(name or "").strip()
        if not clean or not isinstance(state, dict):
            return {"success": False, "error": "A snapshot name and JSON object state are required."}
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in clean)[:80] or uuid4().hex[:8]
        snapshot = {
            "schema_version": 1,
            "id": uuid4().hex[:12],
            "name": safe,
            "created_at": _now(),
            "tags": [str(tag)[:50] for tag in tags if str(tag).strip()],
            "state": state,
        }
        path = cls.ROOT / f"{safe}-{snapshot['id']}.json"
        _json_save(path, snapshot)
        return {"success": True, "snapshot": snapshot, "path": str(path)}

    @classmethod
    def list_snapshots(cls, limit: int = 50) -> list[dict[str, Any]]:
        cls.ROOT.mkdir(parents=True, exist_ok=True)
        files = sorted(cls.ROOT.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        result = []
        for path in files[:max(1, min(int(limit), 200))]:
            payload = _json_load(path, {})
            if isinstance(payload, dict):
                result.append({k: payload.get(k) for k in ("id", "name", "created_at", "tags")})
        return result

    @classmethod
    def _get(cls, selector: str) -> tuple[Path | None, dict[str, Any] | None]:
        selector = str(selector or "").strip()
        files = sorted(cls.ROOT.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files:
            payload = _json_load(path, {})
            if not isinstance(payload, dict):
                continue
            if payload.get("id") == selector or payload.get("name") == selector or path.name == selector:
                return path, payload
        return None, None

    @classmethod
    def diff(cls, first: str, second: str) -> dict[str, Any]:
        _, a = cls._get(first)
        _, b = cls._get(second)
        if not a or not b:
            return {"success": False, "error": "Snapshot not found."}

        def walk(x: Any, y: Any, path: str = "") -> list[dict[str, Any]]:
            if isinstance(x, dict) and isinstance(y, dict):
                keys = sorted(set(x) | set(y))
                changes = []
                for key in keys:
                    child = f"{path}.{key}" if path else str(key)
                    changes.extend(walk(x.get(key), y.get(key), child))
                return changes
            if x != y:
                return [{"path": path, "before": x, "after": y}]
            return []

        return {"success": True, "from": a.get("name"), "to": b.get("name"), "changes": walk(a.get("state"), b.get("state"))}

    @classmethod
    def restore(cls, selector: str) -> dict[str, Any]:
        _, payload = cls._get(selector)
        if not payload:
            return {"success": False, "error": "Snapshot not found."}
        return {
            "success": True,
            "snapshot": {k: payload.get(k) for k in ("id", "name", "created_at", "tags")},
            "state": payload.get("state", {}),
            "restore_mode": "explicit-apply",
        }


class RemoteComputeManager:
    """Remote execution through already-paired Brahma Connect computers.

    No arbitrary shell channel is introduced here. The existing gateway remains
    the authorization and capability gate for every remote request.
    """

    @staticmethod
    def _service():
        from brahma_connect.service import get_service

        return get_service(Path(__file__).resolve().parent.parent)

    @classmethod
    def computers(cls) -> list[dict[str, Any]]:
        try:
            devices = cls._service().list_devices()
        except Exception as exc:
            return [{"success": False, "error": str(exc)}]
        result = []
        for device in devices:
            if not isinstance(device, dict):
                continue
            platform_name = str(device.get("platform", "")).lower()
            name = str(device.get("name", "")).lower()
            if platform_name in {"windows", "pc", "desktop"} or any(word in name for word in ("pc", "desktop", "laptop", "computer")):
                result.append(device)
        return result

    @classmethod
    def route(cls, target: str, action: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        target = str(target or "").strip()
        action = str(action or "").strip()
        if not target or not action:
            return {"success": False, "error": "target and action are required"}
        try:
            return asyncio_run(
                cls._service().route_command(
                    target,
                    action,
                    dict(parameters or {}),
                )
            )
        except Exception as exc:
            return {"success": False, "error": str(exc)}


def asyncio_run(awaitable):
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    # A synchronous capability may still be invoked from an async caller. Run the
    # coroutine on a dedicated event-loop thread rather than nesting loops in the
    # same thread, which asyncio forbids.
    holder: dict[str, Any] = {}
    def _runner() -> None:
        try:
            holder["result"] = asyncio.run(awaitable)
        except BaseException as exc:
            holder["error"] = exc

    worker = threading.Thread(target=_runner, name="BrahmaCapabilityAsyncBridge", daemon=False)
    worker.start()
    worker.join()
    if "error" in holder:
        raise holder["error"]
    return holder.get("result")


class PhoneLinkBridge:
    """Bridge to the installed Phone Link surface plus authorized Brahma devices."""

    APP_ID = "Microsoft.YourPhone_8wekyb3d8bbwe!App"

    @staticmethod
    def _windows() -> bool:
        return platform.system() == "Windows"

    @classmethod
    def installed(cls) -> bool:
        if not cls._windows():
            return False
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-AppxPackage -Name Microsoft.YourPhone | Select-Object -First 1 -ExpandProperty PackageFullName"],
                capture_output=True, text=True, timeout=4.0, creationflags=creationflags,
            )
            return bool(proc.stdout.strip())
        except Exception:
            return False

    @classmethod
    def status(cls) -> dict[str, Any]:
        computers = []
        phones = []
        service_error = ""
        try:
            service = cls._service()
            devices = service.list_devices()
            phones = [d for d in devices if str(d.get("platform", "")).lower() in {"android", "ios"}]
            computers = [d for d in devices if str(d.get("platform", "")).lower() in {"windows", "pc", "desktop"}]
        except Exception as exc:
            service_error = str(exc) or exc.__class__.__name__

        result = {
            "success": not bool(service_error),
            "phone_link_installed": cls.installed(),
            "paired_phones": phones,
            "paired_computers": computers,
            "bridge": "phone-link-surface + Brahma-Connect-device-transport",
            "microsoft_client_api": False,
        }
        if service_error:
            result["error"] = f"Unable to read paired device status: {service_error}"
        return result

    @staticmethod
    def _service():
        from brahma_connect.service import get_service

        return get_service(Path(__file__).resolve().parent.parent)

    @classmethod
    def open(cls) -> dict[str, Any]:
        if not cls._windows():
            return {"success": False, "error": "Phone Link is Windows-only."}
        if not cls.installed():
            return {"success": False, "error": "Microsoft Phone Link is not installed."}
        try:
            subprocess.Popen(
                ["explorer.exe", f"shell:AppsFolder\\{cls.APP_ID}"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return {"success": True, "opened": True}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    @classmethod
    def route(cls, target: str, action: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            devices = cls._service().resolve_devices(target)
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        phones = [d for d in devices if str(d.get("platform", "")).lower() in {"android", "ios"}]
        if len(phones) != 1:
            return {
                "success": False,
                "error": "Expected exactly one paired phone/tablet target.",
                "matches": phones,
            }
        return asyncio_run(cls._service().route_command(phones[0]["device_id"], action, parameters or {}))


class Life360Provider:
    """Read authorized Life360 device_trackers exposed by Home Assistant.

    This intentionally does not log into Life360 or bypass Circle sharing.
    """

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        base_url: str | None = None,
        token: str | None = None,
        entity_ids: Iterable[str] | None = None,
        timeout: float = 4.0,
    ) -> None:
        raw_enabled = os.getenv("JARVIS_LIFE360_ENABLED", "0").strip().lower()
        self.enabled = bool(enabled if enabled is not None else raw_enabled in {"1", "true", "yes", "on"})
        self.base_url = str(base_url or os.getenv("JARVIS_LIFE360_HA_URL", "http://127.0.0.1:8123")).strip().rstrip("/")
        self.token = str(token or os.getenv("JARVIS_LIFE360_HA_TOKEN", "")).strip()
        raw_entities = entity_ids if entity_ids is not None else os.getenv("JARVIS_LIFE360_ENTITY_IDS", "").split(",")
        self.entity_ids = {str(x).strip() for x in raw_entities if str(x).strip()}
        self.timeout = max(0.5, min(float(timeout), 15.0))

    @staticmethod
    def _host_allowed(host: str) -> bool:
        host = host.strip().lower().rstrip(".")
        if host in {"localhost", "homeassistant"} or host.endswith(".local"):
            return True
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(host, None)}
        except OSError:
            return False
        if not addresses:
            return False
        for address in addresses:
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError:
                return False
            if not (parsed.is_private or parsed.is_loopback or parsed.is_link_local):
                return False
        return True

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "configured": bool(self.token),
            "ha_url": self.base_url,
            "entity_allowlist_count": len(self.entity_ids),
        }

    def _request_json(self, path: str) -> Any:
        from urllib.parse import urlsplit

        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or not parsed.hostname:
            raise ValueError("Life360 Home Assistant URL must be a credential-free HTTP(S) URL.")
        if not self._host_allowed(parsed.hostname):
            raise ValueError("Life360 Home Assistant transport must resolve to a local/private host.")
        url = self.base_url + "/" + path.lstrip("/")
        req = urlrequest.Request(url, headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"})
        with urlrequest.urlopen(req, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _point(attrs: dict[str, Any]) -> tuple[float, float] | None:
        try:
            lat = float(attrs.get("latitude"))
            lon = float(attrs.get("longitude"))
        except (TypeError, ValueError):
            return None
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            return None
        return lat, lon

    def locations(self) -> list[dict[str, Any]]:
        if not self.enabled or not self.token:
            return []
        try:
            _ = self._request_json("/api/")
            states = self._request_json("/api/states")
        except (OSError, ValueError, urlerror.URLError, json.JSONDecodeError):
            return []
        if not isinstance(states, list):
            return []

        locations = []
        for item in states:
            if not isinstance(item, dict) or not str(item.get("entity_id", "")).startswith("device_tracker."):
                continue
            entity_id = str(item["entity_id"])
            attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
            attribution = str(attrs.get("attribution") or attrs.get("source") or "").lower()
            explicit = entity_id in self.entity_ids
            inferred = "life360" in attribution or "life360" in entity_id.lower()
            if self.entity_ids and not explicit:
                continue
            if not self.entity_ids and not inferred:
                continue
            point = self._point(attrs)
            if point is None:
                continue
            locations.append({
                "id": entity_id,
                "label": str(attrs.get("friendly_name") or entity_id)[:120],
                "point": {"latitude": point[0], "longitude": point[1]},
                "authorized": True,
                "kind": "family",
                "source": "life360",
                "state": str(item.get("state") or "unknown")[:40],
                "accuracy_m": self._float(attrs.get("gps_accuracy")),
                "battery_percent": self._float(attrs.get("battery_level")),
                "address": str(attrs.get("address") or attrs.get("street") or "")[:200],
                "place": str(attrs.get("place") or attrs.get("zone") or "")[:120],
                "last_seen": str(attrs.get("last_seen") or attrs.get("timestamp") or "")[:80],
                "driving": self._bool(attrs.get("driving")),
                "charging": self._bool(attrs.get("charging")),
            })
        return locations

    @staticmethod
    def _float(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _bool(value: Any) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}


class GodsEyeExpansion:
    """Merge current-location, connected-device, and authorized family feeds."""

    @classmethod
    def snapshot(cls) -> dict[str, Any]:
        from core.gods_eye import GodsEye

        eye = GodsEye()
        family_provider = Life360Provider()
        family = family_provider.locations()
        devices = eye.provider_locations()
        current = eye._current_snapshot().as_dict()
        return {
            "schema_version": 1,
            "surface": "gods-eye",
            "current": current,
            "connected_devices": devices,
            "family": family,
            "family_provider": family_provider.status(),
            "counts": {
                "current": 1 if current.get("point") else 0,
                "connected_devices": len(devices),
                "family": len(family),
            },
        }

    @classmethod
    def globe_payload(cls) -> dict[str, Any]:
        from core.gods_eye import GodsEye

        return GodsEye().globe_payload()


def execute_selected_capability(name: str, action: str, **kwargs: Any) -> dict[str, Any]:
    name = str(name or "").strip().lower()
    action = str(action or "status").strip().lower()

    if name == "continuous_learning":
        if action == "record":
            return LearningEngine.record(
                kwargs.get("signal", ""),
                outcome=kwargs.get("outcome", ""),
                score=kwargs.get("score"),
                context=kwargs.get("context"),
            )
        if action == "feedback":
            return LearningEngine.feedback(kwargs.get("rule", ""), kwargs.get("category", "general"))
        return {"success": True, "events": LearningEngine.recent(kwargs.get("limit", 25))}

    if name == "advanced_analysis":
        if action == "events":
            return AdvancedAnalyzer.analyze_events(kwargs.get("events", []), kwargs.get("key", "value"))
        return AdvancedAnalyzer.analyze(kwargs.get("values", []))

    if name == "spatial_audio":
        if action == "play":
            return SpatialAudioEngine.play_tone(
                frequency_hz=kwargs.get("frequency_hz", 440),
                duration_s=kwargs.get("duration_s", 0.16),
                azimuth_deg=kwargs.get("azimuth_deg", 0),
                distance=kwargs.get("distance", 1),
                sample_rate=kwargs.get("sample_rate", 22050),
            )
        samples = kwargs.get("samples", [])
        return {"success": True, "gains": SpatialAudioEngine.gains(kwargs.get("azimuth_deg", 0), kwargs.get("distance", 1)),
                "stereo": SpatialAudioEngine.spatialize(samples, azimuth_deg=kwargs.get("azimuth_deg", 0), distance=kwargs.get("distance", 1))}

    if name == "optimization_learning":
        if action == "recommendations":
            payload = _json_load(OptimizationLearner.PATH, {"samples": []})
            return {"success": True, "recommendations": OptimizationLearner.recommendations(payload.get("samples", []))}
        return OptimizationLearner.observe(**kwargs)

    if name == "remote_computing":
        if action == "list":
            return {"success": True, "computers": RemoteComputeManager.computers()}
        return RemoteComputeManager.route(kwargs.get("target", ""), kwargs.get("remote_action", kwargs.get("action_name", "")), kwargs.get("parameters", {}))

    if name == "time_machine":
        if action == "save":
            return TimeMachine.save(kwargs.get("name", ""), kwargs.get("state", {}), kwargs.get("tags", []))
        if action == "list":
            return {"success": True, "snapshots": TimeMachine.list_snapshots(kwargs.get("limit", 50))}
        if action == "diff":
            return TimeMachine.diff(kwargs.get("first", ""), kwargs.get("second", ""))
        if action == "restore":
            return TimeMachine.restore(kwargs.get("selector", ""))
        return {"success": True, "snapshots": TimeMachine.list_snapshots(25)}

    if name == "gods_eye_expansion":
        if action == "globe":
            return GodsEyeExpansion.globe_payload()
        return GodsEyeExpansion.snapshot()

    if name == "phone_link_bridge":
        if action == "open":
            return PhoneLinkBridge.open()
        if action == "route":
            return PhoneLinkBridge.route(kwargs.get("target", "phone"), kwargs.get("remote_action", kwargs.get("action_name", "")), kwargs.get("parameters", {}))
        return PhoneLinkBridge.status()

    if name == "life360_family":
        provider = Life360Provider()
        if action == "status":
            return {"success": True, "provider": provider.status()}
        return {"success": True, "locations": provider.locations(), "provider": provider.status()}

    return {"success": False, "error": f"Unknown selected capability: {name}"}
