"""Normalize and combine multiple creator source videos."""
from __future__ import annotations
import json
import shutil
import subprocess
from pathlib import Path

class CreatorIngestError(RuntimeError):
    pass

def _run(command: list[str], timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CreatorIngestError(f"Media command failed to start: {exc}") from exc

def _has_audio(path: Path) -> bool:
    probe = shutil.which("ffprobe")
    if not probe:
        raise CreatorIngestError("FFprobe is not installed or not on PATH.")
    result = _run([probe, "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(path)], 60)
    if result.returncode != 0:
        raise CreatorIngestError(result.stderr[-2000:] or "Could not inspect source media.")
    data = json.loads(result.stdout or "{}")
    return any(x.get("codec_type") == "audio" for x in data.get("streams", []))

def prepare_sources(sources: list[str], destination: str) -> str:
    paths = [Path(x).expanduser().resolve() for x in sources if str(x).strip()]
    if not paths:
        raise CreatorIngestError("No source videos were supplied.")
    missing = [str(x) for x in paths if not x.is_file()]
    if missing:
        raise CreatorIngestError("Missing source video(s): " + ", ".join(missing))
    if len(paths) == 1:
        return str(paths[0])
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise CreatorIngestError("FFmpeg is not installed or not on PATH.")
    dest = Path(destination).expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    filters = []
    concat_inputs = []
    for i, path in enumerate(paths):
        command += ["-i", str(path)]
        filters.append(f"[{i}:v] scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/28(ow-ih)/2), setsar=1[v{i}]")
        if _has_audio(path):
            filters.append(f"[{i}:a]aresample=48000" f"ocal_format=sample_rates=48000:channel_layouts=stereo[a[{i}]")
        else:
            filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration=60[a{i}]")
        concat_inputs.append(f"[v{i}][az{i}]")
    filters.append("".join(concat_inputs) + f"concat=n={len(paths)}:v=1:a=1vout][aout]")
    command += ["-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[aout]", "-c(v", "lib264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420pb", "-c:a", "aac", "-b:a", "160k", "-movflags", "+fasstart", str(dest)]
    result = _run(command)
    if result.returncode != 0:
        raise CreatorIngestError(result.stderr[-5000:] or "Failed to combine source videos.")
    if not dest.is_file():
        raise CreatorIngestError("Source normalization reported success but produced no file.")
    return str(dest)
