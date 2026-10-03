"""Prepare the pinned Windows OmniRoute runtime for Brahma Evo packaging.

This is intentionally the same release-build strategy used by the older JARVIS
build: portable Node, immutable OmniRoute package metadata/integrity checks,
native better-sqlite3 repair, and a final --version/native-load verification.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.request import urlopen
from zipfile import ZipFile

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.runtime_contract import NODE_VERSION, OMNIROUTE_COMMIT, OMNIROUTE_VERSION


NODE_ZIP_NAME = f"node-v{NODE_VERSION}-win-x64.zip"
NODE_URL = f"https://nodejs.org/dist/v{NODE_VERSION}/{NODE_ZIP_NAME}"
NODE_SHA256 = "158f7685b44de51f6c0df1d153526cbcd3e1bc739a8dfc607721cef75de9e541"
OMNIROUTE_METADATA_URL = f"https://registry.npmjs.org/omniroute/{OMNIROUTE_VERSION}"
CACHE_SCHEMA = "1"


def _download(url: str, path: Path) -> None:
    with urlopen(url, timeout=180) as response, path.open("wb") as output:
        shutil.copyfileobj(response, output)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _single_root(path: Path) -> Path:
    roots = [p for p in path.iterdir() if p.is_dir()]
    if len(roots) != 1:
        raise RuntimeError("Unexpected Node archive layout")
    return roots[0]


def _native_ok(node: Path, native: Path) -> bool:
    if not node.is_file() or not native.is_file():
        return False
    result = subprocess.run(
        [
            str(node),
            "-e",
            "process.dlopen({exports:{}}, process.argv[1]); console.log('native-ok')",
            str(native),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        env={**os.environ, "NODE_ENV": "production", "CI": "1"},
        check=False,
    )
    return result.returncode == 0 and "native-ok" in result.stdout


def _healthy(root: Path) -> bool:
    node = root / "node.exe"
    entry = root / "node_modules" / "omniroute" / "bin" / "omniroute.mjs"
    native_candidates = (
        root / "node_modules" / "omniroute" / "dist" / "node_modules" / "better-sqlite3" / "prebuilds" / "win32-x64.node",
        root / "node_modules" / "omniroute" / "dist" / "node_modules" / "better-sqlite3" / "build" / "Release" / "better_sqlite3.node",
    )
    native = next((p for p in native_candidates if p.is_file()), None)
    if native is None or not entry.is_file() or not _native_ok(node, native):
        return False
    version = subprocess.run(
        [str(node), str(entry), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        env={**os.environ, "NODE_ENV": "production"},
        check=False,
    )
    return version.returncode == 0 and version.stdout.strip().splitlines()[-1:] == [OMNIROUTE_VERSION]


def prepare(destination: Path) -> None:
    if sys.platform != "win32":
        raise RuntimeError("The embedded OmniRoute runtime currently targets Windows only.")
    destination = destination.resolve()
    manifest = destination / "runtime-manifest.txt"
    expected_manifest = (
        f"schema={CACHE_SCHEMA}\n"
        f"node={NODE_VERSION}\n"
        f"omniroute={OMNIROUTE_VERSION}\n"
        f"commit={OMNIROUTE_COMMIT}\n"
    )
    if manifest.is_file() and manifest.read_text(encoding="utf-8") == expected_manifest and _healthy(destination):
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="brahma-omniroute-") as temp_name:
        temp = Path(temp_name)

        node_archive = temp / NODE_ZIP_NAME
        _download(NODE_URL, node_archive)
        if _sha256(node_archive) != NODE_SHA256:
            raise RuntimeError("Node.js archive checksum verification failed.")
        node_extract = temp / "node"
        with ZipFile(node_archive) as archive:
            archive.extractall(node_extract)
        node_root = _single_root(node_extract)
        bundled_node = node_root / "node.exe"
        npm = node_root / "npm.cmd"
        if not bundled_node.is_file() or not npm.is_file():
            raise RuntimeError("Bundled Node runtime is incomplete.")

        metadata = temp / "omniroute-metadata.json"
        _download(OMNIROUTE_METADATA_URL, metadata)
        registry = json.loads(metadata.read_text(encoding="utf-8"))
        if str(registry.get("version") or "") != OMNIROUTE_VERSION:
            raise RuntimeError("OmniRoute registry version did not match the pinned release.")
        git_head = str(registry.get("gitHead") or "")
        if OMNIROUTE_COMMIT and git_head and git_head != OMNIROUTE_COMMIT:
            raise RuntimeError(f"OmniRoute gitHead mismatch: {git_head} != {OMNIROUTE_COMMIT}")
        dist = registry.get("dist") or {}
        tarball_url = str(dist.get("tarball") or "")
        integrity = str(dist.get("integrity") or "")
        if not tarball_url or not integrity.startswith("sha512-"):
            raise RuntimeError("OmniRoute metadata did not provide an integrity-verifiable tarball.")

        tarball = temp / f"omniroute-{OMNIROUTE_VERSION}.tgz"
        _download(tarball_url, tarball)
        expected_sha512 = base64.b64decode(integrity.removeprefix("sha512-"))
        if hashlib.sha512(tarball.read_bytes()).digest() != expected_sha512:
            raise RuntimeError("OmniRoute tarball integrity verification failed.")

        staging = temp / "runtime"
        staging.mkdir()
        env = {**os.environ, "NODE_ENV": "production", "CI": "1", "OMNIROUTE_SKIP_POSTINSTALL": "1"}
        result = subprocess.run(
            [
                str(npm),
                "install",
                "--prefix", str(staging),
                "--no-fund",
                "--no-audit",
                "--omit=dev",
                "--install-strategy=hoisted",
                "--package-lock=false",
                "--ignore-scripts=false",
                "--legacy-peer-deps",
                "--no-bin-links",
                "--no-progress",
                str(tarball),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
            env=env,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("OmniRoute production dependency installation failed.")

        shutil.copy2(bundled_node, staging / "node.exe")
        package_root = staging / "node_modules" / "omniroute"
        if not (package_root / "bin" / "omniroute.mjs").is_file():
            raise RuntimeError("OmniRoute CLI entrypoint is missing after install.")
        if not (package_root / "dist" / "server.js").is_file():
            raise RuntimeError("OmniRoute server bundle is missing after install.")

        source_candidates = (
            staging / "node_modules" / "better-sqlite3" / "prebuilds" / "win32-x64.node",
            staging / "node_modules" / "better-sqlite3" / "build" / "Release" / "better_sqlite3.node",
            package_root / "node_modules" / "better-sqlite3" / "prebuilds" / "win32-x64.node",
            package_root / "node_modules" / "better-sqlite3" / "build" / "Release" / "better_sqlite3.node",
        )
        target_candidates = (
            package_root / "dist" / "node_modules" / "better-sqlite3" / "prebuilds" / "win32-x64.node",
            package_root / "dist" / "node_modules" / "better-sqlite3" / "build" / "Release" / "better_sqlite3.node",
        )
        source = next((p for p in source_candidates if p.is_file()), None)
        target = next((p for p in target_candidates if p.suffix == ".node"), None)
        if source is None:
            raise RuntimeError("Prepared OmniRoute runtime is missing a Windows better-sqlite3 native binary.")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if not _healthy(staging):
            raise RuntimeError("Prepared OmniRoute runtime failed native/version verification.")

        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(staging, destination)
        (destination / "runtime-manifest.txt").write_text(expected_manifest, encoding="utf-8")


def main() -> int:
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("build_vendor/omniroute_runtime")
    prepare(destination)
    print(f"Prepared OmniRoute {OMNIROUTE_VERSION} at {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
