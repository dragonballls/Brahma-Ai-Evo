"""AI creator orchestration for Brahma Evo.

The engine follows the deterministic-manifest pattern used by MIT-licensed
dawn-cut, edit-ai, and autobroll: analyze first, plan second, render third.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.user_paths import get_user_data_dir

logger = logging.getLogger("brahma.creator")
CREATOR_ROOT = get_user_data_dir() / "CreatorProjects"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}

def _safe_name(value: str) -> str:
    text = "".join(c if c.isalnum() or c in "-_" else "-" for c in str(value or "").strip())
    return text.strip("-")[:80] or f"project-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

def project_dir(name: str) -> Path:
    path = CREATOR_ROOT / _safe_name(name)
    path.mkdir(parents=True, exist_ok=True)
    return path

def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")

def _ffprobe() -> str | None:
    return shutil.which("ffprobe")

def creator_tools() -> dict[str, Any]:
    return {
        "ffmpeg": _ffmpeg(),
        "ffprobe": _ffprobe(),
        "creator_root": str(CREATOR_ROOT),
        "video_understanding": True,
        "ai_metadata": True,
        "script_generation": True,
        "youtube_api": True,
        "obs_recording": True,
    }

def find_video(source: str | None = None) -> Path:
    if source:
        path = Path(source).expanduser().resolve()
        if path.is_file():
            return path
        raise FileNotFoundError(f"Video file not found: {path}")
    candidates: list[tuple[float, Path]] = []
    for root in (Path.home() / "Desktop", Path.home() / "Downloads", Path.home() / "Videos"):
        if not root.exists():
            continue
        for path in root.iterdir():
            if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
                try:
                    candidates.append((path.stat().st_mtime, path))
                except OSError:
                    pass
    candidates.sort(reverse=True)
    if not candidates:
        raise FileNotFoundError("No video was found in Desktop, Downloads, or Videos.")
    return candidates[0][1]

def _ai_json(prompt: str, system: str, profile: str = "smart") -> dict[str, Any]:
    from llm_client import client
    value = client.intelligent_json(prompt, system=system, profile=profile, max_tokens=5000)
    return value if isinstance(value, dict) else {}

def analyze_source(source: str, goal: str) -> dict[str, Any]:
    from actions.video_understanding import analyze_local_video
    question = (
        "Analyze this entire video for an AI editor. Return ONLY JSON with keys "
        "summary, audience, mood, highlights, scenes, transcript_cues, audio_notes, "
        "edit_opportunities, rights_flags. Highlights/scenes contain start and end seconds. "
        "Transcript cues contain start, end, and text. Identify the strongest moments, dead air, "
        "audio problems, important visuals, on-screen text, and useful B-roll opportunities. "
        "Do not invent events. Production goal: " + goal
    )
    raw = analyze_local_video(source, question=question)
    from actions.video_understanding import _result_text
    text = _result_text({"output": [{"content": [{"type": "text", "text": raw}]}]}) if isinstance(raw, dict) else str(raw)
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {"summary": text, "highlights": [], "scenes": [], "transcript_cues": [], "audio_notes": [], "edit_opportunities": [], "rights_flags": []}
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        data = {"summary": text, "highlights": [], "scenes": [], "transcript_cues": [], "audio_notes": [], "edit_opportunities": [], "rights_flags": []}
    data["source"] = source
    return data

def plan_edit(goal: str, analysis: dict[str, Any], platform: str, target_length: str) -> dict[str, Any]:
    prompt = (
        "Build a deterministic editing manifest for this real video. Return ONLY JSON with keys "
        "style, aspect_ratio, effect_profile, fade_in_seconds, fade_out_seconds, keep_segments, "
        "audio_cleanup, normalize_audio, add_captions, music_mood, music_volume, music_ducking, "
        "thumbnail_timestamp, thumbnail_text, notes. keep_segments is an array of "
        "objects with start,end,speed,reason. Use only timestamps present in the analysis. "
        "An empty list means preserve the complete source. "
        f"platform={platform}; target_length={target_length or 'natural'}; goal={goal}; "
        f"analysis={json.dumps(analysis, ensure_ascii=False)[:45000]}"
    )
    try:
        return _ai_json(prompt, "You are Brahma's deterministic editor planner. Never fabricate footage.")
    except Exception as exc:
        logger.warning("Edit planning failed: %s", exc)
        return {
            "style": "clean",
            "aspect_ratio": "9:16" if platform in {"youtube_short", "tiktok", "instagram_reel", "reels"} else "16:9",
            "effect_profile": "clean",
            "fade_in_seconds": 0,
            "fade_out_seconds": 0,
            "keep_segments": [],
            "audio_cleanup": True,
            "normalize_audio": True,
            "add_captions": bool(analysis.get("transcript_cues")),
            "music_mood": "",
            "music_volume": 0.12,
            "thumbnail_timestamp": 0,
            "thumbnail_text": goal[:45],
            "notes": "Fallback plan; source preserved.",
        }

def metadata(goal: str, analysis: dict[str, Any], plan: dict[str, Any], platform: str) -> dict[str, Any]:
    prompt = (
        "Generate the complete creator package from the actual video. Return ONLY JSON with keys "
        "title, alternative_titles, description, hashtags, tags, chapters, pinned_comment, "
        "thumbnail_text, thumbnail_concept, hook, short_title, short_description, short_hashtags, keywords. "
        "Keep every claim grounded in the analysis. Hashtags must start with #. "
        f"platform={platform}; goal={goal}; analysis={json.dumps(analysis, ensure_ascii=False)[:35000]}; "
        f"plan={json.dumps(plan, ensure_ascii=False)[:12000]}"
    )
    try:
        data = _ai_json(prompt, "You are Brahma's accurate YouTube and social media packaging director.")
    except Exception as exc:
        logger.warning("Metadata generation failed: %s", exc)
        words = ["".join(c for c in w if c.isalnum()).lower() for w in goal.split()]
        words = list(dict.fromkeys(w for w in words if len(w) >= 3))[:12]
        return {
            "title": (goal or "New Video")[:100],
            "alternative_titles": [goal[:90], f"{goal[:65]} Explained"],
            "description": str(analysis.get("summary") or goal),
            "hashtags": ["#" + w for w in words[:8]],
            "tags": words,
            "chapters": [],
            "pinned_comment": "What part stood out to you?",
            "thumbnail_text": goal[:40],
            "thumbnail_concept": "Use the strongest real frame.",
            "hook": goal[:80],
            "short_title": goal[:55],
            "short_description": str(analysis.get("summary") or goal)[:500],
            "short_hashtags": ["#" + w for w in words[:8]],
            "keywords": words,
        }
    data["title"] = str(data.get("title") or goal)[:100]
    data["hashtags"] = [
        x if str(x).startswith("#") else "#" + str(x).replace(" ", "")
        for x in data.get("hashtags", [])
        if str(x).strip()
    ][:20]
    data["tags"] = [str(x) for x in data.get("tags", []) if str(x).strip()][:30]
    data["alternative_titles"] = [str(x) for x in data.get("alternative_titles", []) if str(x).strip()][:5]
    return data

def script(goal: str, analysis: dict[str, Any], length: str = "medium") -> dict[str, Any]:
    prompt = (
        "Create a creator-ready script grounded in this real video analysis. Return ONLY JSON with "
        "title, hook, sections, outro, b_roll_notes, on_screen_text. sections contain heading, "
        "narration, duration_seconds. Do not invent unsupported claims. "
        f"length={length}; goal={goal}; analysis={json.dumps(analysis, ensure_ascii=False)[:40000]}"
    )
    try:
        return _ai_json(prompt, "You are Brahma's factual video scriptwriter.")
    except Exception as exc:
        logger.warning("Script generation failed: %s", exc)
        return {
            "title": goal[:100] or "Video Script",
            "hook": str(analysis.get("summary") or goal)[:220],
            "sections": [{"heading": "Main", "narration": str(analysis.get("summary") or goal), "duration_seconds": 60}],
            "outro": "Thanks for watching.",
            "b_roll_notes": [],
            "on_screen_text": [],
        }

def rights_review(analysis: dict[str, Any], music_path: str | None) -> dict[str, Any]:
    flags = list(analysis.get("rights_flags") or [])
    if music_path:
        flags.append("Verify that the supplied music is licensed for your intended use.")
    else:
        flags.append("Use music you own or music with terms that permit your intended use.")
    return {
        "status": "review_required",
        "flags": flags,
        "note": "Brahma will not disguise copyrighted material or defeat content-identification systems. It can remove or replace unverified music.",
    }

def write_manifest(directory: Path, data: dict[str, Any]) -> Path:
    path = directory / "project.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path

def read_manifest(project: str) -> tuple[Path, dict[str, Any]]:
    raw = Path(project).expanduser()
    path = raw if raw.name == "project.json" else project_dir(project) / "project.json"
    if not path.is_file():
        raise FileNotFoundError(f"Creator project not found: {path}")
    return path.parent, json.loads(path.read_text(encoding="utf-8"))

def create_creator_project(args: dict[str, Any]) -> dict[str, Any]:
    requested = args.get("source_paths") or args.get("sources")
    if isinstance(requested, str):
        requested = [x.strip() for x in requested.split("|") if x.strip()]
    source_list = [str(x) for x in requested] if isinstance(requested, list) else []
    if not source_list:
        source_list = [str(find_video(args.get("source") or args.get("video_path")))]
    goal = str(args.get("goal") or args.get("request") or "Create a polished YouTube video")
    platform = str(args.get("platform") or "youtube").lower()
    project_name = str(args.get("project") or Path(source_list[0]).stem)
    directory = project_dir(project_name)
    if len(source_list) > 1:
        from core.creator_ingest import prepare_sources
        source = Path(prepare_sources(source_list, str(directory / "ingested.mp4")))
    else:
        source = Path(source_list[0]).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"Video file not found: {source}")
    analysis = analyze_source(str(source), goal)
    plan = plan_edit(goal, analysis, platform, str(args.get("target_length") or ""))
    data = metadata(goal, analysis, plan, platform)
    script_data = script(goal, analysis, str(args.get("script_length") or "medium"))
    rights = rights_review(analysis, args.get("music_path"))
    captions_path = None
    cues = analysis.get("transcript_cues") or []
    if cues:
        from core.creator_assets import write_srt
        captions_path = write_srt(cues, directory / "captions.srt")
    thumbnail_path = None
    try:
        from core.creator_assets import extract_thumbnail
        thumbnail_path = extract_thumbnail(
            str(source),
            str(directory / "thumbnail.jpg"),
            float(plan.get("thumbnail_timestamp") or 0),
            str(plan.get("thumbnail_text") or data.get("thumbnail_text") or data.get("title") or ""),
        )
    except Exception as exc:
        logger.warning("Thumbnail generation skipped: %s", exc)
    manifest = {
        "schema_version": 1,
        "project_id": directory.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source),
        "goal": goal,
        "platform": platform,
        "music_path": str(Path(args["music_path"]).expanduser().resolve()) if args.get("music_path") else None,
        "analysis": analysis,
        "plan": plan,
        "metadata": data,
        "script": script_data,
        "rights_review": rights,
        "captions_path": captions_path,
        "thumbnail_path": thumbnail_path,
        "rendered_video": None,
    }
    manifest_path = write_manifest(directory, manifest)
    return {
        "ok": True,
        "project": str(directory),
        "manifest": str(manifest_path),
        "analysis_summary": analysis.get("summary"),
        "plan": plan,
        "metadata": data,
        "script": script_data,
        "rights_review": rights,
        "captions": captions_path,
        "thumbnail": thumbnail_path,
    }

def render_creator_project(project: str) -> dict[str, Any]:
    from core.creator_render import render_project
    directory, manifest = read_manifest(project)
    output = directory / f"{manifest['project_id']}_final.mp4"
    result = render_project(manifest, str(output))
    manifest["rendered_video"] = str(output)
    manifest["rendered_at"] = datetime.now(timezone.utc).isoformat()
    write_manifest(directory, manifest)
    return result | {"project": str(directory), "metadata": manifest.get("metadata", {}), "rights_review": manifest.get("rights_review", {})}

def refresh_creator_metadata(project: str) -> dict[str, Any]:
    directory, manifest = read_manifest(project)
    manifest["metadata"] = metadata(
        str(manifest.get("goal") or ""),
        manifest.get("analysis") or {},
        manifest.get("plan") or {},
        str(manifest.get("platform") or "youtube"),
    )
    write_manifest(directory, manifest)
    return manifest["metadata"]

def creator_script(project: str, length: str = "medium") -> dict[str, Any]:
    directory, manifest = read_manifest(project)
    value = script(str(manifest.get("goal") or ""), manifest.get("analysis") or {}, length)
    manifest["script"] = value
    write_manifest(directory, manifest)
    return value

def creator_rights_review(project: str) -> dict[str, Any]:
    _, manifest = read_manifest(project)
    return rights_review(manifest.get("analysis") or {}, manifest.get("music_path"))

def publish_creator_project(project: str, privacy: str, playlist_id: str | None) -> dict[str, Any]:
    from core.creator_publish import publish_project
    _, manifest = read_manifest(project)
    return publish_project(manifest, privacy=privacy, playlist_id=playlist_id)


def produce_creator_project(args: dict[str, Any]) -> dict[str, Any]:
    result = create_creator_project(args)
    rendered = render_creator_project(result["project"])
    return {
        "ok": True,
        "project": result["project"],
        "analysis_summary": result.get("analysis_summary"),
        "metadata": rendered.get("metadata") or result.get("metadata"),
        "script": result.get("script"),
        "rights_review": rendered.get("rights_review") or result.get("rights_review"),
        "render": rendered,
    }
