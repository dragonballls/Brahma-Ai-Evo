"""Explicit language-lock policy for Brahma Evo conversations."""

from __future__ import annotations


DEFAULT_LANGUAGE = "English"


def prompt_block() -> str:
    return """[LANGUAGE LOCK]
- Use the current conversation language and preserve it consistently.
- Default language is English unless the conversation has been explicitly switched to another language.
- NEVER switch languages merely because the user writes, quotes, pastes, references, or asks about text in another language.
- NEVER infer a requested language change from automatic language detection, code, filenames, URLs, tool results, copied content, names, or examples.
- Change response language only when the user explicitly requests it (for example: "speak Spanish", "answer in Hindi", "reply in Japanese", "switch to French").
- An explicit translation request authorizes the requested target language for that translation; do not treat the source text itself as an instruction to change the conversation language.
- Once explicitly switched, remain in that language until the user explicitly requests another language or explicitly asks to switch back.
- Keep technical identifiers, code, commands, filenames, URLs, and quoted text exactly as needed; do not translate them unless requested.
- Never mix languages for style, emphasis, filler, emotion, or speech realism when no language switch was explicitly requested.
- Never mix languages for emotion, fillers, emphasis, or style without an explicit language request.
"""


def is_language_switch_request(text: str) -> bool:
    """Conservative helper for explicit user language-switch requests."""
    value = str(text or "").casefold().strip()
    if not value:
        return False

    markers = (
        "speak in ",
        "answer in ",
        "reply in ",
        "respond in ",
        "use the ",
        "switch to ",
        "change to ",
        "talk in ",
        "from now on, speak ",
        "from now on speak ",
    )
    return any(marker in value for marker in markers)
