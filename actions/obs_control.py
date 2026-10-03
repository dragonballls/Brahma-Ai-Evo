
"""OBS Studio control for Brahma Evo via obs-websocket 5.x."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import uuid
from typing import Any

from core.user_paths import get_user_data_dir

try:
    from memory import config_manager
except Exception:
    config_manager = None

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4455
ENV_PASSWORD = "BRAHMA_OBS_WEBSOCKET_PASSWORD"
PASSWORD_KEY = "obs_websocket_password_protected"

_LOCK = threading.RLock()
_CLIENT = None
_SIGNATURE = None


class OBSControlError(RuntimeError):
    pass


def _cfg() -> tuple[str, int, str]:
    host, port, protected = DEFAULT_HOST, DEFAULT_PORT, ""
    if config_manager is not None:
        try:
            host = str(config_manager.get_setting("obs_websocket_host", host) or host)
            port = int(config_manager.get_setting("obs_websocket_port", port) or port)
            protected = str(config_manager.get_setting(PASSWORD_KEY, "") or "")
        except Exception:
            pass
    return host, port, protected


def _protect(password: str) -> str:
    if not password or os.name != "nt":
        return ""
    try:
        import win32crypt
        blob = win32crypt.CryptProtectData(
            password.encode("utf-8"), "Brahma OBS", None, None, None, 0
        )
        return "dpapi:" + base64.b64encode(blob).decode("ascii")
    except Exception:
        return ""


def _unprotect(protected: str) -> str:
    if not protected or not protected.startswith("dpapi:") or os.name != "nt":
        return ""
    try:
        import win32crypt
        blob = base64.b64decode(protected[6:])
        return win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1].decode("utf-8")
    except Exception:
        return ""


def _password() -> str:
    env = os.environ.get(ENV_PASSWORD, "")
    if env:
        return env
    return _unprotect(_cfg()[2])


def _save(host: str, port: int, password: str | None = None) -> None:
    values: dict[str, Any] = {
        "obs_websocket_host": host,
        "obs_websocket_port": port,
    }
    if password is not None:
        protected = _protect(password)
        if password and not protected and os.name == "nt":
            raise OBSControlError(
                "Brahma could not protect the OBS password with Windows DPAPI. "
                "Use the BRAHMA_OBS_WEBSOCKET_PASSWORD environment variable instead."
            )
        values[PASSWORD_KEY] = protected
    if config_manager is None:
        raise OBSControlError("Brahma configuration storage is unavailable.")
    config_manager.save_settings(values)


def _websocket_module():
    try:
        import websocket
        return websocket
    except ImportError as exc:
        raise OBSControlError(
            "OBS control requires websocket-client. Install Brahma's requirements first."
        ) from exc


def _auth(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(
        hashlib.sha256((password + salt).encode("utf-8")).digest()
    ).decode("ascii")
    return base64.b64encode(
        hashlib.sha256((secret + challenge).encode("utf-8")).digest()
    ).decode("ascii")


class OBSWebSocketClient:
    def __init__(self, host: str, port: int, password: str, timeout: float = 4.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self.ws = None
        self.counter = 0

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    def connect(self) -> None:
        if self.ws is not None:
            return
        wsmod = _websocket_module()
        try:
            self.ws = wsmod.create_connection(self.url, timeout=self.timeout)
            hello = json.loads(self.ws.recv())
            if hello.get("op") != 0:
                raise OBSControlError("OBS did not return a WebSocket Hello message.")
            data = hello.get("d") or {}
            identify: dict[str, Any] = {"rpcVersion": int(data.get("rpcVersion") or 1)}
            authentication = data.get("authentication") or {}
            if authentication:
                if not self.password:
                    raise OBSControlError(
                        "OBS WebSocket authentication is enabled. Configure its password for Brahma."
                    )
                identify["authentication"] = _auth(
                    self.password,
                    str(authentication.get("salt") or ""),
                    str(authentication.get("challenge") or ""),
                )
            self.ws.send(json.dumps({"op": 1, "d": identify}))
            identified = json.loads(self.ws.recv())
            if identified.get("op") != 2:
                raise OBSControlError("OBS WebSocket authentication failed.")
        except OBSControlError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise OBSControlError(f"Could not connect to OBS at {self.url}: {exc}") from exc

    def close(self) -> None:
        try:
            if self.ws is not None:
                self.ws.close()
        except Exception:
            pass
        self.ws = None

    def request(self, request_type: str, request_data: dict[str, Any] | None = None) -> dict[str, Any]:
        self.connect()
        assert self.ws is not None
        self.counter += 1
        request_id = f"brahma-{self.counter}-{uuid.uuid4().hex[:8]}"
        self.ws.send(json.dumps({
            "op": 6,
            "d": {
                "requestType": request_type,
                "requestId": request_id,
                "requestData": request_data or {},
            },
        }))
        try:
            while True:
                message = json.loads(self.ws.recv())
                if message.get("op") != 7:
                    continue
                data = message.get("d") or {}
                if str(data.get("requestId")) != request_id:
                    continue
                status = data.get("requestStatus") or {}
                if not status.get("result"):
                    raise OBSControlError(
                        f"OBS rejected {request_type}: "
                        f"{status.get('comment') or status.get('code') or 'unknown error'}"
                    )
                return data.get("responseData") or {}
        except OBSControlError:
            raise
        except Exception as exc:
            self.close()
            raise OBSControlError(f"OBS request failed: {exc}") from exc


def _client() -> OBSWebSocketClient:
    global _CLIENT, _SIGNATURE
    host, port, _ = _cfg()
    signature = (host, port)
    with _LOCK:
        if _CLIENT is None or _SIGNATURE != signature:
            if _CLIENT is not None:
                _CLIENT.close()
            _CLIENT = OBSWebSocketClient(host, port, _password())
            _SIGNATURE = signature
        else:
            _CLIENT.password = _password()
        return _CLIENT


def configure(
    host: str | None = None,
    port: int | str | None = None,
    password: str | None = None,
    clear_password: bool = False,
) -> dict[str, Any]:
    old_host, old_port, _ = _cfg()
    new_host = str(host or old_host or DEFAULT_HOST).strip() or DEFAULT_HOST
    try:
        new_port = int(port or old_port or DEFAULT_PORT)
    except (TypeError, ValueError) as exc:
        raise OBSControlError("OBS WebSocket port must be numeric.") from exc
    if not 1 <= new_port <= 65535:
        raise OBSControlError("OBS WebSocket port must be between 1 and 65535.")
    if clear_password:
        _save(new_host, new_port, "")
    elif password is not None:
        _save(new_host, new_port, str(password))
    else:
        _save(new_host, new_port)

    global _CLIENT, _SIGNATURE
    with _LOCK:
        if _CLIENT is not None:
            _CLIENT.close()
        _CLIENT = None
        _SIGNATURE = None

    current = _client().request("GetCurrentProgramScene")
    return {
        "ok": True,
        "host": new_host,
        "port": new_port,
        "connected": True,
        "current_scene": current.get("currentProgramSceneName"),
        "password_configured": bool(_password()),
    }


def _items(client: OBSWebSocketClient, scene: str | None) -> list[dict[str, Any]]:
    data = client.request("GetSceneItemList", {"sceneName": scene} if scene else {})
    return list(data.get("sceneItems") or [])


def _find_item(items: list[dict[str, Any]], source: str) -> dict[str, Any]:
    target = source.strip().lower()
    for item in items:
        if str(item.get("sourceName") or "").lower() == target:
            return item
    for item in items:
        name = str(item.get("sourceName") or "").lower()
        if target in name or name in target:
            return item
    raise OBSControlError(f"OBS source '{source}' was not found.")


def _preset_position(position: str, transform: dict[str, Any]) -> tuple[float, float]:
    key = position.strip().lower().replace("_", "-").replace(" ", "-")
    if key not in {"top-left", "top-right", "bottom-left", "bottom-right", "center"}:
        raise OBSControlError(
            "Position must be top-left, top-right, bottom-left, bottom-right, or center."
        )
    canvas_w = float(transform.get("canvasWidth") or 1920)
    canvas_h = float(transform.get("canvasHeight") or 1080)
    width = float(transform.get("width") or transform.get("sourceWidth") or 0)
    height = float(transform.get("height") or transform.get("sourceHeight") or 0)
    pad_x = canvas_w * 0.035
    pad_y = canvas_h * 0.035
    anchor = int(transform.get("alignment") or 0)

    horizontal = "center"
    vertical = "center"
    if anchor & 1:
        horizontal = "left"
    elif anchor & 2:
        horizontal = "right"
    if anchor & 4:
        vertical = "top"
    elif anchor & 8:
        vertical = "bottom"

    if key.endswith("left"):
        x = pad_x if horizontal == "left" else pad_x + width / 2
    elif key.endswith("right"):
        x = canvas_w - pad_x if horizontal == "right" else canvas_w - pad_x - width / 2
    else:
        x = canvas_w / 2
    if key.startswith("top"):
        y = pad_y if vertical == "top" else pad_y + height / 2
    elif key.startswith("bottom"):
        y = canvas_h - pad_y if vertical == "bottom" else canvas_h - pad_y - height / 2
    else:
        y = canvas_h / 2
    return x, y


def run(parameters: dict[str, Any] | None = None, player=None, speak=None) -> str:
    args = dict(parameters or {})
    action = str(args.get("action") or "status").strip().lower()

    if action == "configure":
        return json.dumps(configure(
            host=args.get("host"),
            port=args.get("port"),
            password=args.get("password"),
            clear_password=bool(args.get("clear_password", False)),
        ), ensure_ascii=False)

    client = _client()

    if action == "status":
        version = client.request("GetVersion")
        scene = client.request("GetCurrentProgramScene")
        scenes = client.request("GetSceneList")
        stream = client.request("GetStreamStatus")
        record = client.request("GetRecordStatus")
        return json.dumps({
            "connected": True,
            "host": client.host,
            "port": client.port,
            "obs_version": version.get("obsVersion"),
            "obs_websocket_version": version.get("obsWebSocketVersion"),
            "current_scene": scene.get("currentProgramSceneName"),
            "scenes": [x.get("sceneName") for x in scenes.get("scenes", [])],
            "streaming": bool(stream.get("outputActive")),
            "recording": bool(record.get("outputActive")),
        }, ensure_ascii=False)

    if action == "scene_list":
        return json.dumps(client.request("GetSceneList"), ensure_ascii=False)
    if action == "scene_current":
        return json.dumps(client.request("GetCurrentProgramScene"), ensure_ascii=False)

    if action in {"scene_switch", "scene_create", "scene_remove", "scene_rename"}:
        scene = str(args.get("scene") or args.get("scene_name") or "").strip()
        if not scene:
            raise OBSControlError("Tell me the OBS scene name.")
        if action == "scene_switch":
            client.request("SetCurrentProgramScene", {"sceneName": scene})
            return f"Switched OBS to '{scene}'."
        if action == "scene_create":
            client.request("CreateScene", {"sceneName": scene})
            return f"Created OBS scene '{scene}'."
        if action == "scene_remove":
            client.request("RemoveScene", {"sceneName": scene})
            return f"Removed OBS scene '{scene}'."
        new_name = str(args.get("new_name") or "").strip()
        if not new_name:
            raise OBSControlError("Tell me the new scene name.")
        client.request("SetSceneName", {"sceneName": scene, "newSceneName": new_name})
        return f"Renamed OBS scene '{scene}' to '{new_name}'."

    if action == "source_list":
        scene = str(args.get("scene") or "").strip() or None
        return json.dumps({"scene": scene, "items": _items(client, scene)}, ensure_ascii=False)

    if action in {
        "source_show", "source_hide", "source_move", "source_resize",
        "source_rotate", "source_text",
    }:
        source = str(args.get("source") or args.get("source_name") or "").strip()
        if not source:
            raise OBSControlError("Tell me which OBS source to control.")
        scene = str(args.get("scene") or "").strip() or None
        items = _items(client, scene)
        item = _find_item(items, source)
        current_scene = scene or client.request("GetCurrentProgramScene").get("currentProgramSceneName")
        item_id = int(item["sceneItemId"])

        if action in {"source_show", "source_hide"}:
            enabled = action == "source_show"
            client.request("SetSceneItemEnabled", {
                "sceneName": current_scene,
                "sceneItemId": item_id,
                "sceneItemEnabled": enabled,
            })
            return f"OBS source '{item['sourceName']}' is {'visible' if enabled else 'hidden'}."

        if action == "source_text":
            text = str(args.get("text") or args.get("title") or "").strip()
            if not text:
                raise OBSControlError("Tell me the new title/text.")
            client.request("SetInputSettings", {
                "inputName": item["sourceName"],
                "inputSettings": {"text": text},
                "overlay": True,
            })
            return f"Updated OBS text source '{item['sourceName']}'."

        transform = client.request("GetSceneItemTransform", {
            "sceneName": current_scene,
            "sceneItemId": item_id,
        }).get("sceneItemTransform") or {}
        patch: dict[str, float] = {}

        if action == "source_move":
            if args.get("x") is not None:
                patch["positionX"] = float(args["x"])
            if args.get("y") is not None:
                patch["positionY"] = float(args["y"])
            if args.get("position"):
                px, py = _preset_position(str(args["position"]), transform)
                patch["positionX"] = px
                patch["positionY"] = py
        elif action == "source_resize":
            source_w = float(transform.get("sourceWidth") or 0)
            source_h = float(transform.get("sourceHeight") or 0)
            if args.get("scale_x") is not None:
                patch["scaleX"] = float(args["scale_x"])
            elif args.get("width") is not None and source_w > 0:
                patch["scaleX"] = float(args["width"]) / source_w
            if args.get("scale_y") is not None:
                patch["scaleY"] = float(args["scale_y"])
            elif args.get("height") is not None and source_h > 0:
                patch["scaleY"] = float(args["height"]) / source_h
            # A single width/height request preserves the source aspect ratio.
            if args.get("width") is not None and args.get("height") is None and source_w > 0 and source_h > 0:
                factor = float(args["width"]) / source_w
                patch["scaleY"] = factor
            elif args.get("height") is not None and args.get("width") is None and source_w > 0 and source_h > 0:
                factor = float(args["height"]) / source_h
                patch["scaleX"] = factor
        else:
            patch["rotation"] = float(args.get("rotation", 0))

        if not patch:
            raise OBSControlError("No source transform change was provided.")
        client.request("SetSceneItemTransform", {
            "sceneName": current_scene,
            "sceneItemId": item_id,
            "sceneItemTransform": patch,
        })
        return json.dumps(client.request("GetSceneItemTransform", {
            "sceneName": current_scene,
            "sceneItemId": item_id,
        }), ensure_ascii=False)

    if action in {"audio_get", "audio_volume", "audio_mute", "audio_unmute", "audio_toggle_mute"}:
        source = str(args.get("source") or args.get("input_name") or "").strip()
        if not source:
            raise OBSControlError("Tell me the OBS audio source name.")
        if action == "audio_get":
            return json.dumps(client.request("GetInputVolume", {"inputName": source}), ensure_ascii=False)
        if action == "audio_volume":
            volume = float(args.get("volume"))
            if not 0 <= volume <= 100:
                raise OBSControlError("OBS volume must be between 0 and 100.")
            client.request("SetInputVolume", {
                "inputName": source,
                "inputVolumeMul": volume / 100.0,
            })
        elif action == "audio_toggle_mute":
            client.request("ToggleInputMute", {"inputName": source})
        else:
            client.request("SetInputMute", {
                "inputName": source,
                "inputMuted": action == "audio_mute",
            })
        return json.dumps(client.request("GetInputVolume", {"inputName": source}), ensure_ascii=False)

    if action in {"stream_start", "stream_stop", "stream_status"}:
        if action != "stream_status":
            client.request("StartStream" if action == "stream_start" else "StopStream")
        return json.dumps(client.request("GetStreamStatus"), ensure_ascii=False)

    if action in {"record_start", "record_stop", "record_pause", "record_status"}:
        if action != "record_status":
            client.request(
                "StartRecord" if action == "record_start"
                else "StopRecord" if action == "record_stop"
                else "ToggleRecordPause"
            )
        return json.dumps(client.request("GetRecordStatus"), ensure_ascii=False)

    if action == "transition":
        if args.get("transition"):
            client.request("SetCurrentSceneTransition", {
                "transitionName": str(args["transition"])
            })
        if args.get("duration_ms") is not None:
            client.request("SetCurrentSceneTransitionDuration", {
                "transitionDuration": max(0, int(args["duration_ms"]))
            })
        return json.dumps(client.request("GetCurrentSceneTransition"), ensure_ascii=False)

    if action == "service_settings_get":
        return json.dumps(client.request("GetStreamServiceSettings"), ensure_ascii=False)

    if action == "service_settings_set":
        service_type = str(args.get("service_type") or "").strip()
        settings = args.get("service_settings")
        if not service_type or not isinstance(settings, dict):
            raise OBSControlError(
                "Provide service_type and service_settings for OBS service configuration."
            )
        return json.dumps(client.request("SetStreamServiceSettings", {
            "streamServiceType": service_type,
            "streamServiceSettings": settings,
        }), ensure_ascii=False)

    raise OBSControlError(f"Unsupported OBS action: {action}")


def obs_control(parameters: dict[str, Any] | None = None, player=None, speak=None) -> str:
    try:
        result = run(parameters=parameters, player=player, speak=speak)
    except OBSControlError as exc:
        result = f"OBS control failed: {exc}"
    except Exception as exc:
        result = f"OBS control failed safely: {exc}"
    if player is not None:
        try:
            player.write_log(f"OBS: {result}")
        except Exception:
            pass
    return result
