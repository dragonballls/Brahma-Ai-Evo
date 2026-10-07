import os
import shutil
import platform
from pathlib import Path
import stat as _stat
import hashlib
from datetime import datetime

try:
    import send2trash
    _SEND2TRASH = True
except ImportError:
    _SEND2TRASH = False

from core.undo import push_undo

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"

# Undo keeps a file's previous contents in memory so `write` can be reversed.
# Above this size it does not — a 200 MB log would sit in RAM for the rest of
# the session to protect an edit nobody is going to take back.
_UNDO_CONTENT_LIMIT = 1_000_000


def _fingerprint(path: Path):
    """Capture enough identity to ensure an undo only touches the object Brahma created."""
    try:
        path = Path(path)
        if path.is_symlink():
            return None
        stat = path.stat()
        kind = "dir" if path.is_dir() else "file"
        digest = None
        if kind == "file":
            digestor = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digestor.update(chunk)
            digest = digestor.hexdigest()
        return (kind, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, digest)
    except (OSError, ValueError):
        return None


def _undo_move(src: Path, dst: Path):
    """Reverse a move only when the original source is still absent and the moved object is unchanged."""
    expected = _fingerprint(dst)
    def _fn():
        if src.exists():
            return f"Cannot restore '{src.name}' because another file now occupies the original path."
        if _fingerprint(dst) != expected:
            return f"Cannot restore '{src.name}' because the moved item changed or disappeared after the original move."
        try:
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dst), str(src))
        except Exception as exc:
            raise RuntimeError(f"Unable to restore '{src.name}': {exc}") from exc
        return f"'{src.name}' is back in {src.parent.name}/."
    return _fn


def _undo_create(target: Path):
    """Reverse a create only when the object still matches the post-create identity."""
    expected = _fingerprint(target)
    def _fn():
        if _fingerprint(target) != expected:
            return f"'{target.name}' changed after creation — leaving it alone."
        if not target.exists():
            return f"'{target.name}' is already gone."
        if target.is_dir():
            if any(target.iterdir()):
                return (
                    f"'{target.name}' is not empty any more — "
                    f"leaving it alone rather than deleting your files."
                )
            target.rmdir()
        else:
            target.unlink()
        return f"Removed '{target.name}'."
    return _fn


def _undo_write(target: Path, previous: str | None, expected_after):
    """Restore prior text only when the target still matches Brahma's own write."""
    def _fn():
        if _fingerprint(target) != expected_after:
            return f"'{target.name}' changed after the original write — leaving it alone."
        if previous is None:
            if target.exists():
                _secure_unlink(target)
                return f"Removed '{target.name}' — it did not exist before."
            return f"'{target.name}' is already gone."
        expected_identity = tuple(expected_after[1:3]) if expected_after and len(expected_after) >= 3 else None
        _secure_write_text(target, previous, append=False, expected_identity=expected_identity)
        return f"Restored the previous contents of '{target.name}'."
    return _fn


_SAFE_ROOTS: list[Path] = [
    Path.home(),
]

def _is_link_like(path: Path) -> bool:
    """Reject symlinks, junctions, and Windows reparse points."""
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        if os.name == "nt":
            attrs = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
            reparse = getattr(_stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            if reparse and attrs & reparse:
                return True
        return False
    except OSError:
        return True


def _has_symlink_component(target: Path) -> bool:
    """Return True when any existing path component is link/reparse-like."""
    try:
        path = target.expanduser()
        current = Path(path.anchor) if path.anchor else Path(".")
        parts = path.parts[1:] if path.anchor else path.parts
        for part in parts:
            current = current / part
            if _is_link_like(current):
                return True
    except OSError:
        return True
    return False


def _is_hardlinked_regular_file(path: Path) -> bool:
    try:
        if not path.is_file() or _is_link_like(path):
            return False
        return int(path.stat().st_nlink) > 1
    except OSError:
        return True


def _is_safe_path(target: Path) -> bool:
    """Require an existing, non-symlink path tree inside an approved root."""
    try:
        if _has_symlink_component(target):
            return False
        resolved = target.resolve()
        return any(
            resolved == root.resolve() or resolved.is_relative_to(root.resolve())
            for root in _SAFE_ROOTS
        )
    except Exception:
        return False

def _get_desktop() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_DESKTOP_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Desktop"

def _get_downloads() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_DOWNLOAD_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Downloads"

def _get_documents() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_DOCUMENTS_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Documents"

def _get_pictures() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_PICTURES_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Pictures"

def _get_music() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_MUSIC_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Music"

def _get_videos() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_VIDEOS_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Videos"


def _resolve_path(raw: str) -> Path:
    shortcuts: dict[str, Path] = {
        "desktop":   _get_desktop(),
        "downloads": _get_downloads(),
        "documents": _get_documents(),
        "pictures":  _get_pictures(),
        "music":     _get_music(),
        "videos":    _get_videos(),
        "home":      Path.home(),
    }
    raw   = raw.strip().strip('"').strip("'")
    lower = raw.lower()
    if lower in shortcuts:
        return shortcuts[lower]

    # "desktop/notes/a.md" and "desktop\notes\a.md" — a shortcut followed by a
    # sub-path.  Without this branch the whole string falls through to the
    # relative-path return below and is resolved against the process CWD instead
    # of the real Desktop: an "Access denied" when the project lives outside the
    # home directory, or — worse — a silent write into a stray "desktop" folder
    # inside the project when it lives inside it.
    head, sep, rest = raw.replace("\\", "/").partition("/")
    if sep and head.lower() in shortcuts:
        rest = rest.strip("/")
        return shortcuts[head.lower()] / rest if rest else shortcuts[head.lower()]

    return Path(raw).expanduser()

def _secure_parent_fd(parent: Path):
    """Open a home-confined parent directory without following reparse components."""
    parent = Path(parent).absolute()
    if not _is_safe_path(parent):
        raise RuntimeError(f"Access denied: {parent}")
    root = Path.home().absolute()
    try:
        relative = parent.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"Access denied: {parent}") from exc

    if os.name == "nt" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        return None

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(str(root), flags)
    try:
        for part in relative.parts:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _secure_write_text(target: Path, content: str, *, append: bool = False, expected_identity=None) -> None:
    """Write a text file without following a raced leaf/parent when the OS supports openat."""
    target = Path(target).absolute()
    parent = target.parent
    leaf = target.name
    if not leaf:
        raise ValueError("A file name is required.")

    if os.name != "nt" and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"):
        parent_fd = _secure_parent_fd(parent)
        if parent_fd is None:
            raise RuntimeError("Secure parent descriptor could not be opened.")
        try:
            flags = os.O_WRONLY | os.O_NOFOLLOW
            if expected_identity is None:
                flags |= os.O_CREAT | os.O_EXCL
            elif append:
                flags |= os.O_APPEND
            else:
                flags |= os.O_TRUNC
            fd = os.open(leaf, flags, 0o600, dir_fd=parent_fd)
            try:
                if expected_identity is not None:
                    opened = os.fstat(fd)
                    actual_identity = (int(opened.st_dev), int(opened.st_ino))
                    if tuple(expected_identity) != actual_identity:
                        raise RuntimeError("Target changed identity before secure write; refusing the overwrite.")
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                fd = -1
            finally:
                if fd >= 0:
                    os.close(fd)
            return
        finally:
            os.close(parent_fd)

    if _is_link_like(target):
        raise RuntimeError("Target is a link/reparse point.")
    if expected_identity is not None:
        current = _fingerprint(target)
        if current is None or tuple(expected_identity) != tuple(current[1:3]):
            raise RuntimeError("Target changed identity before write; refusing the overwrite.")
    with target.open("a" if append else "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _secure_read_text(target: Path, max_chars: int, *, expected_identity=None) -> tuple[str, int]:
    """Read a validated text file without following a raced leaf on POSIX."""
    target = Path(target).absolute()
    if os.name != "nt" and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"):
        parent_fd = _secure_parent_fd(target.parent)
        if parent_fd is None:
            raise RuntimeError("Secure parent descriptor could not be opened.")
        try:
            fd = os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            try:
                opened = os.fstat(fd)
                actual_identity = (int(opened.st_dev), int(opened.st_ino))
                if expected_identity is not None and tuple(expected_identity) != actual_identity:
                    raise RuntimeError("Target changed identity before secure read; refusing access.")
                with os.fdopen(fd, "r", encoding="utf-8", errors="ignore") as handle:
                    fd = -1
                    content = handle.read(max_chars + 1)
                return content, int(opened.st_size)
            finally:
                if fd >= 0:
                    os.close(fd)
        finally:
            os.close(parent_fd)

    if _is_link_like(target):
        raise RuntimeError("Target is a link/reparse point.")
    if expected_identity is not None:
        current = _fingerprint(target)
        if current is None or tuple(expected_identity) != tuple(current[1:3]):
            raise RuntimeError("Target changed identity before read; refusing access.")
    return target.read_text(encoding="utf-8", errors="ignore")[:max_chars + 1], int(target.stat().st_size)


def _secure_unlink(target: Path) -> None:
    """Remove only the named leaf under a validated parent directory."""
    target = Path(target).absolute()
    if os.name != "nt" and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"):
        parent_fd = _secure_parent_fd(target.parent)
        if parent_fd is None:
            raise RuntimeError("Secure parent descriptor could not be opened.")
        try:
            os.unlink(target.name, dir_fd=parent_fd)
            return
        finally:
            os.close(parent_fd)
    target.unlink()


def _format_size(b: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"

def _safe_trash(target: Path) -> str:

    if not _SEND2TRASH:
        return (
            "send2trash is not installed. "
            "Run: pip install send2trash — "
            "Permanent deletion is disabled for safety."
        )
    send2trash.send2trash(str(target))
    return f"Moved to Trash: {target.name}"


def list_files(path: str = "desktop", show_hidden: bool = False) -> str:
    try:
        target = _resolve_path(path)
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Path not found: {target}"
        if not target.is_dir():
            return f"Not a directory: {target}"

        max_items = 500
        items = []
        total_items = 0
        for item in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            if not show_hidden and item.name.startswith("."):
                continue
            total_items += 1
            if len(items) >= max_items:
                continue
            try:
                if item.is_dir():
                    items.append(f"📁 {item.name}/")
                else:
                    size = _format_size(item.stat().st_size)
                    items.append(f"📄 {item.name} ({size})")
            except OSError:
                items.append(f"⚠️ {item.name} (metadata unavailable)")

        if not items:
            return f"Directory is empty: {target.name}/"

        if total_items > max_items:
            return (
                f"Contents of {target.name}/ (showing first {max_items} of {total_items} items):\n"
                + "\n".join(items)
            )
        return f"Contents of {target.name}/ ({total_items} items):\n" + "\n".join(items)

    except PermissionError:
        return f"Permission denied: {path}"
    except Exception as e:
        return f"Error listing files: {e}"


def create_file(path: str, name: str = "", content: str = "") -> str:
    try:
        base   = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        previous = None
        expected_identity = None
        if existed:
            try:
                if _is_link_like(target) or not target.is_file():
                    return f"Could not create file: existing target '{target.name}' is not a regular file."
                if _is_hardlinked_regular_file(target):
                    return f"Could not create file: existing target '{target.name}' has multiple hard links; refusing an unsafe overwrite."
                previous = target.read_text(encoding="utf-8")
                stat = target.stat(follow_symlinks=False)
                expected_identity = (int(stat.st_dev), int(stat.st_ino))
            except Exception as exc:
                return f"Could not create file: existing target '{target.name}' could not be read safely: {exc}"
        _secure_write_text(target, content, append=False, expected_identity=expected_identity)
        expected_after = _fingerprint(target)
        if expected_after is None:
            return f"Could not create file: unable to verify the created file safely."
        push_undo(f"created {target.name}",
                  _undo_write(target, previous, expected_after) if existed else _undo_create(target))
        return f"File created: {target.name}"
    except Exception as e:
        return f"Could not create file: {e}"


def create_folder(path: str, name: str = "") -> str:
    try:
        base   = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        already = target.exists()
        target.mkdir(parents=True, exist_ok=True)
        # Only offer to undo a folder we actually made. "mkdir -p" on something
        # that was already there is not a change, and undoing it would delete a
        # directory the user has had for years.
        if not already:
            push_undo(f"created folder {target.name}", _undo_create(target))
        return f"Folder created: {target.name}"
    except Exception as e:
        return f"Could not create folder: {e}"


def delete_file(path: str, name: str = "") -> str:
    try:
        base   = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Not found: {target.name}"

        # Safe-directory check — protect critical user folders
        protected = {
            _get_desktop(), _get_downloads(), _get_documents(),
            _get_pictures(), _get_music(), _get_videos(), Path.home()
        }
        if target.resolve() in {p.resolve() for p in protected}:
            return f"Protected directory, cannot delete: {target.name}"

        original = target.resolve()
        result   = _safe_trash(target)
        if result.startswith("Moved to Trash"):
            push_undo(f"deleted {original.name}",
                      lambda p=original: _restore_from_trash(p))
        return result

    except PermissionError:
        return f"Permission denied: {path}"
    except Exception as e:
        return f"Could not delete: {e}"


def move_file(path: str, name: str = "", destination: str = "") -> str:
    try:
        base   = _resolve_path(path)
        src    = (base / name) if name else base
        dst    = _resolve_path(destination) if destination else None

        if not src.exists():
            return f"Source not found: {src.name}"
        if dst is None:
            return "No destination specified."
        if not _is_safe_path(src):
            return f"Access denied (source): {src}"
        if not _is_safe_path(dst):
            return f"Access denied (destination): {dst}"

        if dst.is_dir():
            dst = dst / src.name
        if dst.exists() or dst.is_symlink():
            return f"Destination already exists: {dst.name}. Refusing to overwrite it."

        dst.parent.mkdir(parents=True, exist_ok=True)
        if not _is_safe_path(dst.parent):
            return f"Access denied (destination parent): {dst.parent}"
        origin = src.resolve()
        shutil.move(str(src), str(dst))
        push_undo(f"moved {origin.name} to {dst.parent.name}/",
                  _undo_move(origin, dst.resolve()))
        return f"Moved: {src.name} → {dst.parent.name}/"

    except Exception as e:
        return f"Could not move: {e}"


def copy_file(path: str, name: str = "", destination: str = "") -> str:
    try:
        base = _resolve_path(path)
        src  = (base / name) if name else base
        dst  = _resolve_path(destination) if destination else None

        if not src.exists():
            return f"Source not found: {src.name}"
        if dst is None:
            return "No destination specified."
        if not _is_safe_path(src):
            return f"Access denied (source): {src}"
        if not _is_safe_path(dst):
            return f"Access denied (destination): {dst}"

        if dst.is_dir():
            dst = dst / src.name
        if dst.exists() or dst.is_symlink():
            return f"Destination already exists: {dst.name}. Refusing to overwrite it."

        dst.parent.mkdir(parents=True, exist_ok=True)
        if not _is_safe_path(dst.parent):
            return f"Access denied (destination parent): {dst.parent}"

        if src.is_dir():
            shutil.copytree(str(src), str(dst))
        else:
            shutil.copy2(str(src), str(dst))

        # The undo for a copy is deleting the copy — never the original.
        _copy = dst.resolve()
        def _undo_copy():
            if not _copy.exists():
                return f"The copy '{_copy.name}' is already gone."
            if _copy.is_dir():
                shutil.rmtree(_copy)
            else:
                _copy.unlink()
            return f"Removed the copy in {_copy.parent.name}/."
        push_undo(f"copied {src.name} to {dst.parent.name}/", _undo_copy)

        return f"Copied: {src.name} → {dst.parent.name}/"

    except Exception as e:
        return f"Could not copy: {e}"


def rename_file(path: str, name: str = "", new_name: str = "") -> str:
    try:
        base     = _resolve_path(path)
        target   = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Not found: {target.name}"
        if not new_name:
            return "No new name provided."

        new_name = str(new_name).strip()
        new_path = target.parent / new_name
        if not _is_safe_path(new_path):
            return f"Access denied (destination): {new_path}"
        if new_path.exists():
            return f"A file named '{new_name}' already exists here."

        old_path = target.resolve()
        target.rename(new_path)
        push_undo(f"renamed {old_path.name} to {new_name}",
                  _undo_move(old_path, new_path.resolve()))
        return f"Renamed: {target.name} → {new_name}"

    except Exception as e:
        return f"Could not rename: {e}"


def read_file(path: str, name: str = "", max_chars: int = 4000) -> str:
    try:
        base   = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"File not found: {target.name}"
        if not target.is_file():
            return f"Not a file: {target.name}"

        try:
            max_chars = max(1, min(int(max_chars), 1_000_000))
        except (TypeError, ValueError):
            return "Could not read file: max_chars must be a positive integer."
        try:
            stat_result = target.stat(follow_symlinks=False)
            expected_identity = (int(stat_result.st_dev), int(stat_result.st_ino))
            content, total_bytes = _secure_read_text(
                target,
                max_chars,
                expected_identity=expected_identity,
            )
        except OSError as exc:
            return f"Could not read file: {exc}"
        if len(content) > max_chars:
            content = content[:max_chars] + f"\n\n[Truncated — {total_bytes} bytes on disk]"
        return content

    except Exception as e:
        return f"Could not read file: {e}"


def write_file(path: str, name: str = "", content: str = "",
               append: bool = False) -> str:
    try:
        base   = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        target.parent.mkdir(parents=True, exist_ok=True)

        # Snapshot before writing. None means "did not exist", which is a
        # different undo (delete it) from "existed and had this in it".
        previous: str | None = None
        undoable = True
        expected_identity = None
        existed = target.exists() or target.is_symlink()
        if existed:
            if _is_link_like(target) or not target.is_file():
                return "Could not write file: existing target is not a regular file."
            if _is_hardlinked_regular_file(target):
                return "Could not write file: existing target has multiple hard links; refusing an unsafe overwrite."
            try:
                size = target.stat().st_size
                if size > _UNDO_CONTENT_LIMIT:
                    if not append:
                        return (
                            "Could not write file: existing target is too large to "
                            "overwrite safely without a verified rollback copy."
                        )
                    undoable = False
                else:
                    previous = target.read_text(encoding="utf-8")
                    stat = target.stat(follow_symlinks=False)
                    expected_identity = (int(stat.st_dev), int(stat.st_ino))
            except Exception as exc:
                return (
                    "Could not write file: existing target could not be read safely; "
                    f"refusing to overwrite it: {exc}"
                )

        mode = "a" if append else "w"
        if not _is_safe_path(target.parent):
            return f"Access denied: {target.parent}"
        if _is_link_like(target) or _is_hardlinked_regular_file(target):
            return f"Could not write file: link/reparse or hard-linked targets are not permitted."
        _secure_write_text(target, content, append=append, expected_identity=expected_identity)

        expected_after = _fingerprint(target) if undoable else None
        if undoable and expected_after is None:
            return f"Could not write file: unable to verify the resulting file safely."

        action = "Appended to" if append else "Written to"
        if undoable:
            push_undo(f"wrote to {target.name}", _undo_write(target, previous, expected_after))
            return f"{action}: {target.name}"
        return (f"{action}: {target.name}. "
                f"(Too large to keep a copy of the old contents, so this one "
                f"cannot be undone.)")
    except Exception as e:
        return f"Could not write file: {e}"


def find_files(name: str = "", extension: str = "",
               path: str = "home", max_results: int = 20) -> str:
    try:
        search_path = _resolve_path(path)
        if not _is_safe_path(search_path):
            return f"Access denied: {search_path}"
        if not search_path.exists():
            return f"Search path not found: {path}"

        results    = []
        dir_count  = 0
        max_dirs   = 500  # performance + safety limit

        for item in search_path.rglob("*"):
            if item.is_dir():
                dir_count += 1
                if dir_count > max_dirs:
                    break
                continue
            if not item.is_file():
                continue
            if extension and item.suffix.lower() != extension.lower():
                continue
            if name and name.lower() not in item.name.lower():
                continue
            size = _format_size(item.stat().st_size)
            results.append(f"📄 {item.name} ({size}) — {item.parent}")
            if len(results) >= max_results:
                break

        if not results:
            query = name or extension or "files"
            return f"No {query} found in {search_path.name}/"

        return f"Found {len(results)} file(s):\n" + "\n".join(results)

    except Exception as e:
        return f"Search error: {e}"


def get_largest_files(path: str = "downloads", count: int = 10) -> str:
    count = min(count, 50)  # maksimum 50
    try:
        search_path = _resolve_path(path)
        if not _is_safe_path(search_path):
            return f"Access denied: {search_path}"
        if not search_path.exists():
            return f"Path not found: {path}"

        files = []
        for item in search_path.rglob("*"):
            if item.is_file():
                try:
                    files.append((item.stat().st_size, item))
                except Exception:
                    continue

        files.sort(reverse=True)
        top = files[:count]

        if not top:
            return "No files found."

        lines = [f"Top {len(top)} largest files in {search_path.name}/:"]
        for size, f in top:
            lines.append(f"  {_format_size(size):>10}  {f.name}  ({f.parent})")

        return "\n".join(lines)

    except Exception as e:
        return f"Error: {e}"


def get_disk_usage(path: str = "home") -> str:
    try:
        target = _resolve_path(path)
        usage  = shutil.disk_usage(target)
        pct    = usage.used / usage.total * 100
        return (
            f"Disk usage ({target}):\n"
            f"  Total : {_format_size(usage.total)}\n"
            f"  Used  : {_format_size(usage.used)} ({pct:.1f}%)\n"
            f"  Free  : {_format_size(usage.free)}"
        )
    except Exception as e:
        return f"Could not get disk usage: {e}"


def organize_desktop(mode: str = "by_type") -> str:
    from actions.desktop_organizer_mcp import get_organizer_engine
    return get_organizer_engine().organize(target="desktop", mode=mode)


def organize_folder(folder_path: str, mode: str = "by_type") -> str:
    from actions.desktop_organizer_mcp import get_organizer_engine
    target = folder_path or "downloads"
    return get_organizer_engine().organize(target=target, mode=mode)


def get_file_info(path: str, name: str = "") -> str:
    try:
        base   = _resolve_path(path)
        target = (base / name) if name else base
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Not found: {target.name}"

        stat = target.stat()
        info = {
            "Name":      target.name,
            "Type":      "Folder" if target.is_dir() else "File",
            "Size":      _format_size(stat.st_size),
            "Location":  str(target.parent),
            "Created":   datetime.fromtimestamp(stat.st_ctime).strftime("%Y-%m-%d %H:%M"),
            "Modified":  datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "Extension": target.suffix or "—",
        }
        return "\n".join(f"  {k}: {v}" for k, v in info.items())

    except Exception as e:
        return f"Could not get file info: {e}"

def file_controller(
    parameters: dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    action = params.get("action", "").lower().strip()
    path   = params.get("path", "desktop")
    name   = params.get("name", "")

    if player:
        player.write_log(f"[file] {action} {name or path}")

    try:
        if action == "list":
            return list_files(path)

        elif action == "create_file":
            return create_file(path, name=name, content=params.get("content", ""))

        elif action == "create_folder":
            return create_folder(path, name=name)

        elif action == "delete":
            return delete_file(path, name=name)

        elif action == "move":
            return move_file(path, name=name, destination=params.get("destination", ""))

        elif action == "copy":
            return copy_file(path, name=name, destination=params.get("destination", ""))

        elif action == "rename":
            return rename_file(path, name=name, new_name=params.get("new_name", ""))

        elif action == "read":
            return read_file(path, name=name)

        elif action == "write":
            return write_file(
                path, name=name,
                content=params.get("content", ""),
                append=params.get("append", False)
            )

        elif action == "find":
            return find_files(
                name=name or params.get("name", ""),
                extension=params.get("extension", ""),
                path=path,
                max_results=min(int(params.get("max_results", 20)), 50),
            )

        elif action == "largest":
            return get_largest_files(
                path=path,
                count=int(params.get("count", 10)),
            )

        elif action == "disk_usage":
            return get_disk_usage(path)

        elif action == "organize_desktop":
            return organize_desktop(mode=params.get("mode", "by_type"))

        elif action in ("organize_folder", "organize"):
            return organize_folder(path or params.get("folder", ""), mode=params.get("mode", "by_type"))

        elif action in ("preview_organize", "organize_preview", "preview"):
            from actions.desktop_organizer_mcp import get_organizer_engine
            return get_organizer_engine().preview(target=path or "desktop", mode=params.get("mode", "by_type"))

        elif action in ("undo_organize", "organize_undo", "undo"):
            from actions.desktop_organizer_mcp import get_organizer_engine
            return get_organizer_engine().undo()

        elif action in ("find_duplicates", "duplicates", "dupes"):
            from actions.desktop_organizer_mcp import get_organizer_engine
            return get_organizer_engine().find_duplicates(target=path or "downloads")

        elif action == "info":
            return get_file_info(path, name=name)

        else:
            return f"Unknown action: '{action}'"

    except Exception as e:
        return f"File controller error ({action}): {e}"


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "file_controller",
    "description": "Manages files and folders: list, create, delete, move, copy, rename, read, write, find, disk usage.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "list | create_file | create_folder | delete | move | copy | rename | read | write | find | largest | disk_usage | organize_desktop | organize_folder | preview_organize | undo_organize | find_duplicates | info"
            },
            "path": {
                "type": "STRING",
                "description": "File/folder path or shortcut: desktop, downloads, documents, home"
            },
            "destination": {
                "type": "STRING",
                "description": "Destination path for move/copy"
            },
            "new_name": {
                "type": "STRING",
                "description": "New name for rename"
            },
            "content": {
                "type": "STRING",
                "description": "Content for create_file/write"
            },
            "name": {
                "type": "STRING",
                "description": "File name to search for"
            },
            "extension": {
                "type": "STRING",
                "description": "File extension to search (e.g. .pdf)"
            },
            "count": {
                "type": "INTEGER",
                "description": "Number of results for largest"
            }
        },
        "required": [
            "action"
        ]
    },
    "handler": file_controller,
}
