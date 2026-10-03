"""Conversational entry point for Brahma Creator Studio."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def creator_control(parameters: dict[str, Any] | None = None, player=None, speak=None) -> str:
    args = dict(parameters or {})
    action = str(args.get("action") or "tools").strip().lower()
    try:
        from core.creator_engine import (
            creator_tools,
            create_creator_project,
            read_manifest,
            render_creator_project,
            publish_creator_project,
            refresh_creator_metadata,
            creator_script,
            creator_rights_review,
            produce_creator_project,
        )
        if action == "tools":
            return json.dumps(creator_tools(), ensure_ascii=False)
        if action in {"create", "plan"}:
            return json.dumps(create_creator_project(args), ensure_ascii=False)
        if action == "produce":
            return json.dumps(produce_creator_project(args), ensure_ascii=False)
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
        if action == "music_search":
            from core.creator_audio import recommend_music
            return recommend_music(
                str(args.get("query") or args.get("mood") or ""),
                str(args.get("mood") or ""),
                player=player,
            )
        if action == "record_audio":
            from core.creator_audio import record_microphone
            output = str(args.get("output") or (Path.home() / "Desktop" / "brahma_creator_recording.wav"))
            duration = float(args.get("duration_seconds") or args.get("duration") or 30)
            return record_microphone(output, duration)
        if action == "voiceover":
            from core.creator_voice import generate_voiceover
            output = str(args.get("output") or (Path.home() / "Desktop" / "brahma_creator_voiceover.mp3"))
            text_value = str(args.get("text") or args.get("script") or args.get("request") or "").strip()
            return generate_voiceover(
                text_value,
                output,
                voice=str(args.get("voice") or "en-US-GuyNeural"),
                rate=str(args.get("rate") or "+0%"),
            )
        if action == "status":
            return json.dumps(read_manifest(str(args.get("project") or ""))[1], ensure_ascii=False)
        if action in {"record_start", "record_stop"}:
            from actions.obs_control import obs_control
            return obs_control({"action": action}, player=player, speak=speak)
        return "Creator Studio supports tools, create/plan, produce, render, publish, metadata, script, rights_review, music_search, record_audio, voiceover, status, record_start, and record_stop."
    except Exception as exc:
        return f"Creator Studio failed: {exc}"
