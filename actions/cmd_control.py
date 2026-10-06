"""Constrained command/open adapter used by the legacy AgentExecutor planner.

This module intentionally does not provide unrestricted shell execution. It supports
the planner's existing benign "open this file/app" workflow and a small allowlist of
read-only diagnostic commands. Complex UI actions should use computer_control.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

SAFE_COMMANDS = {
    "whoami",
    "hostname",
    "ipconfig",
    "tasklist",
    "systeminfo",
    "where",
    "dir",
    "echo",
}

_BLOCKED_OPEN_EXTENSIONS = {
    ".exe", ".com", ".bat", ".cmd", ".ps1", ".psm1", ".msi", ".msp",
    ".scr", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta",
    ".lnk", ".url", ".website", ".msc", ".cpl", ".reg", ".inf", ".chm",
    ".scf", ".gadget",
    ".py", ".pyw", ".pyc", ".sh", ".bash", ".zsh", ".fish",
}


def _resolve_user_path(value: str) -> Path:
    target = (value or "").strip().strip('"')
    aliases = {
        "desktop": Path.home() / "Desktop",
        "downloads": Path.home() / "Downloads",
        "documents": Path.home() / "Documents",
        "pictures": Path.home() / "Pictures",
        "music": Path.home() / "Music",
        "videos": Path.home() / "Videos",
    }
    lowered = target.casefold()
    if lowered in aliases:
        return aliases[lowered]
    return Path(target).expanduser().resolve()


def _open_target(task: str) -> str | None:
    lowered = task.casefold().strip()
    if not lowered.startswith("open "):
        return None

    remainder = task.strip()[5:].strip()
    if " with notepad" in remainder.casefold():
        remainder = remainder[:remainder.casefold().rfind(" with notepad")].strip()
        if " on desktop" in remainder.casefold():
            remainder = remainder[:remainder.casefold().rfind(" on desktop")].strip()
        target = _resolve_user_path(str(Path.home() / "Desktop" / remainder))
        if not target.exists():
            target = _resolve_user_path(remainder)
        if target.exists() and os.name == "nt":
            try:
                proc = subprocess.Popen(
                    ["notepad.exe", str(target)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                return f"Opened {target} with Notepad." if proc.poll() is None else f"Could not open {target} with Notepad."
            except OSError as exc:
                return f"Could not open {target} with Notepad: {exc}"

    # Try the literal target first, then common user folders.
    candidates = [_resolve_user_path(remainder)]
    for root in ("Desktop", "Downloads", "Documents"):
        candidates.append(Path.home() / root / remainder)

    target = next((p for p in candidates if p.exists()), None)
    if target is None:
        return f"Could not find the requested file or application: {remainder}"

    try:
        resolved_target = target.resolve(strict=True)
    except OSError as exc:
        return f"Could not resolve the requested target safely: {exc}"

    if resolved_target.is_file() and resolved_target.suffix.casefold() in _BLOCKED_OPEN_EXTENSIONS:
        return "Opening executable, script, shortcut, or shell-link files is blocked by the legacy command adapter. Use a dedicated, explicit application-control action."
    if target.is_symlink():
        # The resolved extension is checked above; block remaining symlink indirection
        # so filesystem changes cannot turn a previously safe target into code execution.
        return "Opening symlink targets is blocked by the legacy command adapter."

    if os.name == "nt":
        os.startfile(str(resolved_target))
    else:
        try:
            proc = subprocess.Popen(
                ["xdg-open", str(target)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if proc.poll() is not None:
                return f"Could not open {target}."
        except OSError as exc:
            return f"Could not open {target}: {exc}"
    return f"Opened {target}."


def cmd_control(
    parameters: dict[str, Any] | None = None,
    response: Any = None,
    player: Any = None,
    session_memory: Any = None,
    speak: Any = None,
) -> str:
    p = parameters or {}
    task = str(p.get("task") or p.get("command") or "").strip()
    if not task:
        return "A command or open task is required."

    opened = _open_target(task)
    if opened:
        return opened

    try:
        argv = shlex.split(task, posix=os.name != "nt")
    except ValueError as exc:
        return f"Invalid command syntax: {exc}"

    if not argv:
        return "A command or open task is required."

    if "/" in argv[0] or "\\" in argv[0] or Path(argv[0]).is_absolute():
        return "Explicit executable paths are not permitted through the legacy command adapter."
    command_name = Path(argv[0]).name.casefold()
    if command_name.endswith(".exe"):
        command_name = command_name[:-4]

    if command_name not in SAFE_COMMANDS:
        return (
            f"Command '{argv[0]}' is not allowed through the legacy command adapter. "
            "Use computer_control for supported desktop actions."
        )

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            stdin=subprocess.DEVNULL,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"Command failed to start: {exc}"

    output = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0:
        return f"Command failed (exit {proc.returncode}): {output[:2000]}"
    return output[:8000] or f"Command '{argv[0]}' completed successfully."


run = cmd_control
