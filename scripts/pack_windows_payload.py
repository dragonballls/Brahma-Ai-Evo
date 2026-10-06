#!/usr/bin/env python3
"""Build and verify the single-file Windows application payload used by the setup wizard."""
from __future__ import annotations

import argparse
import hashlib
import os
from collections import Counter
from pathlib import Path, PurePosixPath
import stat
import tempfile
import zipfile

REQUIRED_FILES = ("BrahmaEvo.exe", "BrahmaEvoSupervisor.exe")


def _validate_source(source: Path) -> list[Path]:
    source = source.resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"Payload directory not found: {source}")
    for required in REQUIRED_FILES:
        required_path = source / required
        if not required_path.is_file() or required_path.is_symlink():
            raise FileNotFoundError(f"Required payload file missing or unsafe: {required_path}")

    files: list[Path] = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Payload source may not contain symlinks: {path}")
        if path.is_file():
            files.append(path)
        elif path.exists() and not path.is_dir():
            raise ValueError(f"Payload source contains an unsupported special file: {path}")
    return files


def _validate_archive_entries(archive: zipfile.ZipFile) -> None:
    infos = archive.infolist()
    names = [str(info.filename or "") for info in infos]
    duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
    if duplicates:
        raise RuntimeError(f"Payload archive contains duplicate entries: {duplicates[:5]}")

    for info in infos:
        name = str(info.filename or "")
        if not name or "\\" in name:
            raise RuntimeError(f"Payload archive contains an unsafe entry name: {name!r}")

        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or (path.parts and path.parts[0].endswith(":")):
            raise RuntimeError(f"Payload archive contains a path-traversal entry: {name!r}")

        mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(mode)
        if stat.S_ISLNK(mode) or (file_type and file_type != stat.S_IFREG):
            raise RuntimeError(f"Payload archive contains a non-regular entry: {name!r}")


def build(source: Path, output: Path) -> dict[str, object]:
    files = _validate_source(source)
    output.parent.mkdir(parents=True, exist_ok=True)

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{output.name}.build-",
        suffix=".zip",
        dir=str(output.parent),
    )
    os.close(fd)
    temp_output = Path(temp_name)
    try:
        with zipfile.ZipFile(
            temp_output,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=1,
            allowZip64=True,
        ) as archive:
            for file_path in files:
                if file_path.is_symlink():
                    raise ValueError(f"Payload source changed to a symlink during packaging: {file_path}")
                archive.write(file_path, file_path.relative_to(source).as_posix())

        result = verify(source, temp_output)
        temp_output.replace(output)
        result["archive"] = str(output)
        return result
    finally:
        temp_output.unlink(missing_ok=True)


def verify(source: Path, archive_path: Path) -> dict[str, object]:
    files = _validate_source(source)
    expected = {p.relative_to(source).as_posix(): p.stat().st_size for p in files}
    with zipfile.ZipFile(archive_path, "r") as archive:
        _validate_archive_entries(archive)
        actual = {
            info.filename: info.file_size
            for info in archive.infolist()
            if not info.is_dir()
        }
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        size_mismatches = sorted(
            name for name in expected
            if name in actual and actual[name] != expected[name]
        )
        if missing or unexpected or size_mismatches:
            raise RuntimeError(
                "Payload archive mismatch: "
                f"missing={missing[:5]}, unexpected={unexpected[:5]}, "
                f"size_mismatches={size_mismatches[:5]}"
            )

        required = {name: actual[name] for name in REQUIRED_FILES}
        digest = hashlib.sha256()
        with archive_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
        digest = digest.hexdigest()

    return {
        "archive": str(archive_path),
        "files": len(expected),
        "bytes": archive_path.stat().st_size,
        "required": required,
        "sha256": digest,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    result = verify(args.source, args.output) if args.verify else build(args.source, args.output)
    print(
        f"Payload {'verified' if args.verify else 'built'}: "
        f"{result['files']} files, {result['bytes']} bytes, sha256={result['sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
