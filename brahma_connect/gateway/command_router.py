from __future__ import annotations

import asyncio
from typing import Any

from .capability_manager import CapabilityManager
from .device_manager import DeviceManager
from .protocol import ProtocolTypes, build_message, new_request_id
from .websocket import ConnectionHub


ACTION_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "file_list": ("files",),
    "file_read": ("files",),
    "file_write": ("files",),
    "file_delete": ("files",),
    "ui_dump": ("ui_control",),
    "ui_tap": ("ui_control",),
    "ui_swipe": ("ui_control",),
    "ui_type": ("ui_control",),
    "unlock_phone": ("unlock_phone",),
}


class CommandRouter:
    def __init__(self, device_manager: DeviceManager, hub: ConnectionHub, capability_manager: CapabilityManager):
        self.device_manager = device_manager
        self.hub = hub
        self.capability_manager = capability_manager

    async def route(self, target: str, action: str, parameters: dict[str, Any] | None = None, *, timeout: float = 30.0) -> dict[str, Any]:
        parameters = dict(parameters or {})
        matches = self.device_manager.resolve(target)
        if not matches:
            return {
                "success": False,
                "device": target,
                "action": action,
                "error": f"No device matches '{target}'.",
                "error_code": "DEVICE_NOT_FOUND",
            }
        if len(matches) > 1:
            return {
                "success": False,
                "device": target,
                "action": action,
                "error": "Multiple devices matched the request.",
                "error_code": "MULTIPLE_DEVICES",
                "matches": [item.to_dict() for item in matches],
            }
        device = matches[0]
        if device.revoked:
            return {
                "success": False,
                "device": device.device_id,
                "action": action,
                "error": f"Device '{device.name}' has been revoked.",
                "error_code": "DEVICE_REVOKED",
            }
        if not device.online:
            return {
                "success": False,
                "device": device.device_id,
                "action": action,
                "error": f"Your {device.name} is currently offline.",
                "error_code": "DEVICE_OFFLINE",
            }

        requested_capabilities = parameters.pop("required_capabilities", [])
        canonical_capabilities = ACTION_CAPABILITIES.get(str(action or "").strip().lower(), ())
        required = list(canonical_capabilities)
        if isinstance(requested_capabilities, str):
            requested_capabilities = [requested_capabilities]
        for capability in list(requested_capabilities or []):
            if capability not in required:
                required.append(capability)
        missing = self.capability_manager.missing(device.capabilities, required)
        if missing:
            return {
                "success": False,
                "device": device.device_id,
                "action": action,
                "error": f"That device does not support: {', '.join(missing)}.",
                "error_code": "CAPABILITY_MISSING",
                "missing": missing,
            }

        request_id = new_request_id()
        payload = {
            "device": device.device_id,
            "action": action,
            "parameters": parameters,
        }
        message = build_message(ProtocolTypes.EXECUTE, payload, request_id=request_id)
        future = await self.hub.set_pending(device.device_id, request_id)
        if future is None:
            return {
                "success": False,
                "device": device.device_id,
                "action": action,
                "request_id": request_id,
                "error": f"Unable to send command to {device.name}.",
                "error_code": "DEVICE_UNAVAILABLE",
            }
        try:
            sent = await self.hub.send_to_device(device.device_id, message)
        except Exception as exc:
            await self.hub.reject_pending(device.device_id, request_id, str(exc))
            return {
                "success": False,
                "device": device.device_id,
                "action": action,
                "request_id": request_id,
                "error": str(exc),
                "error_code": "ROUTER_ERROR",
            }
        if not sent:
            await self.hub.reject_pending(
                device.device_id,
                request_id,
                f"Unable to send command to {device.name}.",
            )
            return {
                "success": False,
                "device": device.device_id,
                "action": action,
                "request_id": request_id,
                "error": f"Unable to send command to {device.name}.",
                "error_code": "DEVICE_UNAVAILABLE",
            }
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.CancelledError:
            await self.hub.reject_pending(device.device_id, request_id, "Command cancelled.")
            raise
        except asyncio.TimeoutError:
            await self.hub.reject_pending(
                device.device_id,
                request_id,
                f"Timed out waiting for {device.name}.",
            )
            return {
                "success": False,
                "device": device.device_id,
                "action": action,
                "request_id": request_id,
                "error": f"Timed out waiting for {device.name}.",
                "error_code": "TIMEOUT",
            }
        except Exception as exc:
            await self.hub.reject_pending(device.device_id, request_id, str(exc))
            return {
                "success": False,
                "device": device.device_id,
                "action": action,
                "request_id": request_id,
                "error": str(exc),
                "error_code": "ROUTER_ERROR",
            }
        if isinstance(result, dict):
            result.setdefault("success", True)
            result.setdefault("device", device.device_id)
            result.setdefault("action", action)
            result.setdefault("request_id", request_id)
            return result
        return {
            "success": True,
            "device": device.device_id,
            "action": action,
            "request_id": request_id,
            "data": result,
        }
