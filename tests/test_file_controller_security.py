import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_rename_destination_is_confined_to_safe_root():
    source = (ROOT / "actions" / "file_controller.py").read_text(encoding="utf-8")
    start = source.index("def rename_file")
    end = source.index("def read_file", start)
    block = source[start:end]
    assert "new_path = target.parent / new_name" in block
    assert 'if not _is_safe_path(new_path):' in block
    assert "Access denied (destination)" in block


def test_file_controller_rejects_symlink_components_before_file_mutation():
    source = (ROOT / "actions" / "file_controller.py").read_text(encoding="utf-8")
    assert "def _has_symlink_component" in source
    assert "if _has_symlink_component(target):" in source
    assert "return True" in source[source.index("def _has_symlink_component"):source.index("def _is_safe_path")]


def test_file_controller_undo_checks_post_action_identity_before_mutation():
    source = (ROOT / "actions" / "file_controller.py").read_text(encoding="utf-8")
    assert "def _fingerprint" in source
    assert "if _fingerprint(dst) != expected:" in source
    assert "if _fingerprint(target) != expected_after:" in source
    assert "because another file now occupies the original path" in source


def test_create_file_refuses_unreadable_existing_targets_instead_of_registering_delete_undo():
    source = (ROOT / "actions" / "file_controller.py").read_text(encoding="utf-8")
    start = source.index("def create_file")
    end = source.index("def create_folder", start)
    block = source[start:end]
    assert "existing target" in block
    assert "could not be read safely" in block
    assert "previous = target.read_text(encoding=\"utf-8\")" in block


def test_write_file_refuses_unreadable_existing_targets(monkeypatch, tmp_path):
    from actions import file_controller

    target = tmp_path / "existing.txt"
    target.write_text("original", encoding="utf-8")
    monkeypatch.setattr(file_controller, "_SAFE_ROOTS", [tmp_path])

    original_read_text = Path.read_text

    def fail_target_read(self, *args, **kwargs):
        if self == target:
            raise OSError("simulated unreadable target")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_target_read)
    result = file_controller.write_file(str(tmp_path), "existing.txt", "replacement")
    assert "refusing to overwrite it" in result
    Path.read_text = original_read_text
    assert target.read_text(encoding="utf-8") == "original"


def test_write_file_refuses_oversized_existing_targets_for_replacement(tmp_path, monkeypatch):
    from actions import file_controller

    target = tmp_path / "large.txt"
    target.write_bytes(b"x" * (file_controller._UNDO_CONTENT_LIMIT + 1))
    monkeypatch.setattr(file_controller, "_SAFE_ROOTS", [tmp_path])

    result = file_controller.write_file(str(tmp_path), "large.txt", "replacement")
    assert "too large to overwrite safely" in result
    assert target.read_bytes().startswith(b"x")


def test_move_and_copy_refuse_existing_destinations(tmp_path, monkeypatch):
    from actions import file_controller

    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("source", encoding="utf-8")
    destination.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(file_controller, "_SAFE_ROOTS", [tmp_path])

    move_result = file_controller.move_file(str(tmp_path), "source.txt", str(destination))
    assert "Refusing to overwrite it" in move_result
    assert source.exists()
    assert destination.read_text(encoding="utf-8") == "keep"

    copy_result = file_controller.copy_file(str(tmp_path), "source.txt", str(destination))
    assert "Refusing to overwrite it" in copy_result
    assert source.exists()


def test_read_file_does_not_load_entire_file_before_truncating(tmp_path, monkeypatch):
    from actions import file_controller

    target = tmp_path / "large.txt"
    target.write_text("A" * 100_000, encoding="utf-8")
    monkeypatch.setattr(file_controller, "_SAFE_ROOTS", [tmp_path])

    class GuardedReader:
        def __init__(self, raw):
            self.raw = raw
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            self.raw.close()
        def read(self, size=-1):
            assert size <= 101
            return self.raw.read(size)

    original_open = Path.open

    def guarded_open(self, *args, **kwargs):
        raw = original_open(self, *args, **kwargs)
        if self == target:
            return GuardedReader(raw)
        return raw

    monkeypatch.setattr(Path, "open", guarded_open)
    result = file_controller.read_file(str(tmp_path), "large.txt", max_chars=100)
    assert "[Truncated" in result
    assert len(result.split("\n\n[Truncated")[0]) == 100


def test_file_processor_output_path_skips_existing_symlink(tmp_path):
    from actions import file_processor

    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    link = tmp_path / "video_compressed.mp4"

    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        return

    output = file_processor._output_path(source, "compressed")
    assert output != link
    assert not output.exists()


def test_file_processor_output_path_advances_past_broken_symlink(tmp_path):
    from actions import file_processor

    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")
    broken = tmp_path / "video_compressed_1.mp4"

    try:
        broken.symlink_to(tmp_path / "missing-target.mp4")
    except (OSError, NotImplementedError):
        return

    output = file_processor._output_path(source, "compressed")
    assert output == tmp_path / "video_compressed.mp4" or output == tmp_path / "video_compressed_2.mp4"
    assert output != broken


def test_list_files_does_not_build_unbounded_result_list(tmp_path, monkeypatch):
    from actions import file_controller

    for i in range(700):
        (tmp_path / f"file-{i:04d}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(file_controller, "_SAFE_ROOTS", [tmp_path])

    result = file_controller.list_files(str(tmp_path))
    assert "showing first 500 of 700 items" in result
    assert result.count("📄") <= 500


def test_write_file_rejects_symlink_replacement_before_secure_open(tmp_path, monkeypatch):
    from actions import file_controller

    target = tmp_path / "victim.txt"
    outside = tmp_path / "outside.txt"
    backup = tmp_path / "original.txt"
    target.write_text("original", encoding="utf-8")
    outside.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(file_controller, "_SAFE_ROOTS", [tmp_path])
    real_open = file_controller.os.open
    swapped = {"done": False}

    def race_open(path_value, flags, mode=0o777, *, dir_fd=None):
        if (
            dir_fd is not None
            and path_value == target.name
            and not swapped["done"]
            and flags & getattr(os, "O_NOFOLLOW", 0)
        ):
            swapped["done"] = True
            target.replace(backup)
            target.symlink_to(outside)
        return real_open(path_value, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(file_controller.os, "open", race_open)
    result = file_controller.write_file(str(tmp_path), target.name, "malicious")
    assert "could not write file" in result.lower()
    assert outside.read_text(encoding="utf-8") == "keep"


def test_write_file_rejects_hardlink_replacement_before_secure_write(tmp_path, monkeypatch):
    from actions import file_controller

    if os.name == "nt":
        pytest.skip("Descriptor-relative hard-link race test targets POSIX openat semantics.")

    target = tmp_path / "victim.txt"
    outside = tmp_path / "outside.txt"
    backup = tmp_path / "original.txt"
    target.write_text("original", encoding="utf-8")
    outside.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(file_controller, "_SAFE_ROOTS", [tmp_path])
    real_open = file_controller.os.open
    swapped = {"done": False}

    def race_open(path_value, flags, mode=0o777, *, dir_fd=None):
        if (
            dir_fd is not None
            and path_value == target.name
            and not swapped["done"]
            and flags & getattr(os, "O_NOFOLLOW", 0)
        ):
            swapped["done"] = True
            target.replace(backup)
            os.link(outside, target)
        return real_open(path_value, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(file_controller.os, "open", race_open)
    result = file_controller.write_file(str(tmp_path), target.name, "malicious")
    assert "could not write file" in result.lower()
    assert outside.read_text(encoding="utf-8") == "keep"


def test_read_file_rejects_symlink_replacement_before_secure_open(tmp_path, monkeypatch):
    from actions import file_controller

    target = tmp_path / "victim.txt"
    outside = tmp_path / "outside.txt"
    backup = tmp_path / "original.txt"
    target.write_text("safe", encoding="utf-8")
    outside.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(file_controller, "_SAFE_ROOTS", [tmp_path])
    real_open = file_controller.os.open
    swapped = {"done": False}

    def race_open(path_value, flags, mode=0o777, *, dir_fd=None):
        if (
            dir_fd is not None
            and path_value == target.name
            and flags & getattr(os, "O_NOFOLLOW", 0)
            and not swapped["done"]
        ):
            swapped["done"] = True
            target.replace(backup)
            target.symlink_to(outside)
        return real_open(path_value, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(file_controller.os, "open", race_open)
    result = file_controller.read_file(str(tmp_path), target.name)
    assert "could not read file" in result.lower()
    assert "secret" not in result


@pytest.mark.skipif(os.name != "nt", reason="Windows handle integration")
def test_undo_move_passes_exact_handle_identity_to_windows_rename(tmp_path, monkeypatch):
    from actions import file_controller

    dst = tmp_path / "moved.txt"
    src = tmp_path / "original.txt"
    dst.write_text("moved", encoding="utf-8")

    captured = {}

    real_winfs = file_controller._WINFS
    real_rename = real_winfs.rename

    def fake_rename(source, destination, *, source_identity=None):
        captured["source"] = source
        captured["destination"] = destination
        captured["source_identity"] = source_identity

    monkeypatch.setattr(real_winfs, "rename", fake_rename)
    undo = file_controller._undo_move(src, dst)
    assert undo().startswith("'original.txt' is back")
    assert captured["source_identity"] is not None
    assert len(tuple(captured["source_identity"])) == 3
    monkeypatch.setattr(real_winfs, "rename", real_rename)


@pytest.mark.skipif(os.name != "nt", reason="Windows handle integration")
def test_undo_write_passes_exact_handle_identity_to_windows_writer(tmp_path, monkeypatch):
    from actions import file_controller

    target = tmp_path / "target.txt"
    target.write_text("after", encoding="utf-8")
    expected = file_controller._fingerprint(target)
    captured = {}

    real_winfs = file_controller._WINFS
    real_write = real_winfs.write_text

    def fake_write(path, content, *, append=False, expected_identity=None):
        captured["path"] = path
        captured["content"] = content
        captured["expected_identity"] = expected_identity
        return tuple(expected_identity)

    monkeypatch.setattr(real_winfs, "write_text", fake_write)
    undo = file_controller._undo_write(target, "before", expected)
    assert undo() == "Restored the previous contents of 'target.txt'."
    assert captured["content"] == "before"
    assert len(tuple(captured["expected_identity"])) == 3
    monkeypatch.setattr(real_winfs, "write_text", real_write)


@pytest.mark.skipif(os.name != "nt", reason="Windows handle integration")
def test_secure_unlink_uses_handle_backed_delete(tmp_path, monkeypatch):
    from actions import file_controller

    target = tmp_path / "target.txt"
    target.write_text("data", encoding="utf-8")
    identity = file_controller._expected_identity(target)

    captured = {}
    real_winfs = file_controller._WINFS
    real_unlink = real_winfs.unlink

    def fake_unlink(path, *, expected_identity=None):
        captured["path"] = path
        captured["expected_identity"] = expected_identity

    monkeypatch.setattr(real_winfs, "unlink", fake_unlink)
    file_controller._secure_unlink(target, expected_identity=identity)
    assert captured["path"] == target
    assert captured["expected_identity"] == identity
    monkeypatch.setattr(real_winfs, "unlink", real_unlink)


@pytest.mark.skipif(os.name != "nt", reason="Windows safe-write dispatch")
def test_secure_write_text_dispatches_to_windows_handle_writer(tmp_path, monkeypatch):
    from actions import file_controller

    calls = {}

    class FakeWinFS:
        def write_text(self, path, content, *, append=False, expected_identity=None):
            calls["path"] = path
            calls["content"] = content
            calls["append"] = append
            calls["expected_identity"] = expected_identity

        def unlink(self, *args, **kwargs):
            raise AssertionError("secure write must not dispatch to Windows unlink")

    monkeypatch.setattr(file_controller, "_WINFS", FakeWinFS())
    file_controller._secure_write_text(
        tmp_path / "target.txt",
        "replacement",
        append=True,
        expected_identity=(1, 2, 3),
    )
    assert calls == {
        "path": tmp_path / "target.txt",
        "content": "replacement",
        "append": True,
        "expected_identity": (1, 2, 3),
    }
