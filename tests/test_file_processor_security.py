import io
import os
import tarfile
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_archive_extraction_rejects_traversal_links_and_size_abuse():
    source = (ROOT / "actions" / "file_processor.py").read_text(encoding="utf-8")
    assert "def _safe_archive_target" in source
    assert "path-traversal member" in source
    assert "Archive symlink members are not allowed." in source
    assert "Archive link members are not allowed." in source
    assert "MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024" in source
    assert "def preflight(members)" in source
    assert "O_EXCL" in source
    assert "O_NOFOLLOW" in source
    assert "Archive would overwrite an existing path" in source


def test_video_transcription_uses_exclusive_temp_creation():
    source = (ROOT / "actions" / "file_processor.py").read_text(encoding="utf-8")
    assert "tempfile.mkstemp" in source
    assert "tempfile.mktemp" not in source


def test_archive_preflight_rejects_file_directory_path_conflicts():
    source = (ROOT / "actions" / "file_processor.py").read_text(encoding="utf-8")
    start = source.index("def preflight(members)")
    end = source.index("\n\n    try:\n", start)
    block = source[start:end]
    assert "seen_types: dict[Path, bool]" in block
    assert "file/directory path conflict" in block
    assert "return [(member, target, is_dir) for member, target, is_dir in planned]" in block

def test_ffmpeg_processing_checks_process_exit_codes_before_success():
    source = (ROOT / "actions" / "file_processor.py").read_text(encoding="utf-8")
    start = source.index("def _process_video")
    block = source[start:source.index("def _safe_archive_target", start)]
    assert "returncode == 0" in block
    assert block.count("if result.returncode != 0:") >= 6


def test_ffmpeg_operations_report_failure_instead_of_success():
    source = (ROOT / "actions" / "file_processor.py").read_text(encoding="utf-8")
    for phrase in (
        "Extract audio failed (ffmpeg exit",
        "Trim failed (ffmpeg exit",
        "Extract frame failed (ffmpeg exit",
        "Compress failed (ffmpeg exit",
        "Video transcription failed (ffmpeg exit",
        "Convert failed (ffmpeg exit",
    ):
        assert phrase in source

def test_file_processor_confines_input_paths_and_avoids_overwrite():
    source = (ROOT / "actions" / "file_processor.py").read_text(encoding="utf-8")
    assert "def _resolve_input_path" in source
    assert "File processing is limited to paths inside the user's home directory." in source
    assert "if not base.exists():" in source
    assert 'f"{src.stem}_{suffix}_{counter}{ext}"' in source


def test_file_processor_python_execution_requires_confirmation_and_checks_exit_code():
    source = (ROOT / "actions" / "file_processor.py").read_text(encoding="utf-8")
    assert "def _run_python_with_confirmation" in source
    assert "from core.confirm import request" in source
    assert 'key=f"file-execute:{path}"' in source
    assert "Python execution failed (exit" in source
    assert "Python execution timed out after 30 seconds." in source
    assert 'return _run_python_with_confirmation(path)' in source


def test_archive_extraction_destination_is_home_confined_and_symlink_free():
    source = (ROOT / "actions" / "file_processor.py").read_text(encoding="utf-8")
    assert "def _safe_archive_destination" in source
    assert "Archive extraction is limited to destinations inside the user's home directory." in source
    assert "Archive extraction destinations may not contain symlinked path components." in source
    assert "_safe_archive_destination(" in source


def test_file_processor_bounds_large_inputs_and_document_dimensions():
    source = (ROOT / "actions" / "file_processor.py").read_text(encoding="utf-8")
    assert "MAX_INPUT_BYTES = 128 * 1024 * 1024" in source
    assert "MAX_DOCUMENT_PAGES = 500" in source
    assert "MAX_PRESENTATION_SLIDES = 500" in source
    assert "MAX_IMAGE_DIMENSION = 12000" in source
    assert "Input file exceeds the" in source
    assert "PDF exceeds the {MAX_DOCUMENT_PAGES}-page processing limit." in source
    assert "Presentation exceeds the {MAX_PRESENTATION_SLIDES}-slide processing limit." in source
    assert "Image dimensions exceed the {MAX_IMAGE_DIMENSION}px safety limit." in source

def _home_tempdir():
    return tempfile.TemporaryDirectory(prefix="brahma_archive_test_", dir=str(Path.home()))

def test_archive_runtime_rejects_malicious_paths_duplicate_members_and_collisions():
    from actions.file_processor import _safe_extract_archive

    with _home_tempdir() as root:
        base = Path(root)
        archive_path = base / "malicious.zip"
        with zipfile.ZipFile(archive_path, "w") as zf:
            for name in ("../escape.txt", "/absolute.txt", "C:/drive-relative.txt", "C:drive-relative.txt", r"\\server\\share\\unc.txt"):
                zf.writestr(name, b"x")
        with pytest.raises(ValueError):
            _safe_extract_archive(archive_path, base / "out")
        assert not (base / "escape.txt").exists()

    with _home_tempdir() as root:
        base = Path(root)
        archive_path = base / "dupe.zip"
        with zipfile.ZipFile(archive_path, "w") as zf:
            zf.writestr("same.txt", b"one")
            zf.writestr("same.txt", b"two")
        with pytest.raises(ValueError):
            _safe_extract_archive(archive_path, base / "out")

    with _home_tempdir() as root:
        base = Path(root)
        out = base / "out"
        out.mkdir()
        (out / "existing.txt").write_text("original", encoding="utf-8")
        archive_path = base / "collision.zip"
        with zipfile.ZipFile(archive_path, "w") as zf:
            zf.writestr("existing.txt", b"replacement")
        with pytest.raises(ValueError):
            _safe_extract_archive(archive_path, out)
        assert (out / "existing.txt").read_text(encoding="utf-8") == "original"

def test_archive_runtime_rejects_zip_symlink_tar_links_and_special_files():
    from actions.file_processor import _safe_extract_archive

    with _home_tempdir() as root:
        base = Path(root)
        archive_path = base / "symlink.zip"
        info = zipfile.ZipInfo("evil.txt")
        info.external_attr = (0o120000 << 16)
        with zipfile.ZipFile(archive_path, "w") as zf:
            zf.writestr(info, b"target")
        with pytest.raises(ValueError):
            _safe_extract_archive(archive_path, base / "out")

    with _home_tempdir() as root:
        base = Path(root)
        archive_path = base / "links.tar"
        with tarfile.open(archive_path, "w") as tf:
            sym = tarfile.TarInfo("sym.txt")
            sym.type = tarfile.SYMTYPE
            sym.linkname = "/outside"
            tf.addfile(sym)
            hard = tarfile.TarInfo("hard.txt")
            hard.type = tarfile.LNKTYPE
            hard.linkname = "sym.txt"
            tf.addfile(hard)
        with pytest.raises(ValueError):
            _safe_extract_archive(archive_path, base / "out")

def test_archive_runtime_enforces_compression_ratio_and_rolls_back_partial_failure():
    from actions.file_processor import _safe_extract_archive

    with _home_tempdir() as root:
        base = Path(root)
        archive_path = base / "bomb.zip"
        payload = b"0" * (2 * 1024 * 1024)
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            zf.writestr("bomb.bin", payload)
        with pytest.raises(ValueError, match="compression ratio"):
            _safe_extract_archive(archive_path, base / "out")

    with _home_tempdir() as root:
        base = Path(root)
        archive_path = base / "partial.zip"
        with zipfile.ZipFile(archive_path, "w") as zf:
            zf.writestr("first.txt", b"one")
            zf.writestr("second.txt", b"two")
        out = base / "out"
        import actions.file_processor as fp
        calls = {"count": 0}
        original_open = fp.zipfile.ZipFile.open

        def open_with_second_failure(self, member, mode="r", pwd=None, force_zip64=False):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("simulated source failure")
            return original_open(self, member, mode=mode, pwd=pwd, force_zip64=force_zip64)

        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(fp.zipfile.ZipFile, "open", open_with_second_failure)
            with pytest.raises(OSError):
                _safe_extract_archive(archive_path, out)
        finally:
            monkeypatch.undo()
        assert not (out / "first.txt").exists()
        assert not (out / "second.txt").exists()


def test_archive_runtime_rejects_tar_gz_compression_bomb():
    from actions.file_processor import _safe_extract_archive

    with _home_tempdir() as root:
        base = Path(root)
        archive_path = base / "bomb.tar.gz"
        payload = b"0" * (8 * 1024 * 1024)
        with tarfile.open(archive_path, "w:gz", compresslevel=9) as tf:
            info = tarfile.TarInfo("bomb.bin")
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))

        with pytest.raises(ValueError, match="compression ratio"):
            _safe_extract_archive(archive_path, base / "out")


def test_archive_runtime_rejects_member_emitting_more_bytes_than_declared():
    from actions import file_processor as fp

    with _home_tempdir() as root:
        base = Path(root)
        archive_path = base / "malformed.zip"
        with zipfile.ZipFile(archive_path, "w") as zf:
            zf.writestr("small.txt", b"x")

        original_open = fp.zipfile.ZipFile.open

        class OversizedReader:
            def __init__(self):
                self.sent = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                if self.sent:
                    return b""
                self.sent = True
                return b"x" * (1024 * 1024 * 2)

        def forged_open(self, member, mode="r", pwd=None, force_zip64=False):
            if getattr(member, "filename", "") == "small.txt":
                return OversizedReader()
            return original_open(self, member, mode=mode, pwd=pwd, force_zip64=force_zip64)

        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(fp.zipfile.ZipFile, "open", forged_open)
            with pytest.raises(ValueError, match="declared"):
                fp._safe_extract_archive(archive_path, base / "out")
        finally:
            monkeypatch.undo()

        assert not (base / "out" / "small.txt").exists()


def test_generated_output_requires_nonempty_regular_file(tmp_path):
    from actions.file_processor import _verify_output_artifact

    output = tmp_path / "result.bin"
    with pytest.raises(RuntimeError, match="regular file"):
        _verify_output_artifact(output)
    output.write_bytes(b"ok")
    assert _verify_output_artifact(output) == output
    output.write_bytes(b"")
    with pytest.raises(RuntimeError, match="empty"):
        _verify_output_artifact(output)


def test_secure_text_output_rejects_race_created_symlink(tmp_path, monkeypatch):
    from actions import file_processor

    target = tmp_path / "result.txt"
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")

    real_open = file_processor.os.open
    swapped = {"done": False}

    def race_open(path_value, flags, mode=0o777, *, dir_fd=None):
        if (
            dir_fd is not None
            and path_value == target.name
            and flags & getattr(os, "O_EXCL", 0)
            and not swapped["done"]
        ):
            swapped["done"] = True
            try:
                target.symlink_to(outside)
            except OSError:
                pass
        return real_open(path_value, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(file_processor.os, "open", race_open)
    with pytest.raises(OSError):
        file_processor._secure_write_new_text(target, "attacker")

    assert outside.read_text(encoding="utf-8") == "keep"
    if target.exists():
        assert target.is_symlink()


def test_generated_artifacts_require_basic_format_integrity(tmp_path):
    from actions.file_processor import _verify_output_artifact

    valid_json = tmp_path / "valid.json"
    valid_json.write_text("{\"ok\": true}", encoding="utf-8")
    assert _verify_output_artifact(valid_json) == valid_json

    invalid_json = tmp_path / "invalid.json"
    invalid_json.write_text("{", encoding="utf-8")
    with pytest.raises(RuntimeError, match="format validation|Invalid"):
        _verify_output_artifact(invalid_json)
    assert not invalid_json.exists()

    valid_image = tmp_path / "valid.png"
    from PIL import Image
    Image.new("RGB", (4, 4), "white").save(valid_image)
    assert _verify_output_artifact(valid_image) == valid_image

    invalid_image = tmp_path / "invalid.png"
    invalid_image.write_bytes(b"not-an-image")
    with pytest.raises(Exception):
        _verify_output_artifact(invalid_image)
    assert not invalid_image.exists()


def test_video_failed_ffmpeg_output_is_removed(tmp_path, monkeypatch):
    from actions import file_processor

    source = tmp_path / "sample.mp4"
    source.write_bytes(b"placeholder")
    output = tmp_path / "sample_audio.mp3"

    monkeypatch.setattr(file_processor, "_ffmpeg_available", lambda: True, raising=False)

    def fake_run(*args, **kwargs):
        Path(args[0][-1]).write_bytes(b"partial")
        return __import__("subprocess").CompletedProcess(args[0], 1, "", "simulated ffmpeg failure")

    monkeypatch.setattr(file_processor.subprocess, "run", fake_run)
    result = file_processor._process_video(source, "extract_audio", {})
    assert result.startswith("Extract audio failed (ffmpeg exit")
    assert not output.exists()


def test_image_operations_report_success_only_after_valid_artifact(tmp_path):
    from actions.file_processor import _process_image
    from PIL import Image

    source = tmp_path / "sample.png"
    Image.new("RGB", (32, 24), "white").save(source)

    assert "Saved:" in _process_image(source, "resize", {"width": 16})
    resized = next(tmp_path.glob("sample_resized_16x12*.png"))
    with Image.open(resized) as image:
        image.verify()

    assert "Saved:" in _process_image(source, "convert", {"format": "jpg"})
    converted = next(tmp_path.glob("sample_converted*.jpg"))
    with Image.open(converted) as image:
        image.verify()

    assert "Saved:" in _process_image(source, "compress", {"quality": 70})
    compressed = next(tmp_path.glob("sample_compressed_q70*.jpg"))
    with Image.open(compressed) as image:
        image.verify()
