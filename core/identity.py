import json
import os
import stat as _stat
import threading
import uuid
from pathlib import Path
from typing import Dict, Any, List

from core.runtime_paths import IDENTITY_PATH

def get_base_dir() -> Path:
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _is_link_like(path: Path) -> bool:
    path = Path(path)
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None and is_junction():
        return True
    if os.name == "nt":
        try:
            attrs = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
            reparse = getattr(_stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            if reparse and attrs & reparse:
                return True
        except OSError:
            return True
    return False


def _assert_safe_path(path: Path) -> None:
    current = Path(path)
    while True:
        if _is_link_like(current):
            raise RuntimeError(f"Identity persistence path must not contain a symlink, junction, or reparse point: {current}")
        try:
            if current.exists() and current.is_file():
                stat_result = current.stat(follow_symlinks=False)
                if int(getattr(stat_result, "st_nlink", 1)) > 1:
                    raise RuntimeError(f"Identity persistence path has multiple hard links: {current}")
        except OSError as exc:
            raise RuntimeError(f"Identity persistence path could not be inspected safely: {current}") from exc
        if current.parent == current:
            break
        current = current.parent


class IdentityService:
    def __init__(self):
        self._lock = threading.RLock()
        # Identity is mutable runtime state, so packaged installs keep it in user data.
        self.config_file = IDENTITY_PATH
        self.bundled_config_file = get_base_dir() / "config" / "identity.json"
        self.data: Dict[str, Any] = {
            "owner": {
                "name": "",
                "preferred_name": "",
                "role": "",
                "location": "",
                "interests": [],
                "about": ""
            },
            "assistant": {
                "name": "Brahma",
                "application_name": "Brahma Evo",
                "title": "Personal AI Assistant"
            },
            "behavior": {
                "mode": "professional",
                "proactive": True,
                "custom_instructions": ""
            },
            "system": {
                "shared_computer": False
            }
        }
        self.load()

    def load(self):
        with self._lock:
            self._load_unlocked()

    def _load_unlocked(self):
        # Seed from a bundled identity template when present, then overlay persisted
        # user state. Corrupt or link-like persisted state must fail closed rather
        # than silently reverting the user's identity to defaults or the template.
        sources = [self.bundled_config_file, self.config_file]
        loaded_any = False
        for source in sources:
            if not source.exists():
                continue
            _assert_safe_path(source)
            try:
                loaded_data = json.loads(source.read_text(encoding="utf-8"))
            except Exception as exc:
                raise RuntimeError(f"Identity configuration is corrupt or unreadable: {source}") from exc
            if not isinstance(loaded_data, dict):
                raise RuntimeError(f"Identity configuration has an invalid root schema: {source}")
            loaded_any = True
            for section, values in loaded_data.items():
                if section in self.data and isinstance(values, dict):
                    self.data[section].update(values)
                else:
                    self.data[section] = values

        if not self.config_file.exists() and not loaded_any:
            self.save()

    def save(self):
        with self._lock:
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            _assert_safe_path(self.config_file)
            temp = self.config_file.with_name(
                f".{self.config_file.name}.{uuid.uuid4().hex}.tmp"
            )
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                    fd = -1
                    handle.write(json.dumps(self.data, indent=4, ensure_ascii=False))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, self.config_file)
                _assert_safe_path(self.config_file)
                persisted = json.loads(self.config_file.read_text(encoding="utf-8"))
                if not isinstance(persisted, dict) or persisted != self.data:
                    raise RuntimeError("Identity save verification found a mismatched final state.")
            except Exception:
                try:
                    if fd >= 0:
                        os.close(fd)
                except OSError:
                    pass
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise

    def _set_value(self, section: str, key: str, value: Any) -> None:
        with self._lock:
            self.data.setdefault(section, {})[key] = value
            self.save()

    # Assistant methods
    def get_assistant_name(self) -> str:
        val = self.data["assistant"].get("name", "Brahma")
        return val if val is not None else "Brahma"
        
    def set_assistant_name(self, name: str):
        self._set_value("assistant", "name", name)

    def get_application_name(self) -> str:
        val = self.data["assistant"].get("application_name", "Brahma Evo")
        return val if val is not None else "Brahma Evo"
        
    def set_application_name(self, name: str):
        self._set_value("assistant", "application_name", name)

    def get_assistant_title(self) -> str:
        val = self.data["assistant"].get("title", "Personal AI Assistant")
        return val if val is not None else "Personal AI Assistant"
        
    def set_assistant_title(self, title: str):
        self._set_value("assistant", "title", title)

    # Owner Profile methods
    def get_owner_name(self) -> str:
        val = self.data["owner"].get("name", "")
        return val if val is not None else ""
        
    def set_owner_name(self, name: str):
        with self._lock:
            self.data["owner"]["name"] = name
            if not self.data["owner"].get("preferred_name"):
                self.data["owner"]["preferred_name"] = name
            self.save()

    def get_owner_role(self) -> str:
        val = self.data["owner"].get("role", "")
        return val if val is not None else ""
        
    def set_owner_role(self, role: str):
        self._set_value("owner", "role", role)

    def get_owner_location(self) -> str:
        val = self.data["owner"].get("location", "")
        return val if val is not None else ""
        
    def set_owner_location(self, location: str):
        self._set_value("owner", "location", location)

    def get_owner_interests(self) -> List[str]:
        val = self.data["owner"].get("interests", [])
        return val if val is not None else []
        
    def set_owner_interests(self, interests: List[str]):
        self._set_value("owner", "interests", list(interests or []))

    def get_owner_about(self) -> str:
        val = self.data["owner"].get("about", "")
        return val if val is not None else ""
        
    def set_owner_about(self, about: str):
        self._set_value("owner", "about", about)

    # Behavior methods
    def get_behavior_mode(self) -> str:
        val = self.data["behavior"].get("mode", "professional")
        return val if val is not None else "professional"
        
    def set_behavior_mode(self, mode: str):
        self._set_value("behavior", "mode", mode)

    def get_custom_instructions(self) -> str:
        val = self.data["behavior"].get("custom_instructions", "")
        base = val if val is not None else ""
        # Inject the single self-awareness model at the existing system-prompt
        # boundary. This keeps identity grounding consistent without creating
        # another prompt pipeline or background worker.
        try:
            from core.self_model import self_awareness
            awareness = self_awareness.prompt_block()
            return (base + "\n\n" + awareness).strip() if awareness else base
        except Exception:
            return base
        
    def set_custom_instructions(self, instructions: str):
        self._set_value("behavior", "custom_instructions", instructions)
        
    def is_proactive(self) -> bool:
        val = self.data["behavior"].get("proactive", True)
        return val if val is not None else True
        
    def set_proactive(self, proactive: bool):
        self._set_value("behavior", "proactive", bool(proactive))

    # System methods
    def is_shared_computer(self) -> bool:
        val = self.data["system"].get("shared_computer", False)
        return val if val is not None else False
        
    def set_shared_computer(self, is_shared: bool):
        self._set_value("system", "shared_computer", bool(is_shared))

    def is_setup_complete(self) -> bool:
        # Consider setup complete if owner name is provided
        val = self.data["owner"].get("name", "")
        return bool(val.strip()) if val else False

# Global singleton instance
identity = IdentityService()
