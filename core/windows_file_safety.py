"""Windows handle-backed filesystem safety primitives."""
from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

if sys.platform != "win32":  # pragma: no cover
    raise ImportError("windows_file_safety is Windows-only")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
DELETE = 0x00010000
FILE_READ_ATTRIBUTES = 0x00000080
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
CREATE_NEW = 1
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_WRITE_THROUGH = 0x80000000

FileAttributeTagInfo = 9
FileDispositionInfo = 4
FileRenameInfo = 3


class FILE_ATTRIBUTE_TAG_INFO_STRUCT(ctypes.Structure):
    _fields_ = [("FileAttributes", ctypes.c_ulong), ("ReparseTag", ctypes.c_ulong)]


class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_ulong),
        ("ftCreationTimeLow", ctypes.c_ulong),
        ("ftCreationTimeHigh", ctypes.c_ulong),
        ("ftLastAccessTimeLow", ctypes.c_ulong),
        ("ftLastAccessTimeHigh", ctypes.c_ulong),
        ("ftLastWriteTimeLow", ctypes.c_ulong),
        ("ftLastWriteTimeHigh", ctypes.c_ulong),
        ("dwVolumeSerialNumber", ctypes.c_ulong),
        ("nFileSizeHigh", ctypes.c_ulong),
        ("nFileSizeLow", ctypes.c_ulong),
        ("nNumberOfLinks", ctypes.c_ulong),
        ("nFileIndexHigh", ctypes.c_ulong),
        ("nFileIndexLow", ctypes.c_ulong),
    ]


class FILE_DISPOSITION_INFO_STRUCT(ctypes.Structure):
    _fields_ = [("DeleteFile", ctypes.c_byte)]


kernel32.CreateFileW.argtypes = [
    ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
    ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
]
kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.CloseHandle.restype = ctypes.c_int
kernel32.GetFinalPathNameByHandleW.argtypes = [
    ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong,
]
kernel32.GetFinalPathNameByHandleW.restype = ctypes.c_ulong
kernel32.GetFileInformationByHandle.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(BY_HANDLE_FILE_INFORMATION),
]
kernel32.GetFileInformationByHandle.restype = ctypes.c_int
kernel32.GetFileInformationByHandleEx.argtypes = [
    ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong,
]
kernel32.GetFileInformationByHandleEx.restype = ctypes.c_int
kernel32.SetFileInformationByHandle.argtypes = [
    ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong,
]
kernel32.SetFileInformationByHandle.restype = ctypes.c_int


def _win_path(path: Path | str) -> str:
    return str(Path(path).expanduser().absolute())


def _strip_device_prefix(path: str) -> str:
    if path.startswith(r"\\?\UNC\\"):
        return r"\\" + path[8:]
    if path.startswith(r"\\?\"):
        return path[4:]
    return path


def _final_path(handle: int) -> str:
    size = 512
    while size <= 32768:
        buffer = ctypes.create_unicode_buffer(size)
        result = kernel32.GetFinalPathNameByHandleW(
            ctypes.c_void_p(handle), buffer, size, 0,
        )
        if result == 0:
            raise OSError(ctypes.get_last_error(), "GetFinalPathNameByHandleW failed")
        if result < size - 1:
            return _strip_device_prefix(buffer.value)
        size *= 2
    raise OSError("Final handle path exceeds supported length")


def _is_under_home(final_path: str) -> bool:
    try:
        Path(final_path).absolute().relative_to(Path.home().absolute())
        return True
    except (OSError, ValueError):
        return False


def _handle_info(handle: int) -> BY_HANDLE_FILE_INFORMATION:
    info = BY_HANDLE_FILE_INFORMATION()
    if not kernel32.GetFileInformationByHandle(ctypes.c_void_p(handle), ctypes.byref(info)):
        raise OSError(ctypes.get_last_error(), "GetFileInformationByHandle failed")
    return info


def _reject_reparse(handle: int, info: BY_HANDLE_FILE_INFORMATION) -> None:
    attrs = FILE_ATTRIBUTE_TAG_INFO_STRUCT()
    if kernel32.GetFileInformationByHandleEx(
        ctypes.c_void_p(handle),
        FileAttributeTagInfo,
        ctypes.byref(attrs),
        ctypes.sizeof(attrs),
    ):
        if attrs.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError("Refusing to operate on a Windows reparse point")
    elif info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
        raise OSError("Refusing to operate on a Windows reparse point")


def _identity(info: BY_HANDLE_FILE_INFORMATION) -> tuple[int, int, int]:
    return (
        int(info.dwVolumeSerialNumber),
        int(info.nFileIndexHigh),
        int(info.nFileIndexLow),
    )


def open_safe_file(
    path: Path | str,
    *,
    write: bool = False,
    append: bool = False,
    create_new: bool = False,
) -> tuple[int, str, BY_HANDLE_FILE_INFORMATION]:
    """Open a regular file, reject reparse points, and prove its final path."""
    disposition = CREATE_NEW if create_new else OPEN_EXISTING
    desired = GENERIC_READ | (GENERIC_WRITE if write else 0)
    flags = FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT
    if write:
        flags |= FILE_FLAG_WRITE_THROUGH

    handle = kernel32.CreateFileW(
        _win_path(path),
        desired,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        disposition,
        flags,
        None,
    )
    if handle in (None, INVALID_HANDLE_VALUE):
        raise OSError(ctypes.get_last_error(), f"CreateFileW failed for {path}")

    try:
        final = _final_path(handle)
        if not _is_under_home(final):
            raise OSError("Refusing a file whose final handle path is outside the user's home directory")
        info = _handle_info(handle)
        _reject_reparse(handle, info)
        if info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY:
            raise OSError("Refusing to operate on a directory as a regular file")

        import msvcrt
        fd = msvcrt.open_osfhandle(int(handle), os.O_BINARY | (os.O_RDWR if write else os.O_RDONLY))
        handle = None  # ownership transferred to fd
        if append:
            os.lseek(fd, 0, os.SEEK_END)
        return fd, final, info
    except Exception:
        if handle not in (None, INVALID_HANDLE_VALUE):
            kernel32.CloseHandle(ctypes.c_void_p(handle))
        raise


def read_text(path: Path | str, *, max_chars: int, expected_identity=None) -> tuple[str, int]:
    fd, _final, info = open_safe_file(path, write=False)
    try:
        if expected_identity is not None and len(tuple(expected_identity)) == 3:
            if tuple(expected_identity) != _identity(info):
                raise OSError("Target changed identity before secure Windows read")
        with os.fdopen(fd, "r", encoding="utf-8", errors="ignore", closefd=True) as handle:
            fd = -1
            content = handle.read(max_chars + 1)
        return content, (int(info.nFileSizeHigh) << 32) | int(info.nFileSizeLow)
    finally:
        if fd >= 0:
            os.close(fd)


def write_text(
    path: Path | str,
    content: str,
    *,
    append: bool = False,
    expected_identity=None,
) -> tuple[int, int, int]:
    fd, _final, info = open_safe_file(
        path,
        write=True,
        append=append,
        create_new=expected_identity is None,
    )
    try:
        if expected_identity is not None and len(tuple(expected_identity)) == 3:
            if tuple(expected_identity) != _identity(info):
                raise OSError("Target changed identity before secure Windows write")
        if not append:
            os.ftruncate(fd, 0)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        return _identity(info)
    finally:
        if fd >= 0:
            os.close(fd)


def unlink(path: Path | str, *, expected_identity=None) -> None:
    handle = kernel32.CreateFileW(
        _win_path(path),
        DELETE | FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle in (None, INVALID_HANDLE_VALUE):
        raise OSError(ctypes.get_last_error(), f"CreateFileW failed for {path}")
    try:
        final = _final_path(handle)
        if not _is_under_home(final):
            raise OSError("Refusing to delete a file outside the user's home directory")
        info = _handle_info(handle)
        _reject_reparse(handle, info)
        if expected_identity is not None and len(tuple(expected_identity)) == 3:
            if tuple(expected_identity) != _identity(info):
                raise OSError("Target changed identity before secure Windows delete")
        disposition = FILE_DISPOSITION_INFO_STRUCT(1)
        if not kernel32.SetFileInformationByHandle(
            ctypes.c_void_p(handle),
            FileDispositionInfo,
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        ):
            raise OSError(ctypes.get_last_error(), "SetFileInformationByHandle(delete) failed")
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def rename(source: Path | str, destination: Path | str, *, source_identity=None) -> None:
    source_handle = kernel32.CreateFileW(
        _win_path(source),
        DELETE | FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if source_handle in (None, INVALID_HANDLE_VALUE):
        raise OSError(ctypes.get_last_error(), f"CreateFileW failed for {source}")

    destination_path = Path(destination)
    parent_handle = kernel32.CreateFileW(
        _win_path(destination_path.parent),
        FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if parent_handle in (None, INVALID_HANDLE_VALUE):
        kernel32.CloseHandle(ctypes.c_void_p(source_handle))
        raise OSError(ctypes.get_last_error(), f"CreateFileW failed for destination parent {destination_path.parent}")

    try:
        final_source = _final_path(source_handle)
        final_parent = _final_path(parent_handle)
        if not _is_under_home(final_source) or not _is_under_home(final_parent):
            raise OSError("Refusing a rename outside the user's home directory")

        source_info = _handle_info(source_handle)
        parent_info = _handle_info(parent_handle)
        _reject_reparse(source_handle, source_info)
        _reject_reparse(parent_handle, parent_info)
        if source_identity is not None and len(tuple(source_identity)) == 3:
            if tuple(source_identity) != _identity(source_info):
                raise OSError("Source changed identity before secure Windows rename")

        leaf = destination_path.name
        if not leaf or leaf in {".", ".."} or "\x00" in leaf or "/" in leaf or "\\" in leaf:
            raise ValueError("Invalid destination filename")

        name = leaf.encode("utf-16-le")
        pointer_size = ctypes.sizeof(ctypes.c_void_p)
        root_offset = pointer_size
        length_offset = root_offset + pointer_size
        name_offset = length_offset + ctypes.sizeof(ctypes.c_ulong)
        size = name_offset + len(name)
        raw = ctypes.create_string_buffer(size)
        ctypes.c_ubyte.from_buffer(raw, 0).value = 0
        ctypes.c_void_p.from_buffer(raw, root_offset).value = parent_handle
        ctypes.c_ulong.from_buffer(raw, length_offset).value = len(name)
        raw[name_offset:name_offset + len(name)] = name

        if not kernel32.SetFileInformationByHandle(
            ctypes.c_void_p(source_handle),
            FileRenameInfo,
            ctypes.byref(raw),
            size,
        ):
            raise OSError(ctypes.get_last_error(), "SetFileInformationByHandle(rename) failed")
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(parent_handle))
        kernel32.CloseHandle(ctypes.c_void_p(source_handle))
