from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


class CommandSafetyError(RuntimeError):
    """Raised when a trusted executable cannot be resolved safely."""



def resolve_trusted_executable(
    command: str,
    workspace: Path,
    *,
    windows_name: str | None = None,
) -> str:
    """Resolve a PATH executable while rejecting workspace/CWD shadows."""
    name = str(command).strip()
    if not name:
        raise CommandSafetyError("Executable name is empty.")

    resolved = shutil.which(name, path=os.environ.get("PATH"))
    if not resolved:
        raise CommandSafetyError(f"{name} executable could not be resolved safely.")

    executable = Path(resolved).resolve()
    if not executable.is_file():
        raise CommandSafetyError(f"Resolved {name} executable is not a regular file.")

    for forbidden_root in (Path(workspace).resolve(), Path.cwd().resolve()):
        try:
            executable.relative_to(forbidden_root)
        except ValueError:
            continue
        raise CommandSafetyError(
            f"{name} executable may not come from the repository or current directory."
        )

    if os.name == "nt" and windows_name and executable.name.casefold() != windows_name.casefold():
        raise CommandSafetyError(
            f"Windows {name} executable must resolve to {windows_name}."
        )

    return str(executable)

def resolve_git_executable(repo: Path) -> str:
    """Resolve Git without allowing a repository/CWD executable shadow."""
    return resolve_trusted_executable("git", repo, windows_name="git.exe")


def hidden_creationflags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
