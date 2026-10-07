from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


class CommandSafetyError(RuntimeError):
    """Raised when a trusted executable cannot be resolved safely."""


def resolve_git_executable(repo: Path) -> str:
    """Resolve Git without allowing a repository/CWD executable shadow."""
    resolved = shutil.which("git", path=os.environ.get("PATH"))
    if not resolved:
        raise CommandSafetyError("Git executable could not be resolved safely.")

    executable = Path(resolved).resolve()
    if not executable.is_file():
        raise CommandSafetyError("Resolved Git executable is not a regular file.")

    for forbidden_root in (Path(repo).resolve(), Path.cwd().resolve()):
        try:
            executable.relative_to(forbidden_root)
        except ValueError:
            continue
        raise CommandSafetyError(
            "Git executable may not come from the repository or current directory."
        )

    if os.name == "nt" and executable.name.casefold() != "git.exe":
        raise CommandSafetyError("Windows Git executable must resolve to git.exe.")

    return str(executable)


def hidden_creationflags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
