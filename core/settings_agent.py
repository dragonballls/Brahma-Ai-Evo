"""Conversational JARVIS settings controller.

Maps natural-language outcomes to Brahma's persisted settings through a strict
allowlist. The LLM is used only to interpret ambiguous language; it never
receives credentials or gets arbitrary config-file access.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.user_paths import get_user_data_dir


CONFIG_DIR = get_user_data_dir() / "config"
APP_SETTINGS_FILE = CONFIG_DIR / "app_settings.json"


@dataclass(frozen=True)
class SettingSpec:
    key: str
    description: str
    kind: str
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    aliases: tuple[str, ...] = ()


SETTING_SPECS: tuple[SettingSpec, ...] = (
    SettingSpec("startup_animation_enabled", "Show the startup animation when Brahma launches.", "bool", aliases=("startup animation", "boot animation")),
    SettingSpec("show_workspace_on_startup", "Open the main workspace automatically when Brahma starts.", "bool", aliases=("workspace on startup", "open workspace on startup")),
    SettingSpec("launch_minimized", "Start Brahma minimized.", "bool", aliases=("start minimized", "launch minimized")),
    SettingSpec("check_updates_on_startup", "Check for updates when Brahma starts.", "bool", aliases=("startup updates", "check updates")),
    SettingSpec("default_ai_provider", "Default AI provider.", "choice", ("Gemini", "OpenRouter", "Local"), aliases=("ai provider", "default provider", "provider")),
    SettingSpec("auto_provider_switch", "Automatically switch AI provider if the selected provider fails.", "bool", aliases=("automatic provider switching", "provider fallback", "auto switch")),
    SettingSpec("attention_message_prompts", "Show attention prompts for incoming messages/events.", "bool", aliases=("message prompts", "attention messages")),
    SettingSpec("attention_call_prompts", "Show attention prompts for incoming calls.", "bool", aliases=("call prompts", "attention calls")),
    SettingSpec("developer_mode_enabled", "Enable developer mode.", "bool", aliases=("developer mode", "dev mode")),
    SettingSpec("developer_mode_workspace", "Workspace directory used by developer mode.", "string", aliases=("developer workspace", "dev workspace")),
    SettingSpec("sound_effects_enabled", "Enable JARVIS interface sound effects.", "bool", aliases=("sound effects", "interface sounds", "ui sounds")),
    SettingSpec("sound_effects_volume", "Sound-effects volume percentage.", "number",  minimum=0, maximum=100, aliases=("sound effects volume", "ui volume")),
    SettingSpec("push_to_talk_enabled", "Enable push-to-talk voice operation.", "bool", aliases=("push to talk", "ptt")),
    SettingSpec("offline_mode_enabled", "Keep Brahma in offline/local mode.", "bool", aliases=("offline mode", "air gapped mode")),
    SettingSpec("intelligence_mode", "Conversational intelligence mode.", "choice", ("smart", "fast", "off"), aliases=("intelligence mode", "reasoning mode")),
    SettingSpec("intelligence_orchestration_enabled", "Enable multi-model reasoning and final synthesis.", "bool", aliases=("multi model reasoning", "multi model", "orchestration")),
    SettingSpec("local_ai_url", "Local AI server URL.", "string", aliases=("local server", "local ai url", "ollama url")),
    SettingSpec("local_ai_model", "Local AI model identifier.", "string", aliases=("local model", "ollama model")),
    SettingSpec("desktop_mode_enabled", "Enable Brahma desktop environment mode.", "bool", aliases=("desktop mode",)),
    SettingSpec("desktop_performance_profile", "Desktop rendering/performance profile.", "choice", ("adaptive", "balanced", "performance", "game", "efficiency"), aliases=("performance profile", "desktop performance")),
    SettingSpec("show_desktop_performance_overlay", "Show the desktop performance overlay.", "bool", aliases=("performance overlay", "desktop overlay")),
    SettingSpec("desktop_workerw_backend_enabled", "Use the WorkerW desktop backend.", "bool", aliases=("workerw", "workerw backend")),
    SettingSpec("app_theme", "Brahma interface theme color or predefined theme name.", "string", aliases=("theme", "color theme", "interface color")),
)


_SPEC_BY_KEY = {spec.key: spec for spec in SETTING_SPECS}
_ALIAS_TO_KEY = {
    alias.lower(): spec.key
    for spec in SETTING_SPECS
    for alias in spec.aliases
}


def load_settings() -> dict[str, Any]:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not APP_SETTINGS_FILE.exists():
        return {}
    try:
        data = json.loads(APP_SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(updates: dict[str, Any]) -> None:
    current = load_settings()
    current.update(updates)
    tmp = APP_SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(current, indent=4, ensure_ascii=False), encoding="utf-8")
    tmp.replace(APP_SETTINGS_FILE)


def catalog() -> list[dict[str, Any]]:
    return [
        {
            "key": s.key,
            "description": s.description,
            "kind": s.kind,
            **({"choices": list(s.choices)} if s.choices else {}),
            **({"minimum": s.minimum, "maximum": s.maximum} if s.minimum is not None else {}),
            "aliases": list(s.aliases),
        }
        for s in SETTING_SPECS
    ]


def _lookup(text: str) -> str | None:
    clean = text.lower().strip().replace("_", " ")
    if clean in _SPEC_BY_KEY:
        return clean
    if clean in _ALIAS_TO_KEY:
        return _ALIAS_TO_KEY[clean]
    for alias, key in sorted(_ALIAS_TO_KEY.items(), key=lambda item: len(item[0]), reverse=True):
        if alias in clean:
            return key
    return None


def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"on", "true", "yes", "enable", "enabled", "enable it"}:
        return True
    if text in {"off", "false", "no", "disable", "disabled", "turn it off"}:
        return False
    return None


def validate(key: str, value: Any) -> Any:
    spec = _SPEC_BY_KEY.get(key)
    if spec is None:
        raise ValueError(f"Setting '{key}' is not supported by conversational configuration.")

    if spec.kind == "bool":
        parsed = _parse_bool(value)
        if parsed is None:
            raise ValueError(f"'{value}' is not a valid on/off value for {key}.")
        return parsed

    if spec.kind == "number":
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"'{value}' is not a valid number for {key}.") from exc
        if spec.minimum is not None and number < spec.minimum:
            raise ValueError(f"{key} cannot be below {spec.minimum}.")
        if spec.maximum is not None and number > spec.maximum:
            raise ValueError(f"{key} cannot be above {spec.maximum}.")
        return int(number) if number.is_integer() else number

    if spec.kind == "choice":
        raw = str(value or "").strip().lower()
        choices = {choice.lower(): choice for choice in spec.choices}
        if raw not in choices:
            raise ValueError(f"{key} must be one of: {', '.join(spec.choices)}.")
        return choices[raw]

    return str(value or "").strip()


def _extract_value(text: str, key: str) -> Any | None:
    spec = _SPEC_BY_KEY[key]
    lower = text.lower()

    if spec.kind == "bool":
        negative = re.search(r"\b(?:disable|turn\s+off|switch\s+off|don't|do not|never)\b", lower)
        positive = re.search(r"\b(?:enable|turn\s+on|switch\s+on)\b", lower)
        if negative and not positive:
            return False
        if positive and not negative:
            return True
        return None

    patterns = [
        rf"\b(?:set|make|change|switch|use|choose|pick)\b[^.!?\n]{{0,80}}?\b{re.escape(key.replace('_', ' '))}\b\s*(?:to|as|=|:)?\s*([^.!?\n]+)",
    ]
    for alias in sorted(spec.aliases, key=len, reverse=True):
        patterns.append(rf"\b{re.escape(alias)}\b\s*(?:to|as|=|:)?\s*([^.!?\n]+)")
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            value = match.group(1).strip().strip('"').strip("'")
            if spec.kind == "number":
                numeric = re.search(r"-?\d+(?:\.\d+)?", value)
                return numeric.group(0) if numeric else None
            if spec.kind == "choice":
                for choice in spec.choices:
                    if re.search(rf"\b{re.escape(choice)}\b", value, re.I):
                        return choice
            return value
    return None


def deterministic_plan(request: str) -> list[dict[str, Any]]:
    patches: list[dict[str, Any]] = []
    lower = request.lower()

    # Natural outcome shortcuts.
    if any(term in lower for term in ("low power", "save battery", "use less power", "lighter on my pc", "lightweight")):
        patches.extend([
            {"key": "desktop_performance_profile", "value": "efficiency"},
            {"key": "show_desktop_performance_overlay", "value": False},
            {"key": "startup_animation_enabled", "value": False},
        ])
    if "high performance" in lower or "maximum performance" in lower:
        patches.append({"key": "desktop_performance_profile", "value": "performance"})

    for spec in SETTING_SPECS:
        if spec.key.replace("_", " ") in lower or any(alias in lower for alias in spec.aliases):
            value = _extract_value(request, spec.key)
            if value is not None:
                patches.append({"key": spec.key, "value": value})

    # Common provider phrasings that do not use the setting name.
    provider_map = {
        "gemini": "Gemini",
        "google gemini": "Gemini",
        "openrouter": "OpenRouter",
        "local ai": "Local",
        "ollama": "Local",
    }
    if re.search(r"\b(?:use|switch to|make|set)\b", lower):
        for phrase, provider in provider_map.items():
            if phrase in lower:
                patches.append({"key": "default_ai_provider", "value": provider})
                break

    # Deduplicate with last value winning.
    merged: dict[str, Any] = {}
    for patch in patches:
        merged[str(patch["key"])] = patch["value"]
    return [{"key": key, "value": value} for key, value in merged.items()]


def _redact_request(request: str) -> str:
    # Do not send probable credentials to an LLM for interpretation.
    patterns = [
        r"(?i)\bsk-[A-Za-z0-9_-]{16,}\b",
        r"(?i)\bAIza[0-9A-Za-z_-]{20,}\b",
        r"(?i)\bgh[pousr]_[A-Za-z0-9_]{20,}\b",
        r"(?i)\b(?:api[_ -]?key|client[_ -]?secret|access[_ -]?token|refresh[_ -]?token|password)\s*[:=]\s*\S+",
    ]
    redacted = request
    for pattern in patterns:
        redacted = re.sub(pattern, "[REDACTED_SECRET]", redacted)
    return redacted


def _llm_plan(request: str) -> list[dict[str, Any]]:
    from llm_client import client

    prompt = (
        "Map the user's natural-language settings request to zero or more settings from this allowlist. "
        "Return valid JSON with a patches array and an optional needs_clarification boolean. "
        "Never invent keys. Use the exact setting key names. Convert on/off to booleans and numeric values to numbers. "
        "Do not make changes outside this catalog. If the user asks for an unsupported or genuinely ambiguous setting, "
        "return an empty patch list.\n\n"
        f"ALLOWLIST:\n{json.dumps(catalog(), indent=2)}\n\n"
        f"REQUEST:\n{_redact_request(request)}"
    )
    data = client.intelligent_json(
        prompt,
        system="You are JARVIS's settings parser. Output valid JSON only.",
        profile="fast",
        max_tokens=1200,
    )
    patches = data.get("patches", []) if isinstance(data, dict) else []
    return patches if isinstance(patches, list) else []

def plan(request: str) -> list[dict[str, Any]]:
    request = str(request or "").strip()
    if not request:
        return []
    patches = deterministic_plan(request)
    if patches:
        return patches
    try:
        return _llm_plan(request)
    except Exception:
        return []


def apply(request: str) -> dict[str, Any]:
    patches = plan(request)
    if not patches:
        return {
            "ok": False,
            "changed": [],
            "message": "I couldn't map that request to a supported Brahma setting. Tell me the outcome you want, such as 'make startup quieter' or 'use OpenRouter by default'.",
        }

    validated: dict[str, Any] = {}
    errors: list[str] = []
    for patch in patches:
        key = str(patch.get("key") or "").strip()
        try:
            validated[key] = validate(key, patch.get("value"))
        except Exception as exc:
            errors.append(str(exc))

    if errors:
        return {"ok": False, "changed": [], "message": "; ".join(errors)}

    try:
        before = load_settings()
        save_settings(validated)
    except Exception as exc:
        return {"ok": False, "changed": [], "message": f"Could not save settings: {exc}"}

    changed = []
    for key, value in validated.items():
        changed.append({
            "key": key,
            "old": before.get(key),
            "new": value,
            "description": _SPEC_BY_KEY[key].description,
        })
    return {
        "ok": True,
        "changed": changed,
        "message": _format_success(changed),
        "requires_restart": any(
            item["key"] in {
                "startup_animation_enabled",
                "show_workspace_on_startup",
                "launch_minimized",
                "desktop_workerw_backend_enabled",
                "developer_mode_workspace",
            }
            for item in changed
        ),
    }


def status() -> dict[str, Any]:
    current = load_settings()
    return {
        key: current.get(spec.key)
        for spec in SETTING_SPECS
        if spec.key in current
    }


def _format_success(changed: list[dict[str, Any]]) -> str:
    labels = []
    for item in changed:
        label = item["key"].replace("_", " ")
        labels.append(f"{label}: {item['new']}")
    suffix = " Some changes may take effect after a restart." if any(
        item["key"] in {"startup_animation_enabled", "show_workspace_on_startup", "launch_minimized", "desktop_workerw_backend_enabled", "developer_mode_workspace"}
        for item in changed
    ) else ""
    return "Done, sir. " + "; ".join(labels) + "." + suffix
