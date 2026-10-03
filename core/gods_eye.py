"""God's Eye location context for Brahma Evo.

Adapted from the verified JARVIS God’s Eye contract. The globe consumes only
structured, validated coordinates and never treats missing provider data as
known location data.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GeoPoint:
    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if not -90.0 <= self.latitude <= 90.0:
            raise ValueError("latitude must be between -90 and 90")
        if not -180.0 <= self.longitude <= 180.0:
            raise ValueError("longitude must be between -180 and 180")

    def as_dict(self) -> dict[str, float]:
        return {"latitude": self.latitude, "longitude": self.longitude}


@dataclass(frozen=True)
class LocationSnapshot:
    point: GeoPoint | None
    accuracy_m: float | None
    permitted: bool
    source: str

    def __post_init__(self) -> None:
        if self.accuracy_m is not None and self.accuracy_m < 0:
            raise ValueError("accuracy_m cannot be negative")

    def as_dict(self) -> dict[str, object]:
        return {
            "point": self.point.as_dict() if self.point else None,
            "accuracy_m": self.accuracy_m,
            "permitted": self.permitted,
            "source": self.source,
        }


def _point(raw: object) -> GeoPoint | None:
    if not isinstance(raw, dict):
        return None
    value = raw.get("point") if isinstance(raw.get("point"), dict) else raw
    if not isinstance(value, dict):
        return None
    try:
        lat = float(value.get("latitude", value.get("lat")))
        lon = float(value.get("longitude", value.get("lon")))
    except (TypeError, ValueError):
        return None
    try:
        return GeoPoint(lat, lon)
    except ValueError:
        return None


class GodsEye:
    """Lightweight God’s Eye service boundary used by the 3D globe."""

    def __init__(self, config_dir: Path | None = None) -> None:
        from core.user_paths import get_user_data_dir

        self.config_dir = Path(config_dir or (get_user_data_dir() / "config"))
        self._connect_service = None

    def _current_snapshot(self) -> LocationSnapshot:
        try:
            from core.device_location import get_device_location

            raw = get_device_location()
            point = _point(raw)
            permitted = bool(
                isinstance(raw, dict)
                and raw.get("status") == "success"
                and point is not None
            )
            return LocationSnapshot(
                point if permitted else None,
                self._accuracy(raw),
                permitted,
                str((raw or {}).get("source", "device-location"))[:80],
            )
        except Exception as exc:
            return LocationSnapshot(None, None, False, f"unavailable:{type(exc).__name__}")

    @staticmethod
    def _accuracy(raw: object) -> float | None:
        if not isinstance(raw, dict):
            return None
        try:
            value = raw.get("accuracy_m")
            return float(value) if value is not None and float(value) >= 0 else None
        except (TypeError, ValueError):
            return None

    def _connect_devices(self) -> list[dict[str, Any]]:
        try:
            if self._connect_service is None:
                from brahma_connect.service import get_service
                base_dir = Path(__file__).resolve().parent.parent
                self._connect_service = get_service(base_dir)
            return self._connect_service.list_devices()
        except Exception:
            return []

    @staticmethod
    def _metadata_location(device: dict[str, Any]) -> dict[str, Any] | None:
        metadata = device.get("metadata")
        if not isinstance(metadata, dict):
            return None
        raw = (
            metadata.get("location")
            or metadata.get("coordinates")
            or metadata.get("geo")
        )
        if isinstance(raw, dict):
            return raw
        if any(key in metadata for key in ("latitude", "longitude", "lat", "lon")):
            return metadata
        return None

    def provider_locations(self) -> list[dict[str, Any]]:
        """Expose authorized device locations already supplied to Brahma Connect.

        No location is invented from an IP/device name. Providers must supply
        validated coordinates explicitly in device metadata.
        """
        locations: list[dict[str, Any]] = []
        for device in self._connect_devices():
            raw = self._metadata_location(device)
            point = _point(raw)
            if point is None:
                continue
            authorized = bool(
                isinstance(raw, dict)
                and raw.get("authorized", device.get("location_authorized", True))
            )
            if not authorized:
                continue
            locations.append({
                "label": str(
                    (raw or {}).get("label")
                    or (raw or {}).get("name")
                    or device.get("name")
                    or "Connected Device"
                )[:120],
                "point": point.as_dict(),
                "authorized": True,
                "accuracy_m": self._accuracy(raw),
                "source": str((raw or {}).get("source") or "brahma-connect")[:80],
                "kind": str((raw or {}).get("kind") or self._kind_for_device(device)),
            })
        return locations

    @staticmethod
    def _kind_for_device(device: dict[str, Any]) -> str:
        platform = str(device.get("platform", "")).lower()
        name = str(device.get("name", "")).lower()
        if "phone" in name or platform in {"android", "ios"}:
            return "phone"
        if "tablet" in name:
            return "device"
        return "device"

    def saved_locations(self) -> list[dict[str, Any]]:
        """Read explicitly saved coordinate records, when present."""
        candidates: list[Any] = []
        path = self.config_dir / "app_settings.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            for key in ("saved_locations", "locations"):
                value = payload.get(key) if isinstance(payload, dict) else None
                if isinstance(value, list):
                    candidates.extend(value)
        except Exception:
            pass

        result: list[dict[str, Any]] = []
        for item in candidates:
            if not isinstance(item, dict):
                continue
            point = _point(item)
            if point is None:
                continue
            result.append({
                "name": str(item.get("name") or item.get("label") or "Saved location")[:120],
                "point": point.as_dict(),
                "accuracy_m": self._accuracy(item),
                "source": str(item.get("source") or "saved")[:80],
            })
        return result

    def globe_payload(self) -> dict[str, object]:
        from core.gods_eye_globe import build_globe_payload

        return build_globe_payload(self)
