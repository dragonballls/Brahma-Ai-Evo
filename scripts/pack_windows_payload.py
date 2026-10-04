#!/usr/bin/env python3
"""Build and verify the single-file Windows application payload used by the setup wizard."""
from __future__ import annotations
import argparse
import hashlib
from pathlib import Path
import zipfile

REQUIRED_FILES = ("BrahmaEvo.exe", "BrahmaEvoSupervisor.exe")

def _validate_source(source: Path) -> list[Path]:
    if not source.is_dir():
        raise FileNotFoundError(f"Payload directory not found: {source}")
    for required in REQUIRED_FILES:
        if not (source / required).is_file():
            raise FileNotFoundError(f"Required payload file missing: {source / required}")
    return sorted(p for p in source.rglob("*") if p.is_file())

def build(source: Path, output: Path) -> dict[str, object]:
    files = _validate_source(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for file_path in files:
            archive.write(file_path, file_path.relative_to(source).as_posix())
    return verify(source, output)

def verify(source: Path, archive_path: Path) -> dict[str, object]:
    files = _validate_source(source)
    expected = {p.relative_to(source).as_posix(): p.stat().st_size for p in files}
    with zipfile.ZipFile(archive_path, "r") as archive:
        actual = {info.filename: info.file_size for info in archive.infolist() if not info.is_dir()}
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        size_mismatches = sorted(name for name in expected if name in actual and actual[name] != expected[name])
        if missing or unexpected or size_mismatches:
            raise RuntimeError(
                "Payload archive mismatch: "
                f"missing={missing[:5]}, unexpected={unexpected[:5]}, size_mismatches={size_mismatches[:5]}"
            )
        required = {name: actual[name] for name in REQUIRED_FILES}
        digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
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
    print(f"Payload {'verified' if args.verify else 'built'}: {result['files']} files, {result['bytes']} bytes, sha256={result['sha256']}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
