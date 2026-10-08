import json
import math
import time
from actions.brahma_connect import connect_execute

MAX_MOBILE_COORDINATE = 10_000
MAX_TYPED_TEXT = 4_000
MAX_AUTOPILOT_SECONDS = 600.0
MAX_REPEAT_SIGNATURES = 2

def _ui_state_signature(ui_tree) -> str:
    """Canonicalize bounded, user-visible UI state for stale-action detection."""
    if not isinstance(ui_tree, dict):
        return ""
    try:
        screen_width, screen_height = _screen_dimensions(ui_tree)
    except ValueError:
        return ""
    nodes = []
    for node, bounds in _validated_nodes(ui_tree):
        nodes.append({
            "bounds": list(bounds),
            "text": str(node.get("text") or "")[:512],
            "content_description": str(node.get("content_description") or "")[:512],
            "class_name": str(node.get("class_name") or node.get("className") or "")[:256],
        })
    return json.dumps(
        {"screen_width": screen_width, "screen_height": screen_height, "nodes": nodes},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _parse_remote_result(raw, action):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raise ValueError(f"Mobile action '{action}' returned an invalid result.")
    if not isinstance(raw, dict):
        raise ValueError(f"Mobile action '{action}' returned a malformed result.")
    if raw.get("success") is not True:
        raise RuntimeError(str(raw.get("error") or f"Mobile action '{action}' failed."))
    return raw

def _validate_coordinate(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Mobile action coordinate '{name}' must be numeric.")
    number = float(value)
    if not math.isfinite(number) or not number.is_integer():
        raise ValueError(f"Mobile action coordinate '{name}' must be a finite integer.")
    coordinate = int(number)
    if coordinate < 0 or coordinate > MAX_MOBILE_COORDINATE:
        raise ValueError(f"Mobile action coordinate '{name}' is outside the safe range.")
    return coordinate

def _validated_nodes(ui_tree):
    if not isinstance(ui_tree, dict):
        return []
    try:
        screen_width, screen_height = _screen_dimensions(ui_tree)
    except ValueError:
        return []
    nodes = ui_tree.get("nodes")
    if not isinstance(nodes, list):
        return []
    valid = []
    for node in nodes[:500]:
        if not isinstance(node, dict):
            continue
        bounds = node.get("bounds")
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
            continue
        try:
            left, top, right, bottom = (float(value) for value in bounds)
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(v) and v.is_integer() for v in (left, top, right, bottom)):
            continue
        left, top, right, bottom = map(int, (left, top, right, bottom))
        if left < 0 or top < 0 or right > screen_width or bottom > screen_height:
            continue
        if right <= left or bottom <= top:
            continue
        valid.append((node, (left, top, right, bottom)))
    return valid

def _verification_texts(ui_tree):
    values = []
    for node, _bounds in _validated_nodes(ui_tree):
        for key in ("content_description", "text"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                values.append(value.strip()[:512])
    return values

def _verify_completion(ui_tree, verification):
    if isinstance(verification, str):
        claims = [verification.strip()]
    elif isinstance(verification, list):
        claims = [item.strip() for item in verification if isinstance(item, str) and item.strip()]
    else:
        claims = []
    if not claims or len(claims) > 10 or any(len(claim) > 512 for claim in claims):
        return False
    visible = "\n".join(_verification_texts(ui_tree)).casefold()
    return all(claim.casefold() in visible for claim in claims)

def _screen_dimensions(ui_tree: dict) -> tuple[int, int]:
    if not isinstance(ui_tree, dict):
        raise ValueError("Mobile UI dump data must be an object.")
    screen_width = ui_tree.get("screen_width")
    screen_height = ui_tree.get("screen_height")
    if (
        isinstance(screen_width, bool) or not isinstance(screen_width, (int, float))
        or isinstance(screen_height, bool) or not isinstance(screen_height, (int, float))
        or not math.isfinite(float(screen_width))
        or not math.isfinite(float(screen_height))
        or not float(screen_width).is_integer()
        or not float(screen_height).is_integer()
        or int(screen_width) <= 0
        or int(screen_height) <= 0
    ):
        raise ValueError("Mobile UI dump is missing valid device screen dimensions.")
    screen_width = int(screen_width)
    screen_height = int(screen_height)
    if screen_width > MAX_MOBILE_COORDINATE + 1 or screen_height > MAX_MOBILE_COORDINATE + 1:
        raise ValueError("Mobile device screen dimensions exceed the safe coordinate range.")
    return screen_width, screen_height


def _point_is_visible(ui_tree, x, y):
    return any(
        left <= x < right and top <= y < bottom
        for _node, (left, top, right, bottom) in _validated_nodes(ui_tree)
    )

def _build_prompt(instruction: str, ui_tree: dict) -> str:
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("Mobile autopilot instruction must be a non-empty string.")
    if len(instruction) > 4_000:
        raise ValueError("Mobile autopilot instruction exceeds the safe size limit.")
    if not isinstance(ui_tree, dict):
        raise ValueError("Mobile UI dump data must be an object.")
    nodes = ui_tree.get("nodes", [])
    if not isinstance(nodes, list):
        raise ValueError("Mobile UI dump nodes must be a list.")

    screen_width, screen_height = _screen_dimensions(ui_tree)
    simplified_ui = []
    total_chars = 0
    for i, node in enumerate(nodes[:500]):
        if not isinstance(node, dict):
            continue
        bounds = node.get("bounds")
        if (
            not isinstance(bounds, (list, tuple))
            or len(bounds) != 4
            or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in bounds)
        ):
            continue
        try:
            left, top, right, bottom = (float(value) for value in bounds)
            if not all(math.isfinite(value) and value.is_integer() for value in (left, top, right, bottom)):
                continue
            left, top, right, bottom = map(int, (left, top, right, bottom))
            if left < 0 or top < 0 or right > screen_width or bottom > screen_height or right <= left or bottom <= top:
                continue
            cx = _validate_coordinate((left + right) / 2, "ui_center_x")
            cy = _validate_coordinate((top + bottom) / 2, "ui_center_y")
        except ValueError:
            continue
        desc = str(node.get("content_description") or node.get("text") or node.get("class") or "Unnamed element")
        desc = desc[:512]
        row = f"[{i}] {desc} (clickable: {bool(node.get('is_clickable'))}) -> x:{cx}, y:{cy}"
        if total_chars + len(row) > 60_000:
            break
        simplified_ui.append(row)
        total_chars += len(row)

    ui_text = "\n".join(simplified_ui)

    prompt = f"""You are a mobile UI automation agent.
Your goal is: {instruction[:4_000]}

Current UI elements on screen ({screen_width}x{screen_height} pixels):
{ui_text}

Decide the next action to take to accomplish the goal.
Respond ONLY with a JSON object in this format:
- To tap: {{"action": "tap", "x": 100, "y": 200, "reason": "Tapping the search bar"}}
- To swipe: {{"action": "swipe", "x1": 500, "y1": 800, "x2": 500, "y2": 200, "reason": "Scrolling down"}}
- To type text: {{"action": "type", "text": "hello", "reason": "Typing message"}}
- Only when the goal is completely finished and the current UI visibly proves it: {{"action": "done", "verification": ["exact visible text proving completion"], "reason": "The success screen is visible"}}
Never invent coordinates, use non-numeric coordinates, or use unsupported actions.
"""
    return prompt

def mobile_autopilot(parameters: dict, response=None, player=None, session_memory=None, speak=None) -> str:
    target = parameters.get("target") or parameters.get("device_id") or ""
    instruction = parameters.get("instruction")

    if not instruction:
        return json.dumps({"success": False, "error": "Missing instruction."})
    if not isinstance(target, str) or not target.strip():
        return json.dumps({"success": False, "error": "A specific target device is required."})
    try:
        timeout_seconds = float(parameters.get("timeout_seconds", 120.0))
    except (TypeError, ValueError):
        return json.dumps({"success": False, "error": "Invalid autopilot timeout."})
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or timeout_seconds > MAX_AUTOPILOT_SECONDS:
        return json.dumps({"success": False, "error": "Autopilot timeout is outside the safe range."})
    deadline = time.monotonic() + timeout_seconds

    try:
        from core.gemini_runtime import generate_json
    except Exception as e:
        return json.dumps({"success": False, "error": f"Failed to initialize Gemini: {e}"})

    if speak:
        speak(f"Taking over {target} to complete your task.")
    elif player and hasattr(player, 'speak_async'):
        player.speak_async(f"Taking over {target} to complete your task.")
    elif player and hasattr(player, 'write_log'):
        player.write_log(f"Autopilot started on {target} for '{instruction}'")
        
    max_steps = 10
    seen_signatures = {}

    for step in range(max_steps):
        if bool(parameters.get("cancelled", False)):
            return json.dumps({"success": False, "error": "Mobile autopilot was cancelled.", "steps_attempted": step})
        if time.monotonic() >= deadline:
            return json.dumps({"success": False, "error": "Mobile autopilot timed out.", "steps_attempted": step})
        try:
            dump_res = _parse_remote_result(
                connect_execute({"target": target, "action": "ui_dump", "parameters": {}}),
                "ui_dump",
            )
        except (RuntimeError, ValueError) as exc:
            return json.dumps({"success": False, "error": str(exc), "steps_attempted": step})
        ui_tree = dump_res.get("data", {})
        try:
            screen_width, screen_height = _screen_dimensions(ui_tree)
            prompt = _build_prompt(instruction, ui_tree)
        except ValueError as exc:
            return json.dumps({"success": False, "error": str(exc), "steps_attempted": step})
        
        try:
            decision = generate_json(
                prompt,
                system_instruction="You are a mobile UI automation agent. Return ONLY the requested JSON action object.",
                model_name="gemini-3.8-flash",
                max_output_tokens=2048,
            )
        except Exception as e:
            if player:
                player.write_log(f"Autopilot LLM error: {e}")
            break
        if not isinstance(decision, dict):
            return json.dumps({"success": False, "error": "Mobile autopilot received malformed model output.", "steps_attempted": step})
        try:
            decision_bytes = len(json.dumps(decision, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            return json.dumps({"success": False, "error": "Mobile autopilot received unserializable model output.", "steps_attempted": step})
        if decision_bytes > 16 * 1024:
            return json.dumps({"success": False, "error": "Mobile autopilot received oversized model output.", "steps_attempted": step})
        if bool(parameters.get("cancelled", False)):
            return json.dumps({"success": False, "error": "Mobile autopilot was cancelled during model generation.", "steps_attempted": step})
        action = decision.get("action")
        if not isinstance(action, str):
            return json.dumps({"success": False, "error": "Mobile autopilot received an invalid action.", "steps_attempted": step})
        action = action.strip().lower()
        reason = decision.get("reason", "Executing next step")
        if not isinstance(reason, str):
            return json.dumps({"success": False, "error": "Mobile autopilot received an invalid reason.", "steps_attempted": step})
        if len(reason) > 1_000:
            return json.dumps({"success": False, "error": "Mobile autopilot received an oversized reason.", "steps_attempted": step})
        
        if player and hasattr(player, 'write_log'):
            player.write_log(f"Step {step+1}: {reason}")
        if speak:
            speak(reason)
        elif player and hasattr(player, 'speak_async'):
            player.speak_async(reason)
            
        if action == "done":
            if bool(parameters.get("cancelled", False)):
                return json.dumps({"success": False, "error": "Mobile autopilot was cancelled before completion verification.", "steps_attempted": step + 1})
            try:
                verified_ui = _parse_remote_result(
                    connect_execute({"target": target, "action": "ui_dump", "parameters": {}}),
                    "ui_dump",
                )
            except (RuntimeError, ValueError) as exc:
                return json.dumps({"success": False, "error": str(exc), "steps_attempted": step + 1})
            if not _verify_completion(verified_ui.get("data", {}), decision.get("verification")):
                return json.dumps({
                    "success": False,
                    "error": "Model claimed completion without independently verifiable UI evidence.",
                    "steps_attempted": step + 1,
                })
            if speak:
                speak("Task completed successfully.")
            elif player and hasattr(player, 'speak_async'):
                player.speak_async("Task completed successfully.")
            return json.dumps({"success": True, "message": f"Finished: {reason}"})

        if action not in {"tap", "swipe", "type"}:
            return json.dumps({
                "success": False,
                "error": f"Unsupported mobile action: {action!r}.",
                "steps_attempted": step + 1,
            })

        try:
            if action == "tap":
                raw_bounds = [
                    decision.get("x"),
                    decision.get("y"),
                ]
                command_parameters = {
                    "x": _validate_coordinate(raw_bounds[0], "x"),
                    "y": _validate_coordinate(raw_bounds[1], "y"),
                }
                if not (
                    command_parameters["x"] < screen_width
                    and command_parameters["y"] < screen_height
                ):
                    raise ValueError("Mobile tap coordinates are outside the actual device screen bounds.")
                if not _point_is_visible(ui_tree, command_parameters["x"], command_parameters["y"]):
                    raise ValueError("Mobile tap coordinates do not fall within a visible UI element.")
                remote_action = "ui_tap"
                delay = 2.0
            elif action == "swipe":
                command_parameters = {
                    "x1": _validate_coordinate(decision.get("x1"), "x1"),
                    "y1": _validate_coordinate(decision.get("y1"), "y1"),
                    "x2": _validate_coordinate(decision.get("x2"), "x2"),
                    "y2": _validate_coordinate(decision.get("y2"), "y2"),
                }
                if not all(
                    point < limit
                    for point, limit in (
                        (command_parameters["x1"], screen_width),
                        (command_parameters["x2"], screen_width),
                        (command_parameters["y1"], screen_height),
                        (command_parameters["y2"], screen_height),
                    )
                ):
                    raise ValueError("Mobile swipe coordinates are outside the actual device screen bounds.")
                remote_action = "ui_swipe"
                delay = 2.0
            else:
                text_value = decision.get("text")
                if not isinstance(text_value, str) or not text_value.strip():
                    raise ValueError("Mobile type action requires non-empty text.")
                if len(text_value.encode("utf-8")) > 4 * 1024:
                    raise ValueError("Mobile type action text exceeds the safe size limit.")
                command_parameters = {"text": text_value}
                remote_action = "ui_type"
                delay = 1.0
        except ValueError as exc:
            return json.dumps({"success": False, "error": str(exc), "steps_attempted": step + 1})

        signature = json.dumps(
            {"action": action, "parameters": command_parameters, "ui": ui_tree},
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        count = seen_signatures.get(signature, 0) + 1
        seen_signatures[signature] = count
        if count > MAX_REPEAT_SIGNATURES:
            return json.dumps({
                "success": False,
                "error": "Mobile autopilot detected a repeated action loop.",
                "steps_attempted": step + 1,
            })

        if bool(parameters.get("cancelled", False)):
            return json.dumps({
                "success": False,
                "error": "Mobile autopilot was cancelled before action dispatch.",
                "steps_attempted": step + 1,
            })

        try:
            fresh_dump = _parse_remote_result(
                connect_execute({"target": target, "action": "ui_dump", "parameters": {}}),
                "ui_dump",
            )
            fresh_ui = fresh_dump.get("data", {})
            if _ui_state_signature(fresh_ui) != _ui_state_signature(ui_tree):
                return json.dumps({
                    "success": False,
                    "error": "Mobile UI changed before action dispatch; refusing to execute a stale action.",
                    "steps_attempted": step + 1,
                })
        except (RuntimeError, ValueError) as exc:
            return json.dumps({"success": False, "error": str(exc), "steps_attempted": step + 1})

        if bool(parameters.get("cancelled", False)):
            return json.dumps({
                "success": False,
                "error": "Mobile autopilot was cancelled before action dispatch.",
                "steps_attempted": step + 1,
            })

        try:
            _parse_remote_result(
                connect_execute({                    "target": target,
                    "action": remote_action,
                    "parameters": command_parameters,
                }),
                action,
            )
        except (RuntimeError, ValueError) as exc:
            return json.dumps({"success": False, "error": str(exc), "steps_attempted": step + 1})

        if bool(parameters.get("cancelled", False)):
            return json.dumps({
                "success": False,
                "error": "Mobile autopilot was cancelled after remote action dispatch; outcome is not reported as success.",
                "steps_attempted": step + 1,
            })

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return json.dumps({
                "success": False,
                "error": "Mobile autopilot timed out.",
                "steps_attempted": step + 1,
            })
        sleep_until = time.monotonic() + min(delay, remaining)
        while True:
            if bool(parameters.get("cancelled", False)):
                return json.dumps({"success": False, "error": "Mobile autopilot was cancelled during action delay.", "steps_attempted": step + 1})
            wait_for = sleep_until - time.monotonic()
            if wait_for <= 0:
                break
            time.sleep(min(0.25, wait_for))


    return json.dumps({
        "success": False,
        "error": "Mobile autopilot stopped before the goal was confirmed complete.",
        "steps_attempted": max_steps,
    })
