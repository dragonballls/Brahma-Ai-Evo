import json
import math
import time
from actions.brahma_connect import connect_execute

MAX_MOBILE_COORDINATE = 10_000
MAX_TYPED_TEXT = 4_000
MAX_AUTOPILOT_SECONDS = 600.0
MAX_REPEAT_SIGNATURES = 2

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

def _verification_texts(ui_tree):
    if not isinstance(ui_tree, dict):
        return []
    values = []
    for node in ui_tree.get("nodes", []) or []:
        if not isinstance(node, dict):
            continue
        for key in ("content_description", "text"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                values.append(value.strip())
    return values

def _verify_completion(ui_tree, verification):
    if isinstance(verification, str):
        claims = [verification.strip()]
    elif isinstance(verification, list):
        claims = [item.strip() for item in verification if isinstance(item, str) and item.strip()]
    else:
        claims = []
    if not claims:
        return False
    visible = "\n".join(_verification_texts(ui_tree)).casefold()
    return all(claim.casefold() in visible for claim in claims)

def _build_prompt(instruction: str, ui_tree: dict) -> str:
    nodes = ui_tree.get("nodes", [])
    simplified_ui = []
    for i, node in enumerate(nodes):
        bounds = node.get("bounds", [0, 0, 0, 0])
        cx = (bounds[0] + bounds[2]) // 2
        cy = (bounds[1] + bounds[3]) // 2
        desc = node.get("content_description") or node.get("text") or node.get("class")
        simplified_ui.append(f"[{i}] {desc} (clickable: {node.get('is_clickable')}) -> x:{cx}, y:{cy}")
    
    ui_text = "\n".join(simplified_ui)
    
    prompt = f"""You are a mobile UI automation agent.
Your goal is: {instruction}

Current UI elements on screen (with their center coordinates):
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
        prompt = _build_prompt(instruction, ui_tree)
        
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
        action = decision.get("action")
        if not isinstance(action, str):
            return json.dumps({"success": False, "error": "Mobile autopilot received an invalid action.", "steps_attempted": step})
        action = action.strip().lower()
        reason = decision.get("reason", "Executing next step")
        if not isinstance(reason, str):
            return json.dumps({"success": False, "error": "Mobile autopilot received an invalid reason.", "steps_attempted": step})
        
        if player and hasattr(player, 'write_log'):
            player.write_log(f"Step {step+1}: {reason}")
        if speak:
            speak(reason)
        elif player and hasattr(player, 'speak_async'):
            player.speak_async(reason)
            
        if action == "done":
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
                    command_parameters = {
                        "x": _validate_coordinate(decision.get("x"), "x"),
                        "y": _validate_coordinate(decision.get("y"), "y"),
                    }
                    remote_action = "ui_tap"
                    delay = 2.0
                elif action == "swipe":
                    command_parameters = {
                        "x1": _validate_coordinate(decision.get("x1"), "x1"),
                        "y1": _validate_coordinate(decision.get("y1"), "y1"),
                        "x2": _validate_coordinate(decision.get("x2"), "x2"),
                        "y2": _validate_coordinate(decision.get("y2"), "y2"),
                    }
                    remote_action = "ui_swipe"
                    delay = 2.0
                else:
                    text_value = decision.get("text")
                    if not isinstance(text_value, str) or not text_value.strip():
                        raise ValueError("Mobile type action requires non-empty text.")
                    if len(text_value) > MAX_TYPED_TEXT:
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

            try:
                _parse_remote_result(
                    connect_execute({
                        "target": target,
                        "action": remote_action,
                        "parameters": command_parameters,
                    }),
                    action,
                )
            except (RuntimeError, ValueError) as exc:
                return json.dumps({"success": False, "error": str(exc), "steps_attempted": step + 1})

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return json.dumps({"success": False, "error": "Mobile autopilot timed out.", "steps_attempted": step + 1})
            time.sleep(min(delay, remaining))

    return json.dumps({
        "success": False,
        "error": "Mobile autopilot stopped before the goal was confirmed complete.",
        "steps_attempted": max_steps,
    })
