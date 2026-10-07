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


def test_windows_compare_accepts_short_name_alias(tmp_path):
    import ctypes
    from core import windows_file_safety as winfs

    target = tmp_path / "short_alias.txt"
    target.write_text("short-name", encoding="utf-8")

    get_short = ctypes.windll.kernel32.GetShortPathNameW
    get_short.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    get_short.restype = ctypes.c_uint32
    buffer = ctypes.create_unicode_buffer(32768)
    length = get_short(str(tmp_path), buffer, len(buffer))
    if not length or buffer.value == str(tmp_path):
        pytest.skip("This Windows volume does not expose a distinct 8.3 path alias.")

    alias = Path(buffer.value) / target.name
    text, _ = winfs.read_text(alias, max_chars=100)
    assert text == "short-name"


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


def test_windows_exclusive_handle_blocks_write_and_delete_sharing(tmp_path):
    from core import windows_file_safety as winfs

    target = tmp_path / "exclusive.txt"
    target.write_text("protected", encoding="utf-8")

    fd, _final, _info = winfs.open_safe_file(target, write=True, exclusive=True)
    try:
        with pytest.raises(OSError):
            second_fd, _final2, _info2 = winfs.open_safe_file(target, write=True)
            os.close(second_fd)
        read_fd, _final3, _info3 = winfs.open_safe_file(target, write=False, exclusive=False)
        os.close(read_fd)
    finally:
        os.close(fd)

    assert target.read_text(encoding="utf-8") == "protected"


def test_windows_rename_refuses_reparse_parent(tmp_path):
    from core import windows_file_safety as winfs
    real = tmp_path / "real"
    real.mkdir()
    source = real / "source.txt"
    source.write_text("data", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Directory symlinks are unavailable on this Windows runner.")
    with pytest.raises(OSError, match=r"reparse[ -]point"):
        winfs.rename(source, link / "renamed.txt")
    assert source.exists()
