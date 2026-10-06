"""
Learned Rules Engine for Brahma AI
Enables continuous self-improvement by capturing user corrections, preferred behaviors,
and habits, then injecting them dynamically into the core LLM prompt without modifying source code.
"""

from __future__ import annotations
from core.user_paths import get_user_data_dir

import json
import logging
import os
import threading
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("LearnedRules")

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = get_user_data_dir() / "config"
RULES_FILE = CONFIG_DIR / "learned_rules.json"

_SECRET_RE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|secret|bearer)\s*[:=]\s*\S+"
)
_TOKEN_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gsk_[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")


class LearnedRulesEngine:
    """Manages persistent behavioral rules and directives learned from the user."""

    _lock = threading.RLock()

    @staticmethod
    def _load_raw() -> List[Dict[str, Any]]:
        if not RULES_FILE.exists():
            return []
        try:
            with open(RULES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError("Learned-rules file has an invalid root schema.")
            sanitized: List[Dict[str, Any]] = []
            changed = False
            for item in data:
                if not isinstance(item, dict):
                    changed = True
                    continue
                current = dict(item)
                rule = str(current.get("rule") or "")
                if _SECRET_RE.search(rule) or _TOKEN_RE.search(rule):
                    current["rule"] = _SECRET_RE.sub("[REDACTED_SECRET]", rule)
                    current["rule"] = _TOKEN_RE.sub("[REDACTED_SECRET]", current["rule"])
                    changed = True
                sanitized.append(current)
            if changed:
                LearnedRulesEngine._save_raw(sanitized)
            return sanitized
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            quarantine = RULES_FILE.with_name(
                f"{RULES_FILE.name}.corrupt-{int(time.time())}-{uuid.uuid4().hex[:8]}"
            )
            try:
                RULES_FILE.replace(quarantine)
                logger.warning("[LearnedRules] Quarantined corrupted rules file as %s", quarantine.name)
            except OSError:
                logger.warning("[LearnedRules] Failed to quarantine corrupted rules state: %s", exc)
            return []
        except OSError as exc:
            logger.error("[LearnedRules] Refusing to mutate unreadable rules state: %s", exc)
            raise RuntimeError("Unable to read persistent learned-rules state.") from exc

    @staticmethod
    def _save_raw(rules: List[Dict[str, Any]]) -> bool:
        try:
            with LearnedRulesEngine._lock:
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                temp = RULES_FILE.with_name(
                    f".{RULES_FILE.name}.{os.getpid()}-{uuid.uuid4().hex}.tmp"
                )
                try:
                    temp.write_text(
                        json.dumps(rules, indent=4, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    os.replace(temp, RULES_FILE)
                finally:
                    try:
                        temp.unlink(missing_ok=True)
                    except OSError:
                        pass
            return True
        except Exception as e:
            logger.error("[LearnedRules] Failed to save rules: %s", e)
            return False

    @classmethod
    def add_rule(cls, rule_text: str, category: str = "general", origin: str = "user_correction") -> Dict[str, Any]:
        """Adds a new learned rule to the database."""
        clean_text = str(rule_text or "").strip()
        if not clean_text:
            return {"success": False, "message": "Rule text cannot be empty."}
        if _SECRET_RE.search(clean_text) or _TOKEN_RE.search(clean_text):
            return {"success": False, "message": "Credential-like values cannot be stored as learned rules."}

        with cls._lock:
            try:
                rules = cls._load_raw()
            except (RuntimeError, OSError) as exc:
                return {"success": False, "message": str(exc)}

            for r in rules:
                if str(r.get("rule", "")).lower() == clean_text.lower():
                    r["active"] = True
                    r["updated_at"] = time.time()
                    if not cls._save_raw(rules):
                        return {"success": False, "message": "Unable to persist learned rule state."}
                    return {"success": True, "rule": r, "message": "Rule already exists and is active."}

            now = time.time()
            new_rule = {
                "id": str(uuid.uuid4())[:8],
                "rule": clean_text,
                "category": str(category or "general").strip() or "general",
                "origin": str(origin or "user_correction").strip() or "user_correction",
                "active": True,
                "created_at": now,
                "updated_at": now,
            }
            rules.append(new_rule)
            if not cls._save_raw(rules):
                return {"success": False, "message": "Unable to persist learned rule."}
            return {"success": True, "rule": new_rule, "message": f"Learned new rule: '{clean_text}'"}

    @classmethod
    def list_rules(cls, active_only: bool = False) -> List[Dict[str, Any]]:
        """Returns all stored rules."""
        rules = cls._load_raw()
        if active_only:
            return [r for r in rules if r.get("active", True)]
        return rules

    @classmethod
    def toggle_rule(cls, rule_id: str) -> Dict[str, Any]:
        """Toggles a rule on or off."""
        with cls._lock:
            try:
                rules = cls._load_raw()
            except RuntimeError as exc:
                return {"success": False, "message": str(exc)}
            for r in rules:
                if r.get("id") == rule_id:
                    r["active"] = not r.get("active", True)
                    r["updated_at"] = time.time()
                    if not cls._save_raw(rules):
                        return {"success": False, "message": "Unable to persist learned rule state."}
                    status = "activated" if r["active"] else "deactivated"
                    return {"success": True, "message": f"Rule {rule_id} {status}."}
            return {"success": False, "message": f"Rule ID '{rule_id}' not found."}

    @classmethod
    def delete_rule(cls, rule_id: str) -> Dict[str, Any]:
        """Deletes a rule permanently."""
        with cls._lock:
            try:
                rules = cls._load_raw()
            except RuntimeError as exc:
                return {"success": False, "message": str(exc)}
            initial_len = len(rules)
            rules = [r for r in rules if r.get("id") != rule_id]
            if len(rules) < initial_len:
                if not cls._save_raw(rules):
                    return {"success": False, "message": "Unable to persist learned rule state."}
                return {"success": True, "message": f"Rule {rule_id} deleted."}
            return {"success": False, "message": f"Rule ID '{rule_id}' not found."}

    @classmethod
    def get_prompt_injections(cls) -> str:
        """Formats all active rules for runtime system prompt injection."""
        active = [r.get("rule", "").strip() for r in cls.list_rules(active_only=True) if r.get("rule")]
        if not active:
            return ""

        lines = ["\nUSER PREFERENCES & LEARNED BEHAVIORAL DIRECTIVES:"]
        for i, rule in enumerate(active, 1):
            lines.append(f"{i}. {rule}")
        return "\n".join(lines) + "\n"
