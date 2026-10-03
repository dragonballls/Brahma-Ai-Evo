"""Normalize and combine multiple creator source videos."""
from __future__ import annotations
import json, shutil, subprocess
from pathlib import Path

class CreatorIngestError(RuntimeError):
    pass

def _run(cmd: list[str], timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CreatorIngestError(f"Media command failed to start: {exc}") from exc

def _probe(path: Path) -> tuple[bool, float]:
    exe = shutil.which("ffprobe")
    if not exe:
        raise CreatorIngestError("FFprobe is not installed or not on PATH.")
    r = _run([exe, "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(path)], 60)
    if r.returncode != 0:
        raise CreatorIngestError(r.stderr[-2000:] or "Could not inspect source media.")
    data = json.loads(r.stdout or "{}")
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    duration = float((data.get("format") or {}).get("duration") or 0)
    return has_audio, duration

def prepare_sources(sources: list[str], destination: str) -> str:
    paths = [Path(x).expanduser().resolve() for x in sources if str(x).strip()]
    if not paths:
        raise CreatorIngestError("No source videos were supplied.")
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        raise CreatorIngestError("Missing source video(s): " + ", ".join(missing))
    if len(paths) == 1:
        return str(paths[0])
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise CreatorIngestError("FFmpeg is not installed or not on PATH.")
    dest = Path(destination).expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    filters, pairs = [], []
    for i, path in enumerate(paths):
        cmd += ["-i", str(path)]
        filters.append(f"[{i}:v]scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1[v{i}]")
        has_audio, duration = _probe(path)
        if has_audio:
            filters.append(f"[{i}:a]aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo[a{i}]")
        else:
            duration = max(0.05, duration)
            filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={duration}[a{i}]")
        pairs.append(f"[v{i}][a{i}]")
    filters.append("".join(pairs) + f"concat=n={len(paths)}:v=1:a=1[vout][aout]")
    cmd += ["-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dest)]
    r = _run(cmd)
    if r.returncode != 0:
        raise CreatorIngestError(r.stderr[-5000:] or "Failed to combine source videos.")
    if not dest.is_file():
        raise CreatorIngestError("Source normalization produced no output file.")
    return str(dest)
