import json
import os
from pathlib import Path
from typing import Dict, Any, List

from core.runtime_paths import IDENTITY_PATH

def get_base_dir() -> Path:
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

class IdentityService:
    def __init__(self):
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
        # Seed from a bundled identity template when present, then overlay persisted
        # user state. Saving always targets the writable user-data copy.
        sources = [self.bundled_config_file, self.config_file]
        loaded_any = False
        for source in sources:
            if not source.exists():
                continue
            try:
                loaded_data = json.loads(source.read_text(encoding="utf-8"))
                if not isinstance(loaded_data, dict):
                    continue
                loaded_any = True
                for section, values in loaded_data.items():
                    if section in self.data and isinstance(values, dict):
                        self.data[section].update(values)
                    else:
                        self.data[section] = values
            except Exception as e:
                print(f"Error loading identity config: {e}")
        if not self.config_file.exists() and not loaded_any:
            self.save()

    def save(self):
        try:
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=4)
        except Exception as e:
            print(f"Error saving identity config: {e}")

    # Assistant methods
    def get_assistant_name(self) -> str:
        val = self.data["assistant"].get("name", "Brahma")
        return val if val is not None else "Brahma"
        
    def set_assistant_name(self, name: str):
        self.data["assistant"]["name"] = name
        self.save()

    def get_application_name(self) -> str:
        val = self.data["assistant"].get("application_name", "Brahma Evo")
        return val if val is not None else "Brahma Evo"
        
    def set_application_name(self, name: str):
        self.data["assistant"]["application_name"] = name
        self.save()

    def get_assistant_title(self) -> str:
        val = self.data["assistant"].get("title", "Personal AI Assistant")
        return val if val is not None else "Personal AI Assistant"
        
    def set_assistant_title(self, title: str):
        self.data["assistant"]["title"] = title
        self.save()

    # Owner Profile methods
    def get_owner_name(self) -> str:
        val = self.data["owner"].get("name", "")
        return val if val is not None else ""
        
    def set_owner_name(self, name: str):
        self.data["owner"]["name"] = name
        if not self.data["owner"].get("preferred_name"):
            self.data["owner"]["preferred_name"] = name
        self.save()

    def get_owner_role(self) -> str:
        val = self.data["owner"].get("role", "")
        return val if val is not None else ""
        
    def set_owner_role(self, role: str):
        self.data["owner"]["role"] = role
        self.save()

    def get_owner_location(self) -> str:
        val = self.data["owner"].get("location", "")
        return val if val is not None else ""
        
    def set_owner_location(self, location: str):
        self.data["owner"]["location"] = location
        self.save()

    def get_owner_interests(self) -> List[str]:
        val = self.data["owner"].get("interests", [])
        return val if val is not None else []
        
    def set_owner_interests(self, interests: List[str]):
        self.data["owner"]["interests"] = interests
        self.save()

    def get_owner_about(self) -> str:
        val = self.data["owner"].get("about", "")
        return val if val is not None else ""
        
    def set_owner_about(self, about: str):
        self.data["owner"]["about"] = about
        self.save()

    # Behavior methods
    def get_behavior_mode(self) -> str:
        val = self.data["behavior"].get("mode", "professional")
        return val if val is not None else "professional"
        
    def set_behavior_mode(self, mode: str):
        self.data["behavior"]["mode"] = mode
        self.save()

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
        self.data["behavior"]["custom_instructions"] = instructions
        self.save()
        
    def is_proactive(self) -> bool:
        val = self.data["behavior"].get("proactive", True)
        return val if val is not None else True
        
    def set_proactive(self, proactive: bool):
        self.data["behavior"]["proactive"] = proactive
        self.save()

    # System methods
    def is_shared_computer(self) -> bool:
        val = self.data["system"].get("shared_computer", False)
        return val if val is not None else False
        
    def set_shared_computer(self, is_shared: bool):
        self.data["system"]["shared_computer"] = is_shared
        self.save()

    def is_setup_complete(self) -> bool:
        # Consider setup complete if owner name is provided
        val = self.data["owner"].get("name", "")
        return bool(val.strip()) if val else False

# Global singleton instance
identity = IdentityService()
