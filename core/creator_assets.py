"""Creator asset generation: captions and thumbnails."""
from __future__ import annotations
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any

def write_srt(cues: Any, destination: Path) -> str:
    def stamp(value: float) -> str:
        total_ms = int(round(max(0.0, float(value)) * 1000))
        ms = total_ms % 1000
        total = total_ms // 1000
        sec = total % 60
        minutes = total // 60
        minute = minutes % 60
        hour = minutes // 60
        return f"{hour:02d}:{minute:02d}:{sec:02d},{ms:03d}"
    lines = []
    index = 1
    for cue in cues if isinstance(cues, list) else []:
        if not isinstance(cue, dict):
            continue
        try:
            start, end = float(cue["start"]), float(cue["end"])
            value = " ".join(str(cue.get("text") or "").split())
        except (KeyError, TypeError, ValueError):
            continue
        if value and end > start:
            lines += [str(index), f"{stamp(start)} --> {stamp(end)}", value, ""]
            index += 1
    destination.write_text("\n".join(lines), encoding="utf-8")
    return str(destination)

def extract_thumbnail(video: str, destination: str, timestamp: float, text: str) -> str:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is not installed or not on PATH.")
    dest = Path(destination).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    frame = dest.with_suffix(".frame.jpg")
    result = subprocess.run(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", str(max(0.0, float(timestamp))),
         "-i", str(Path(video).resolve()), "-frames:v", "1", "-q:v", "2", str(frame)],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if result.returncode != 0 or not frame.exists():
        raise RuntimeError(result.stderr[-3000:] or "Thumbnail extraction failed.")
    try:
        from PIL import Image, ImageDraw, ImageFont
        image = Image.open(frame).convert("RGB")
        draw = ImageDraw.Draw(image, "RGBA")
        if text:
            font = ImageFont.load_default()
            lines = textwrap.wrap(text, width=34)[:4]
            box_h = 54 + 28 * len(lines)
            draw.rectangle((0, image.height - box_h, image.width, image.height), fill=(0, 0, 0, 175))
            y = image.height - box_h + 18
            for line in lines:
                draw.text((24, y), line, fill=(255, 255, 255), font=font, stroke_width=1, stroke_fill=(0, 0, 0))
                y += 28
        image.save(dest, quality=92)
    except Exception:
        shutil.copy2(frame, dest)
    try:
        frame.unlink()
    except OSError:
        pass
    return str(dest)
