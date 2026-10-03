"""Lightweight speech-rate and prosody profiles for Brahma Evo.

The Live native-audio model receives these as delivery guidance rather than as
unsupported numeric API parameters. Windows Edge TTS/SAPI fallbacks can use the
numeric rate values directly.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SpeechProfile:
    """Human-oriented delivery target for one conversational state."""

    state: str
    rate_percent: int
    pitch_hz: int
    pause_scale: float
    style: str

    @property
    def edge_rate(self) -> str:
        return f"{self.rate_percent:+d}%"

    @property
    def edge_pitch(self) -> str:
        return f"{self.pitch_hz:+d}Hz"

    @property
    def sapi_rate(self) -> int:
        # SAPI's practical range is much coarser than Edge TTS.
        return max(-10, min(10, round(self.rate_percent / 5)))

    def prompt_directive(self) -> str:
        rate_word = (
            "slightly faster" if self.rate_percent >= 5
            else "slightly slower" if self.rate_percent <= -5
            else "near normal speed"
        )
        pitch_word = (
            "slightly brighter" if self.pitch_hz >= 8
            else "slightly deeper" if self.pitch_hz <= -8
            else "near baseline pitch"
        )
        return (
            f"State={self.state}; target pace={rate_word} ({self.rate_percent:+d}% baseline); "
            f"pitch={pitch_word} ({self.pitch_hz:+d} Hz); pause scale={self.pause_scale:.2f}. "
            f"{self.style}"
        )


_BASE = {
    "neutral": (0, 0, 1.00, "Keep a relaxed, conversational cadence with natural sentence timing."),
    "warm": (-5, 8, 1.12, "Slow a touch, use a warmer cadence, and leave gentle space after acknowledgements."),
    "curious": (2, 12, 1.02, "Use a lightly lifted cadence and small pitch movement on genuine questions or discoveries."),
    "amused": (6, 16, 0.92, "Allow a little quicker timing and brighter delivery without sounding theatrical."),
    "concerned": (-8, -10, 1.22, "Slow down, soften the delivery, and leave extra space around important or sensitive points."),
    "stern": (-4, -12, 1.00, "Use a controlled, firm cadence with deliberate emphasis; never become abusive or demeaning."),
    "frustrated": (5, -16, 0.95, "Use tighter, more energetic timing with firm emphasis while staying composed and useful."),
    "urgent": (12, -6, 0.85, "Increase pace and reduce unnecessary pauses, but keep words clear and intelligible."),
}


def profile_for_state(state: str, intensity: float = 0.5) -> SpeechProfile:
    name = str(state or "neutral").strip().casefold()
    if name not in _BASE:
        name = "neutral"

    base_rate, base_pitch, pause_scale, style = _BASE[name]
    strength = max(0.35, min(1.0, float(intensity)))
    rate = round(base_rate * strength)
    pitch = round(base_pitch * strength)
    return SpeechProfile(
        state=name,
        rate_percent=rate,
        pitch_hz=pitch,
        pause_scale=pause_scale,
        style=style,
    )


def profile_prompt_block() -> str:
    lines = [
        "[SPEECH PROSODY]",
        "Change speaking speed and vocal tone with context; do not use one fixed cadence for every response.",
        "Let emotional intensity affect pace, pitch, emphasis, and pause length, while keeping articulation clear.",
        "Use small continuous variations within a response: slow slightly before important points, normalise during routine clauses, and move faster only for urgency or simple continuation.",
        "Do not over-act the emotion. Natural human speech has subtle rate changes, brief pauses, emphasis shifts, and imperfectly uniform rhythm.",
        "Preferred state map:",
    ]
    for name in ("neutral", "warm", "curious", "amused", "concerned", "stern", "frustrated", "urgent"):
        base_rate, base_pitch, pause_scale, _ = _BASE[name]
        lines.append(
            f"- {name}: about {base_rate:+d}% speed, {base_pitch:+d} Hz pitch, pause scale {pause_scale:.2f}."
        )
    lines.append(
        "These are targets, not rigid commands; prioritize intelligibility, turn-taking, and natural delivery."
    )
    return "\n".join(lines) + "\n"


def profile_for_text(text: str, *, state: str = "neutral", intensity: float = 0.5) -> SpeechProfile:
    """Small deterministic text-shape adjustment for fallback TTS."""
    profile = profile_for_state(state, intensity)
    value = str(text or "")
    rate = profile.rate_percent

    # Longer blocks benefit from a tiny slowdown; short confirmations can stay lively.
    if len(value) >= 220:
        rate -= 2
    elif len(value) <= 45:
        rate += 1

    return SpeechProfile(
        state=profile.state,
        rate_percent=max(-12, min(14, rate)),
        pitch_hz=profile.pitch_hz,
        pause_scale=profile.pause_scale,
        style=profile.style,
    )
