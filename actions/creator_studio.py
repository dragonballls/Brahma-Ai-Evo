"""Conversational entry point for Brahma Creator Studio."""
from __future__ import annotations

import json
from typing import Any


def creator_control(parameters: dict[str, Any] | None = None, player=None, speak=None) -> str:
    args = dict(parameters or {})
    action = str(args.get("action") or "tools").strip().lower()
    try:
        from core.creator_engine import (
            creator_tools,
            create_creator_project,
            load_creator_project,
            render_creator_project,
            publish_creator_project,
            refresh_creator_metadata,
            creator_script,
            creator_rights_review,
        )
        if action == "tools":
            return json.dumps(creator_tools(), ensure_ascii=False)
        if action in {"create", "plan"}:
            return json.dumps(create_creator_project(args), ensure_ascii=False)
        if action == "render":
            return json.dumps(render_creator_project(str(args.get("project") or "")), ensure_ascii=False)
        if action == "publish":
            return json.dumps(
                publish_creator_project(
                    str(args.get("project") or ""),
                    privacy=str(args.get("privacy") or "private"),
                    playlist_id=args.get("playlist_id"),
                ),
                ensure_ascii=False,
            )
        if action == "metadata":
            return json.dumps(refresh_creator_metadata(str(args.get("project") or "")), ensure_ascii=False)
        if action == "script":
            return json.dumps(
                creator_script(
                    str(args.get("project") or ""),
                    length=str(args.get("length") or "medium"),
                ),
                ensure_ascii=False,
            )
        if action == "rights_review":
            return json.dumps(creator_rights_review(str(args.get("project") or "")), ensure_ascii=False)
        if action == "status":
            return json.dumps(load_creator_project(str(args.get("project") or ""))[1], ensure_ascii=False)
        if action in {"record_start", "record_stop"}:
            from actions.obs_control import obs_control
            return obs_control({"action": action}, player=player, speak=speak)
        return "Creator Studio supports tools, create, render, publish, metadata, script, rights_review, status, record_start, and record_stop."
    except Exception as exc:
        return f"Creator Studio failed: {exc}"
