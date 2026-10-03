"""Generate Creator narration audio with Edge TTS when available."""
from __future__ import annotations
import shutil
import subprocess
from pathlib import Path

def generate_voiceover(text: str, destination: str, voice: str = "en-US-GuyNeural", rate: str = "+0%") -> str:
    value = str(text or "").strip()
    if not value:
        raise ValueError("Voiceover text cannot be empty.")
    target = Path(destination).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    exe = shutil.which("edge-tts")
    if not exe:
        raise RuntimeError("The edge-tts command is not installed or not on PATH.")
    result = subprocess.run(
        [exe, "--voice", voice, "--rate", rate, "--text", value, "--write-media", str(target)],
        capture_output=True, text=True, timeout=300, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-3000:] or "Voiceover generation failed.")
    if not target.is_file():
        raise RuntimeError("Voiceover generation produced no output file.")
    return str(target)
