"""Deterministic FFmpeg renderer for Creator Studio."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from core.creator_engine import CreatorError, _safe_segments

def _run(command: list[str], timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CreatorError(f"FFmpeg failed to start: {exc}") from exc

def _probe(source: str) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise CreatorError("FFprobe is not installed or not on PATH.")
    result = _run([
        ffprobe, "-v", "error",
        "-show_entries", "format=duration:stream=codec_type,width,height",
        "-of", "json", source
    ], 60)
    if result.returncode != 0:
        raise CreatorError(result.stderr[-3000:] or "FFprobe failed.")
    import json
    return json.loads(result.stdout)

def _segment_filters(info: dict[str, Any], segments: list[dict[str, Any]]) -> tuple[str, str]:
    streams = info.get("streams") or []
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if not segments:
        return "[0:v]setpts=PTS-STARTPTS[vout]", "[0:a]asetpts=PTS-STARTPTS[aout]" if has_audio else ""
    parts, videos, audios = [], [], []
    for i, seg in enumerate(segments):
        start, end, speed = seg["start"], seg["end"], seg["speed"]
        parts.append(f"[0:v]trim=start={start}:end={end},setpts=(PTS-STARTPTS)/{speed}[v{i}]")
        videos.append(f"[v{i}]")
        if has_audio:
            value = float(speed)
            tempo = []
            while value > 2:
                tempo.append("atempo=2")
                value /= 2
            while value < 0.5:
                tempo.append("atempo=0.5")
                value /= 0.5
            tempo.append(f"atempo={value}")
            parts.append(f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS,{','.join(tempo)}[a{i}]")
            audios.append(f"[a{i}]")
    if has_audio:
        parts.append("".join(videos + audios) + f"concat=n={len(segments)}:v=1:a=1[vcat][acat]")
        return ";".join(parts) + ";[vcat]setpts=PTS-STARTPTS[vout]", "[acat]asetpts=PTS-STARTPTS[aout]"
    parts.append("".join(videos) + f"concat=n={len(segments)}:v=1:a=0[vout]")
    return ";".join(parts), ""

def render_project(manifest: dict[str, Any], destination: str) -> dict[str, Any]:
    source = Path(str(manifest.get("source") or "")).expanduser().resolve()
    if not source.is_file():
        raise CreatorError(f"Source video not found: {source}")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise CreatorError("FFmpeg is not installed or not on PATH.")
    info = _probe(str(source))
    duration = float((info.get("format") or {}).get("duration") or 0)
    segments = _safe_segments((manifest.get("plan") or {}).get("keep_segments"), duration)
    vf, af = _segment_filters(info, segments)
    filters = [vf] if vf else []
    if af:
        filters.append(af)

    video_ref = "[vout]"
    streams = info.get("streams") or []
    audio_ref = "[aout]" if any(s.get("codec_type") == "audio" for s in streams) else ""
    plan = manifest.get("plan") or {}

    music_path = manifest.get("music_path")
    command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source)]

    if plan.get("add_captions") and manifest.get("captions_path"):
        caption = Path(str(manifest["captions_path"])).expanduser().resolve()
        if caption.is_file():
            escaped = str(caption).replace("\\", "/").replace(":", "\\:")
            filters.append(f"{video_ref}subtitles='{escaped}'[captioned]")
            video_ref = "[captioned]"

    if audio_ref and plan.get("audio_cleanup"):
        filters.append(
            f"{audio_ref}highpass=f=80,lowpass=f=15000,afftdn=nr=12:nf=-25,"
            "acompressor=threshold=-18dB:ratio=3:attack=20:release=200[clean]"
        )
        audio_ref = "[clean]"

    if audio_ref and plan.get("normalize_audio"):
        filters.append(f"{audio_ref}loudnorm=I=-16:TP=-1.5:LRA=11[norm]")
        audio_ref = "[norm]"

    music = Path(str(music_path)).expanduser().resolve() if music_path else None
    if music and music.is_file() and audio_ref:
        command += ["-stream_loop", "-1", "-i", str(music)]
        volume = max(0.0, min(0.5, float(plan.get("music_volume", 0.12) or 0.12)))
        filters.append(f"[1:a]volume={volume}[music];{audio_ref}[music]amix=inputs=2:duration=first:dropout_transition=2[mixed]")
        audio_ref = "[mixed]"

    if filters:
        command += ["-filter_complex", ";".join(filters)]
    command += ["-map", video_ref]
    if audio_ref:
        command += ["-map", audio_ref]
    command += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
        "-pix_fmt", "yuv420p",
    ]
    if audio_ref:
        command += ["-c:a", "aac", "-b:a", "192k"]
    command += ["-movflags", "+faststart", str(Path(destination).resolve())]

    result = _run(command)
    if result.returncode != 0:
        raise CreatorError(result.stderr[-6000:] or "FFmpeg render failed.")
    output = Path(destination).resolve()
    if not output.is_file():
        raise CreatorError("FFmpeg reported success but no output file exists.")
    return {
        "ok": True,
        "output": str(output),
        "size_mb": round(output.stat().st_size / (1024 * 1024), 2),
        "segments_used": len(segments),
        "audio_cleanup": bool(plan.get("audio_cleanup")),
        "captions": bool(plan.get("add_captions") and manifest.get("captions_path")),
        "music": bool(music and music.is_file()),
    }
