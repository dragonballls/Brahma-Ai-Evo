import subprocess
import sys


def _run(*args: str) -> None:
    subprocess.run(
        [sys.executable, *args],
        check=True,
    )


def _chromium_ready() -> bool:
    probe = (
        "from playwright.sync_api import sync_playwright; "
        "p=sync_playwright().start(); "
        "b=p.chromium.launch(headless=True); "
        "b.close(); "
        "p.stop()"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


print("Installing Python requirements...")
_run("-m", "pip", "install", "--prefer-binary", "--disable-pip-version-check", "--no-input", "-r", "requirements.txt")

if _chromium_ready():
    print("Playwright Chromium runtime already ready; skipping browser download.")
else:
    print("Installing Playwright Chromium runtime...")
    _run("-m", "playwright", "install", "chromium")
    if not _chromium_ready():
        raise SystemExit("Playwright Chromium installation completed but the runtime probe still fails.")

print("\nSetup complete! Run 'python main.py' or start_brahma.bat to start Brahma Evo.")
