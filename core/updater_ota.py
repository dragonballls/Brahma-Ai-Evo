"""Verified release updater for Brahma Evo.

OTA updates are only executed after the downloaded installer matches the release
asset digest published by GitHub (or a SHA256SUMS sidecar in the same release).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
import subprocess

from core.runtime_paths import GITHUB_OWNER, GITHUB_REPOSITORY

GITHUB_REPO = f"{GITHUB_OWNER}/{GITHUB_REPOSITORY}"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}"


def get_current_version() -> str:
    try:
        base_dir = Path(sys._MEIPASS) if hasattr(sys, "_MEIPASS") else Path(__file__).resolve().parent.parent
        version_file = base_dir / "version.txt"
        if version_file.exists():
            import re
            text = version_file.read_text(encoding="utf-8")
            match = re.search(r"ProductVersion', '([^']+)'", text)
            if match:
                return match.group(1).strip()
    except Exception:
        pass
    return "1.0.0"


def parse_semver(ver: str) -> tuple[int, ...]:
    value = str(ver or "").strip().lstrip("v")
    parts = value.split(".")
    return tuple(int(x) if x.isdigit() else 0 for x in parts)


def _get_release() -> dict | None:
    req = urllib.request.Request(
        f"{GITHUB_API}/releases/latest",
        headers={"User-Agent": "BrahmaEvo-OTA", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        data = json.loads(response.read().decode("utf-8"))
    return data if isinstance(data, dict) else None


def _asset_digest(release: dict, asset: dict) -> str | None:
    digest = str(asset.get("digest") or "").strip()
    if digest.lower().startswith("sha256:"):
        return digest.split(":", 1)[1].strip().lower()
    if len(digest) == 64 and all(ch in "0123456789abcdefABCDEF" for ch in digest):
        return digest.lower()

    assets = release.get("assets") or []
    sidecars = [
        item for item in assets
        if str(item.get("name") or "").lower() in {"sha256sums.txt", "sha256sum.txt"}
        or str(item.get("name") or "").lower().endswith((".sha256", ".sha256.txt"))
    ]
    target_name = str(asset.get("name") or "").strip()
    for sidecar in sidecars:
        url = str(sidecar.get("browser_download_url") or "").strip()
        if not url:
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "BrahmaEvo-OTA"})
            with urllib.request.urlopen(req, timeout=10) as response:
                manifest = response.read().decode("utf-8", errors="replace")
            for line in manifest.splitlines():
                fields = line.strip().replace("*", " ").split()
                if len(fields) >= 2 and fields[1].strip() == target_name:
                    candidate = fields[0].strip().lower()
                    if len(candidate) == 64 and all(ch in "0123456789abcdef" for ch in candidate):
                        return candidate
        except Exception:
            continue
    return None


def _release_asset(release: dict, url: str) -> dict | None:
    for asset in release.get("assets") or []:
        if str(asset.get("browser_download_url") or "").strip() == str(url).strip():
            return asset
    return None


def check_for_updates() -> dict | None:
    """Return a newer release only when its installer has a verifiable SHA-256 digest."""
    try:
        data = _get_release()
        if not data:
            return None
        latest_version = str(data.get("tag_name") or "").strip()
        if not latest_version or parse_semver(latest_version) <= parse_semver(get_current_version()):
            return None

        assets = [a for a in (data.get("assets") or []) if isinstance(a, dict)]
        setup_asset = next(
            (
                a for a in assets
                if "setup" in str(a.get("name") or "").lower()
                or "installer" in str(a.get("name") or "").lower()
            ),
            None,
        )
        if setup_asset is None:
            setup_asset = next(
                (a for a in assets if str(a.get("name") or "").lower().endswith(".exe")),
                None,
            )
        if setup_asset is None:
            return None

        digest = _asset_digest(data, setup_asset)
        url = str(setup_asset.get("browser_download_url") or "").strip()
        if not url or not digest:
            print("[OTA] Refusing unsigned/unhashed release installer.")
            return None

        return {
            "version": latest_version,
            "url": url,
            "size": int(setup_asset.get("size") or 0),
            "sha256": digest,
            "notes": data.get("body", ""),
        }
    except Exception as e:
        print(f"[OTA] Failed to check for updates: {e}")
        return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_and_apply_update(
    url: str,
    ui_callback=None,
    expected_sha256: str | None = None,
    expected_size: int = 0,
):
    """Download, verify, then launch a release installer."""
    try:
        digest = str(expected_sha256 or "").strip().lower()
        release = _get_release()
        asset = _release_asset(release or {}, url)
        if asset is None:
            raise RuntimeError("OTA update rejected: URL is not a current GitHub release asset.")
        if not digest:
            digest = _asset_digest(release or {}, asset) or ""
        if not digest:
            raise RuntimeError("OTA update rejected: release installer has no verifiable SHA-256 digest.")

        update_dir = __import__("core.user_paths", fromlist=["get_user_data_dir"]).get_user_data_dir() / "updates"
        update_dir.mkdir(parents=True, exist_ok=True)
        setup_path = update_dir / "BrahmaEvo_Setup_Update.exe"
        temp_path = setup_path.with_suffix(".exe.download")

        req = urllib.request.Request(url, headers={"User-Agent": "BrahmaEvo-OTA"})
        with urllib.request.urlopen(req, timeout=30) as response, temp_path.open("wb") as output:
            total_size = int(response.info().get("Content-Length", 0))
            downloaded = 0
            max_download = max(int(expected_size or 0), 2 * 1024 * 1024 * 1024)
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > max_download:
                    raise RuntimeError("OTA installer is unexpectedly large.")
                output.write(chunk)
                if ui_callback and total_size > 0:
                    ui_callback(int(downloaded / total_size * 100))

        if expected_size and temp_path.stat().st_size != int(expected_size):
            raise RuntimeError("OTA installer size does not match the release asset.")
        actual = _sha256(temp_path)
        if actual != digest:
            raise RuntimeError("OTA installer SHA-256 verification failed; download was not executed.")

        temp_path.replace(setup_path)
        DETACHED_PROCESS = int(getattr(subprocess, "DETACHED_PROCESS", 0x00000008))
        CREATE_NO_WINDOW = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        subprocess.Popen(
            [str(setup_path), "--silent"],
            creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
        )
        raise SystemExit(0)
    except Exception as e:
        try:
            if 'temp_path' in locals():
                temp_path.unlink(missing_ok=True)
        except Exception:
            pass
        print(f"[OTA] Failed to download or apply update: {e}")
        return False
