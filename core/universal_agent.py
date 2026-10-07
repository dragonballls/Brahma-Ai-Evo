"""Universal capability fallback for Brahma Evo.

Existing first-class tools remain the preferred execution path. This module is the
last-resort path for requests that have no matching registered skill: it can
synthesize a verified lightweight skill through Project Ultron, hot-load it,
and execute it against the user's original request.

The generated code is still constrained by SkillForge/Crucible; this layer never
provides raw shell execution or bypasses Brahma's existing tool gates.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from core.dynamic_registry import DynamicToolRegistry


def _result_text(result: Any) -> str:
    if isinstance(result, dict):
        return str(
            result.get("summary")
            or result.get("output")
            or result.get("text")
            or result.get("message")
            or result
        ).strip()
    return str(result).strip()


def _execute_skill(name: str, args: dict[str, Any]) -> Any:
    return DynamicToolRegistry.execute_sync(name, args)


def run(request: str, *, context: str = "", max_repair_attempts: int = 2) -> dict[str, Any]:
    """Fulfill a request with an existing dynamic skill or synthesize a new one."""
    request = (request or "").strip()
    if not request:
        return {"success": False, "status": "invalid", "message": "A request is required."}

    DynamicToolRegistry.initialize()

    # Prefer an already-installed dynamic capability.
    match = DynamicToolRegistry.find_matching_skill(request)
    if match:
        skill_name, extracted_args = match
        args = dict(extracted_args or {})
        args.setdefault("request", request)
        args.setdefault("query", request)
        try:
            result = _execute_skill(skill_name, args)
            if isinstance(result, dict) and result.get("success") is False:
                return {
                    "success": False,
                    "status": "existing-skill-failed",
                    "skill": skill_name,
                    "result": _result_text(result),
                    "raw": result,
                    "error": str(result.get("error") or result.get("message") or "Dynamic skill reported failure."),
                    "message": str(result.get("error") or result.get("message") or "Dynamic skill reported failure."),
                }
            return {
                "success": True,
                "status": "executed-existing-skill",
                "skill": skill_name,
                "result": _result_text(result),
                "raw": result,
            }
        except Exception as exc:
            return {
                "success": False,
                "status": "existing-skill-failed",
                "skill": skill_name,
                "error": str(exc),
            "message": str(exc),
            }

    # No matching capability: synthesize it with Project Ultron.
    from core.skill_forge import SkillForge

    synthesis_context = (
        "This is an automatic last-resort capability expansion. "
        "Prefer Brahma's existing native tools when possible. "
        "Do not create unrestricted shell execution, credential extraction, "
        "arbitrary destructive operations, or hidden background persistence. "
        "Create one focused reusable capability that fulfills the request. "
        "It must be verified by the existing Crucible and hot-load cleanly.\n"
        + (context or "")
    )

    forged = SkillForge.forge_skill(
        request,
        context_hints=synthesis_context,
        max_repair_attempts=max_repair_attempts,
    )
    if not forged.get("success"):
        return {
            "success": False,
            "status": "synthesis-failed",
            "error": str(forged.get("message") or forged.get("error") or "Capability synthesis failed."),
            "message": str(forged.get("message") or forged.get("error") or "Capability synthesis failed."),
            "forge": forged,
        }

    name = str(forged.get("name") or "").strip()
    if not name or not DynamicToolRegistry.has_tool(name):
        DynamicToolRegistry.initialize()
    if not name or not DynamicToolRegistry.has_tool(name):
        return {
            "success": False,
            "status": "synthesis-unavailable",
            "message": "The new capability was generated but could not be registered.",
            "forge": forged,
        }

    # Give the new skill the original intent plus common parameter names so
    # simple generated skills can operate without a second user prompt.
    args = {
        "request": request,
        "query": request,
        "description": request,
    }
    try:
        result = _execute_skill(name, args)
        if isinstance(result, dict) and result.get("success") is False:
            return {
                "success": False,
                "status": "synthesized-but-execution-failed",
                "skill": name,
                "result": _result_text(result),
                "raw": result,
                "error": str(result.get("error") or result.get("message") or "Generated skill reported failure."),
                "message": str(result.get("error") or result.get("message") or "Generated skill reported failure."),
                "forge": forged,
            }
        return {
            "success": True,
            "status": "synthesized-and-executed",
            "skill": name,
            "result": _result_text(result),
            "raw": result,
            "forge": forged,
        }
    except Exception as exc:
        return {
            "success": False,
            "status": "synthesized-but-execution-failed",
            "skill": name,
            "message": str(exc),
            "forge": forged,
        }
