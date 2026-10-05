"""Canonical user-facing capability catalog for Brahma Evo.

The goal is simple feature discovery: users should describe what they want in
ordinary language rather than memorizing internal tool names or command syntax.
The same catalog is used by the text/voice router and the chat quick actions.
"""
from __future__ import annotations

CAPABILITY_GROUPS: tuple[dict[str, object], ...] = (
    {
        "name": "Computer",
        "examples": ("open Chrome", "close an app", "take a screenshot", "change volume", "lock my PC"),
        "terms": ("computer", "pc", "desktop", "app", "program", "window", "screenshot", "screen", "volume", "brightness", "clipboard"),
    },
    {
        "name": "Web",
        "examples": ("search the web", "find flights", "look up the weather", "open a website"),
        "terms": ("web", "browser", "search", "weather", "flight", "website", "url", "internet"),
    },
    {
        "name": "Files & Documents",
        "examples": ("organize my files", "make an Excel sheet", "write a Word document", "make a PDF"),
        "terms": ("file", "folder", "document", "word", "excel", "spreadsheet", "pdf", "presentation", "powerpoint", "ppt", "organize"),
    },
    {
        "name": "Media",
        "examples": ("play music", "control Spotify", "upload a video", "control OBS"),
        "terms": ("music", "spotify", "youtube", "video", "obs", "media", "play", "pause"),
    },
    {
        "name": "Communication",
        "examples": ("send a message", "check Instagram", "send an email", "check my calendar"),
        "terms": ("message", "text", "email", "instagram", "discord", "calendar", "call", "phone"),
    },
    {
        "name": "Devices & Smart Home",
        "examples": ("show my devices", "turn on the living-room light", "connect my phone"),
        "terms": ("device", "phone", "tablet", "tv", "smart home", "light", "plug", "fan", "room"),
    },
    {
        "name": "Brahma",
        "examples": ("remember this", "improve yourself", "check your health", "undo that", "learn this skill"),
        "terms": ("remember", "memory", "improve yourself", "self code", "self-coding", "learn", "skill", "undo", "health", "evolution", "omniroute"),
    },
)

# These are deliberately distinctive enough to help routing. Generic words such as
# "app", "phone", "room", "play", and "web" stay out of the detector because
# they occur frequently in ordinary conversation.
CAPABILITY_ROUTING_TERMS: tuple[str, ...] = (
    "spotify", "instagram", "discord", "calendar", "spreadsheet", "excel",
    "powerpoint", "ppt", "smart home", "obs", "omniroute", "brightness",
    "screenshot", "clipboard", "bluetooth", "flight", "weather",
)

QUICK_ACTIONS: tuple[tuple[str, str], ...] = (
    ("Computer", "Take a screenshot"),
    ("Web", "Search the web for something useful"),
    ("Create", "Make a spreadsheet for me"),
    ("Media", "Play something on Spotify"),
    ("Devices", "Show my connected devices"),
    ("Brahma", "What can you do?"),
)

def _natural_examples() -> str:
    lines = []
    for group in CAPABILITY_GROUPS:
        examples = "; ".join(str(item) for item in group["examples"])
        lines.append(f"- {group['name']}: {examples}")
    return "\n".join(lines)

def prompt_block() -> str:
    return (
        "FEATURE USABILITY CONTRACT:\n"
        "- Users never need to know internal tool names, parameter names, or exact commands.\n"
        "- Interpret ordinary natural-language requests and map them to the appropriate capability.\n"
        "- Prefer the simplest matching capability; do not ask the user to restate a request using a special syntax.\n"
        "- Use tools for actions instead of explaining how the user could do the action manually.\n"
        "- When a request is ambiguous, ask one short clarifying question only when executing the wrong action would be materially different.\n"
        "- Preserve the user's requested goal while translating it into the tool's required parameters.\n"
        "NATURAL-LANGUAGE EXAMPLES:\n"
        f"{_natural_examples()}"
    )

def capability_terms() -> tuple[str, ...]:
    terms: list[str] = []
    for group in CAPABILITY_GROUPS:
        terms.extend(str(item).casefold() for item in group["terms"])
    return tuple(dict.fromkeys(terms))
