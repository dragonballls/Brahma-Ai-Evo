import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows-only handle safety tests")


def test_windows_handle_file_roundtrip_and_rename(tmp_path):
    from core import windows_file_safety as winfs

    source = tmp_path / "source.txt"
    renamed = tmp_path / "renamed.txt"

    winfs.write_text(source, "alpha")
    text, size = winfs.read_text(source, max_chars=100)
    assert text == "alpha"
    assert size == len("alpha")
    first_fd, _final, first_info = winfs.open_safe_file(source, write=True)
    os.close(first_fd)
    identity = (
        int(first_info.dwVolumeSerialNumber),
        int(first_info.nFileIndexHigh),
        int(first_info.nFileIndexLow),
    )
    winfs.write_text(source, "beta", expected_identity=identity)
    winfs.rename(source, renamed)

    text, _ = winfs.read_text(renamed, max_chars=100)
    assert text == "beta"
    assert not source.exists()
    assert renamed.exists()


def test_windows_handle_file_refuses_reparse_leaf(tmp_path):
    from core import windows_file_safety as winfs

    target = tmp_path / "target.txt"
    target.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is unavailable on this Windows runner.")

    with pytest.raises(OSError, match="reparse point"):
        winfs.read_text(link, max_chars=100)


def test_windows_handle_file_rejects_identity_mismatch(tmp_path):
    from core import windows_file_safety as winfs

    target = tmp_path / "target.txt"
    target.write_text("original", encoding="utf-8")

    fd, _final, info = winfs.open_safe_file(target, write=True)
    os.close(fd)
    actual = (
        int(info.dwVolumeSerialNumber),
        int(info.nFileIndexHigh),
        int(info.nFileIndexLow),
    )
    wrong = (actual[0], actual[1], actual[2] + 1)

    with pytest.raises(OSError, match="identity"):
        winfs.write_text(target, "blocked", expected_identity=wrong)
    assert target.read_text(encoding="utf-8") == "original"
