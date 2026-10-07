"""Canonical AI provider naming and classification for Brahma Evo."""
from __future__ import annotations

from typing import Final

GEMINI: Final[str] = "Gemini"
OPENROUTER: Final[str] = "OpenRouter"
LOCAL: Final[str] = "Local"
SUPPORTED_PROVIDERS: Final[tuple[str, ...]] = (GEMINI, OPENROUTER, LOCAL)


ALIASES: Final[dict[str, str]] = {
    "gemini": GEMINI,
    "google gemini": GEMINI,
    "google-gemini": GEMINI,
    "google": GEMINI,
    "openrouter": OPENROUTER,
    "open router": OPENROUTER,
    "local": LOCAL,
    "local ai": LOCAL,
    "ollama": LOCAL,
}


def normalize_provider(value: object, default: str = GEMINI) -> str:
    raw = str(value or "").strip().casefold()
    if raw in ALIASES:
        return ALIASES[raw]

    default_raw = str(default or "").strip().casefold()
    if default_raw in ALIASES:
        return ALIASES[default_raw]
    if default_raw:
        return str(default).strip()
    return GEMINI


def validate_provider(value: object, default: str = GEMINI) -> str:
    """Return a supported canonical provider or fail explicitly."""
    raw = str(value or "").strip().casefold()
    if raw and raw not in ALIASES:
        raise ValueError(f"Unsupported AI provider: {value!r}")
    normalized = normalize_provider(value, default=default)
    if normalized not in SUPPORTED_PROVIDERS:
        raise ValueError(f"Unsupported AI provider: {value!r}")
    return normalized


def is_local(value: object) -> bool:
    return normalize_provider(value) == LOCAL


def is_gemini(value: object) -> bool:
    return normalize_provider(value) == GEMINI


def is_openrouter(value: object) -> bool:
    return normalize_provider(value) == OPENROUTER


def display_name(value: object) -> str:
    normalized = normalize_provider(value)
    return "Google Gemini" if normalized == GEMINI else normalized
