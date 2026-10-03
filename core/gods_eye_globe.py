"""God’s Eye structured payload for Brahma Evo’s existing 3D globe.

Based on the verified JARVIS God’s Eye globe contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GlobeLocator:
    id: str
    label: str
    latitude: float
    longitude: float
    kind: str
    authorized: bool
    accuracy_m: float | None = None
    source: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "kind": self.kind,
            "authorized": self.authorized,
            "accuracy_m": self.accuracy_m,
            "source": self.source,
        }


def _point(raw: object) -> tuple[float, float] | None:
    if not isinstance(raw, dict):
        return None
    point = raw.get("point")
    if not isinstance(point, dict):
        return None
    try:
        lat = float(point["latitude"])
        lon = float(point["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


def _slug(value: str, limit: int = 70) -> str:
    return (
        value.casefold()
        .replace(" ", "-")
        .replace("/", "-")
        .replace("\\", "-")[:limit]
    )


def build_globe_payload(eye: Any) -> dict[str, object]:
    current = eye._current_snapshot()
    current_raw = current.as_dict()

    locators: list[GlobeLocator] = []
    current_point = _point(current_raw)
    if current.permitted and current_point is not None:
        locators.append(
            GlobeLocator(
                "current",
                "Current location",
                current_point[0],
                current_point[1],
                "current",
                True,
                current.accuracy_m,
                current.source,
            )
        )

    for index, item in enumerate(eye.provider_locations()):
        point = _point(item)
        if point is None or not bool(item.get("authorized", True)):
            continue
        label = str(item.get("label") or item.get("name") or "Connected Device")[:120]
        kind = str(item.get("kind") or "device")[:40]
        locators.append(
            GlobeLocator(
                f"{kind}:{index}:{_slug(label, 50)}",
                label,
                point[0],
                point[1],
                kind,
                True,
                item.get("accuracy_m"),
                str(item.get("source") or kind)[:80],
            )
        )

    family_locations = eye.family_locations()
    for index, item in enumerate(family_locations):
        point = _point(item)
        if point is None or not bool(item.get("authorized", True)):
            continue
        label = str(item.get("label") or item.get("name") or "Family")[:120]
        locators.append(
            GlobeLocator(
                f"family:{index}:{_slug(label, 50)}",
                label,
                point[0],
                point[1],
                "family",
                True,
                item.get("accuracy_m"),
                "life360",
            )
        )

    for item in eye.saved_locations():
        point = _point(item)
        if point is None:
            continue
        label = str(item.get("name") or "Saved location")[:120]
        locators.append(
            GlobeLocator(
                "saved:" + _slug(label, 90),
                label,
                point[0],
                point[1],
                "saved",
                True,
                item.get("accuracy_m"),
                str(item.get("source") or "saved")[:80],
            )
        )

    current_authorized = bool(current.permitted and current_point is not None)
    provider_count = len(eye.provider_locations()) + len(family_locations)
    if current_authorized:
        sensor_state = "current-live"
    elif provider_count:
        sensor_state = "authorized-feeds-live"
    elif current.source or current_raw:
        sensor_state = "feeds-unavailable-or-unauthorized"
    else:
        sensor_state = "sensor-status-unavailable"

    return {
        "schema_version": 3,
        "surface": "gods-eye",
        "authorized_current": current_authorized,
        "current": current_raw,
        "sensor_state": sensor_state,
        "current_source": current.source,
        "current_accuracy_m": current.accuracy_m,
        "provider_status": [eye.family_provider_status()],
        "connected_device_count": len(eye.provider_locations()),
        "family_count": len(family_locations),
        "locator_count": len(locators),
        "locators": [item.as_dict() for item in locators],
    }
