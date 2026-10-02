"""Bootstrap Brahma Evo into a project-local virtual environment."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run(*args: str) -> None:
    subprocess.run(list(args), cwd=ROOT, check=True)


def main() -> None:
    if not PYTHON.exists():
        print(f"Creating virtual environment: {VENV}")
        run(sys.executable, "-m", "venv", str(VENV))

    print("Updating pip tooling...")
    run(str(PYTHON), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel")

    print("Installing Brahma Evo dependencies...")
    run(str(PYTHON), "-m", "pip", "install", "-r", "requirements.txt")

    print("Installing Playwright browsers...")
    run(str(PYTHON), "-m", "playwright", "install")

    main_py = "main.py"
    if os.name == "nt":
        launcher = VENV / "Scripts/pythonw.exe"
        print(f"Setup complete. Launch with: {launcher} {ROOT / main_py}")
    else:
        print(f"Setup complete. Launch with: {PYTHON} {ROOT / main_py}")


if __name__ == "__main__":
    main()
