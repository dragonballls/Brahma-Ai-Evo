"""
Dynamic Tool Registry & Hot-Plugging Engine
Part of Project Ultron for Brahma AI.

Manages persistent synthetic skills, dynamic tool declarations for Gemini Live,
hot-reloading, execution dispatch, and lifecycle management.
"""

from __future__ import annotations

import asyncio
import ast
import importlib.util
import inspect
import json
import logging
import os
import re
import shutil
import threading
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.user_paths import get_user_data_dir

logger = logging.getLogger("DynamicToolRegistry")

_CREDENTIAL_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bgsk_[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"),
)


def _redact_text(value: object) -> str:
    text = str(value or "")
    for pattern in _CREDENTIAL_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


PROJECT_ROOT = Path(__file__).resolve().parent.parent
FEATURES_DIR = PROJECT_ROOT / "features"
APPDATA_SKILLS_DIR = get_user_data_dir() / "skills"


def _is_link_like(path: Path) -> bool:
    """Reject symlinks, junctions, and Windows reparse points before loading code."""
    path = Path(path)
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None and is_junction():
        return True
    if os.name == "nt":
        try:
            attrs = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
            reparse = getattr(getattr(os, "stat", None), "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            if reparse and attrs & reparse:
                return True
        except OSError:
            return True
    return False


class DynamicSkill:
    """Represents a loaded, runnable feature or synthetic skill in Brahma AI."""

    def __init__(self, skill_path: Path, manifest: Dict[str, Any], module: Any = None, *, untrusted: bool = False):
        self.skill_path = skill_path
        self.skill_dir = skill_path if skill_path.is_dir() else skill_path.parent
        self.manifest = manifest
        self.module = module
        self._state_lock = threading.RLock()
        self.name: str = str(manifest.get("name") or skill_path.stem).strip()
        self.description: str = str(manifest.get("description") or "")
        raw_parameters = manifest.get("parameters")
        self.parameters: Dict[str, Any] = (
            dict(raw_parameters) if isinstance(raw_parameters, dict)
            else {"type": "OBJECT", "properties": {}}
        )
        raw_triggers = manifest.get("triggers")
        self.triggers: List[str] = (
            [str(item) for item in raw_triggers if str(item).strip()]
            if isinstance(raw_triggers, (list, tuple, set)) else []
        )
        raw_aliases = manifest.get("aliases")
        self.aliases: List[str] = (
            [str(item) for item in raw_aliases if str(item).strip()]
            if isinstance(raw_aliases, (list, tuple, set)) else []
        )
        self.active: bool = bool(manifest.get("active", True))
        self.version: str = str(manifest.get("version") or "1.0.0")
        self.created_at: float = manifest.get("created_at", time.time())
        self.invocations: int = manifest.get("invocations", 0)
        self.last_error: Optional[str] = manifest.get("last_error", None)
        self.untrusted = bool(untrusted)

    def _load_module(self) -> Any:
        with self._state_lock:
            if self.module is None:
                code_path = self.skill_path / "skill.py" if self.skill_path.is_dir() else self.skill_path
                code_path = code_path.resolve(strict=True)
                if code_path.is_symlink():
                    raise RuntimeError(f"Refusing to load symlinked skill code: {code_path}")
                if self.skill_dir.is_symlink():
                    raise RuntimeError(f"Refusing to load skill from a symlinked directory: {self.skill_dir}")
                source = code_path.read_text(encoding="utf-8")
                if self.untrusted:
                    from core.skill_crucible import SkillCrucible
                    safe, reason = SkillCrucible.validate_ast(source)
                    if not safe:
                        raise RuntimeError(f"Persisted skill failed runtime validation: {_redact_text(reason)}")
                module_name = f"brahma_skill_{self.name}_{abs(hash(str(code_path)))}"
                module = type(sys)(module_name)
                module.__file__ = str(code_path)
                module.__package__ = module_name.rpartition(".")[0]
                sys.modules[module_name] = module
                try:
                    code = compile(source, str(code_path), "exec", dont_inherit=True)
                    exec(code, module.__dict__)
                except Exception:
                    sys.modules.pop(module_name, None)
                    raise
                self.module = module
            return self.module

    def to_tool_declaration(self) -> Dict[str, Any]:
        """Returns Gemini function declaration dict."""
        return {
            "name": self.name,
            "description": f"{self.description} (Autonomous dedicated tool: prefer calling this tool directly over generic web search).",
            "parameters": self.parameters,
        }

    def execute_sync(self, **kwargs) -> Any:
        """Executes the skill synchronously (safe for worker threads)."""
        module = self._load_module()
        if not hasattr(module, "execute"):
            raise AttributeError(f"Feature '{self.name}' has no 'execute' function.")
        func = getattr(module, "execute")
        with self._state_lock:
            self.invocations += 1
        if inspect.iscoroutinefunction(func):
            return asyncio.run(func(**kwargs))
        return func(**kwargs)

    async def execute_async(self, **kwargs) -> Any:
        """Executes the skill asynchronously without thread-pool registration conflicts."""
        module = self._load_module()
        if not hasattr(module, "execute"):
            raise AttributeError(f"Feature '{self.name}' has no 'execute' function.")

        func = getattr(module, "execute")
        with self._state_lock:
            self.invocations += 1

        if inspect.iscoroutinefunction(func):
            return await func(**kwargs)
        else:
            return await asyncio.to_thread(func, **kwargs)


class DynamicToolRegistry:
    """Central registry for all hot-loaded features and synthetic Brahma skills."""

    _skills: Dict[str, DynamicSkill] = {}
    _initialized: bool = False
    _registry_lock = threading.RLock()

    @classmethod
    def get_skills_directory(cls) -> Path:
        FEATURES_DIR.mkdir(parents=True, exist_ok=True)
        return FEATURES_DIR

    @classmethod
    def initialize(cls) -> int:
        with cls._registry_lock:
            return cls._initialize_locked()

    @classmethod
    def _initialize_locked(cls) -> int:
        """Loads all valid features and skills from the codebase features/ directory and AppData vault."""
        cls._skills.clear()
        FEATURES_DIR.mkdir(parents=True, exist_ok=True)
        count = 0

        # 1. Load native features from codebase features/ directory
        for item in sorted(FEATURES_DIR.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0):
            if item.name.startswith((".", "_")):
                continue
            # The repository feature tree is trusted code. Do not follow a
            # symlink placed there to execute code from an unrelated location.
            if _is_link_like(item):
                logger.warning("[Registry] Refusing symlinked/junction/reparse feature entry '%s'.", item.name)
                continue

            # Case A: Single Python module file (e.g. features/internet_speed_test.py).
            # When a matching packaged skill directory also exists, the package
            # is authoritative so the same capability is never registered twice.
            if item.is_file() and item.suffix == ".py":
                packaged_dir = item.with_suffix("")
                if (packaged_dir / "manifest.json").is_file() and (packaged_dir / "skill.py").is_file():
                    continue
                try:
                    source = item.read_text(encoding="utf-8")
                    tree = ast.parse(source, filename=str(item))
                    meta: Dict[str, Any] = {}
                    for node in tree.body:
                        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
                        if any(isinstance(target, ast.Name) and target.id == "FEATURE_METADATA" for target in targets):
                            value = node.value
                            parsed = ast.literal_eval(value)
                            if isinstance(parsed, dict):
                                meta = parsed
                            break
                    if not meta:
                        meta = {
                            "name": item.stem,
                            "description": ast.get_docstring(tree) or f"Native feature {item.stem}",
                            "parameters": {"type": "OBJECT", "properties": {}},
                        }

                    manifest_path = item.with_suffix("") / "manifest.json"
                    if manifest_path.exists():
                        if _is_link_like(manifest_path):
                            logger.warning("[Registry] Refusing symlinked native feature manifest '%s'.", item.name)
                            continue
                        with open(manifest_path, "r", encoding="utf-8") as f:
                            package_manifest = json.load(f)
                        meta = {**meta, **package_manifest}

                    feat_name = meta.get("name", item.stem)
                    meta.setdefault("created_at", item.stat().st_mtime)
                    skill = DynamicSkill(item, meta)
                    cls._skills[feat_name] = skill
                    for alias in meta.get("aliases", []):
                        if alias not in cls._skills:
                            cls._skills[alias] = skill
                    count += 1
                except Exception as e:
                    logger.warning(f"[Registry] Failed to load codebase feature '{item.name}': {e}")

            # Case B: Feature directory with manifest.json & skill.py
            elif item.is_dir():
                manifest_file = item / "manifest.json"
                code_file = item / "skill.py"
                if manifest_file.exists() and code_file.exists():
                    if _is_link_like(manifest_file) or _is_link_like(code_file) or _is_link_like(item):
                        logger.warning("[Registry] Refusing symlinked/junction/reparse feature package '%s'.", item.name)
                        continue
                    try:
                        with open(manifest_file, "r", encoding="utf-8") as f:
                            manifest = json.load(f)
                        skill = DynamicSkill(item, manifest)
                        cls._skills[skill.name] = skill
                        for alias in skill.aliases:
                            if alias not in cls._skills:
                                cls._skills[str(alias)] = skill
                        count += 1
                    except Exception as e:
                        logger.warning(f"[Registry] Failed to load feature package '{item.name}': {e}")

        # 2. Check legacy AppData skills vault for backward compatibility.
        # User-generated code is treated as untrusted on every reload: keep it
        # inside the vault and re-run Crucible validation before importing it.
        if APPDATA_SKILLS_DIR.exists():
            raw_vault = APPDATA_SKILLS_DIR.absolute()
            vault_root = APPDATA_SKILLS_DIR.resolve()
            if os.path.normcase(os.path.normpath(str(vault_root))) != os.path.normcase(os.path.normpath(str(raw_vault))):
                logger.warning("[Registry] Refusing a skill vault reached through a reparse/symlink path.")
                cls._initialized = True
                return count
            for item in APPDATA_SKILLS_DIR.iterdir():
                if not item.is_dir() or item.name in cls._skills:
                    continue
                try:
                    skill_root = item.resolve()
                    skill_root.relative_to(vault_root)
                    manifest_file = (skill_root / "manifest.json").resolve()
                    code_file = (skill_root / "skill.py").resolve()
                    manifest_file.relative_to(skill_root)
                    code_file.relative_to(skill_root)
                    if not manifest_file.is_file() or not code_file.is_file():
                        continue
                    source = code_file.read_text(encoding="utf-8")
                    safe, reason = SkillCrucible.validate_ast(source)
                    if not safe:
                        logger.warning(
                            "[Registry] Refusing unsafe persisted skill '%s': %s",
                            item.name,
                            _redact_text(reason),
                        )
                        continue
                    with manifest_file.open("r", encoding="utf-8") as f:
                        manifest = json.load(f)
                    skill = DynamicSkill(skill_root, manifest, untrusted=True)
                    cls._skills[skill.name] = skill
                    for alias in skill.aliases:
                        if alias not in cls._skills:
                            cls._skills[str(alias)] = skill
                    count += 1
                except Exception as exc:
                    logger.warning(
                        "[Registry] Refusing persisted skill '%s': %s",
                        item.name,
                        _redact_text(exc),
                    )

        cls._initialized = True
        logger.info(f"[Registry] Initialized with {count} features and skills.")
        return count

    @classmethod
    def get_tool_declarations(cls) -> List[Dict[str, Any]]:
        """Returns one Gemini-compatible declaration per active skill, not per alias."""
        if not cls._initialized:
            cls.initialize()
        with cls._registry_lock:
            skill_values = list(cls._skills.values())
        declarations: list[Dict[str, Any]] = []
        seen: set[int] = set()
        for skill in skill_values:
            if not skill.active or id(skill) in seen:
                continue
            seen.add(id(skill))
            declarations.append(skill.to_tool_declaration())
        return declarations

    @classmethod
    def has_tool(cls, name: str) -> bool:
        if not cls._initialized:
            cls.initialize()
        with cls._registry_lock:
            skill = cls._skills.get(name)
            return bool(skill and skill.active)

    @classmethod
    def get_skill(cls, name: str) -> Optional[DynamicSkill]:
        if not cls._initialized:
            cls.initialize()
        with cls._registry_lock:
            return cls._skills.get(name)

    @classmethod
    def get_latest_skill(cls) -> Optional[DynamicSkill]:
        """Returns the most recently created active synthetic skill."""
        if not cls._initialized:
            cls.initialize()
        with cls._registry_lock:
            active = [s for s in cls._skills.values() if s.active]
        if not active:
            return None
        active.sort(key=lambda s: getattr(s, "created_at", 0), reverse=True)
        return active[0]

    @classmethod
    def find_matching_skill(cls, text: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        """
        Intelligently matches a natural language user command or directive
        to an active synthetic skill in the registry using semantic token matching
        and dynamic metadata-driven parameter extraction. Completely non-hardcoded.
        """
        if not text or not isinstance(text, str):
            return None
        if not cls._initialized:
            cls.initialize()

        q_lower = text.lower().strip()
        q_words = re.findall(r"[a-z0-9]+", q_lower)
        if not q_words:
            return None

        stopwords = {
            "a", "an", "the", "and", "or", "to", "for", "when", "that", "will", "you", "i", "me", "my",
            "on", "in", "at", "by", "from", "of", "with", "about", "is", "are", "was", "were", "be",
            "this", "that", "it", "can", "please", "do", "what", "how", "give", "brahma", "tell", "show",
            "run", "use", "start", "execute", "check", "get", "fetch", "try", "call", "launch",
            "skill", "skills", "feature", "features", "tool", "tools", "test", "tests", "testing",
            "made", "make", "using", "u", "ur", "your", "created", "create", "built", "build",
            "forged", "forge", "just", "recently", "newly", "new", "latest", "now", "already",
            "one", "did", "have"
        }

        def stem(w: str) -> str:
            w = w.lower()
            for sfx in ("ing", "ies", "es", "s"):
                if w.endswith(sfx) and len(w) > len(sfx) + 2:
                    return w[:-len(sfx)]
            return w

        def contains_term(query: str, term: object) -> bool:
            normalized = re.sub(r"\s+", " ", str(term or "").strip().lower().replace("_", " "))
            if not normalized:
                return False
            return re.search(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])", query) is not None

        q_meaningful = [w for w in q_words if w not in stopwords and len(w) > 2]
        q_stems = {stem(w) for w in q_meaningful}

        # 1. Unnamed skill trigger: ONLY when the user explicitly says 'use the skill', 'test new skill', etc.
        # WITHOUT specifying any specific domain topic (like 'bitcoin', 'cricket', 'internet')!
        generic_directives = {
            "use the skill", "run the skill", "test the skill", "execute the skill",
            "the skill", "use skill", "run skill", "test skill", "use latest skill",
            "run latest skill", "test latest skill", "use new skill", "run new skill",
            "use that skill", "run that skill", "test that skill", "execute that skill",
            "that skill", "this skill", "use this skill", "run this skill", "the feature",
            "use the feature", "run the feature", "launch the skill", "try the skill",
            "use the tool", "run the tool", "test the tool", "execute the tool"
        }
        cleaned_q = " ".join(q_words)
        is_generic_unnamed = (
            cleaned_q in generic_directives or
            (len(q_meaningful) == 0 and any(w in q_words for w in ("skill", "skills", "feature", "features", "tool", "tools"))) or
            (len(q_meaningful) == 0 and any(cleaned_q.startswith(g) for g in ("use", "run", "test", "execute", "start", "launch", "try")))
        )
        if is_generic_unnamed:
            latest = cls.get_latest_skill()
            if latest:
                return latest.name, {}

        # 2. Dynamic metadata-driven semantic skill matching across ALL registered skills
        best_skill = None
        best_score = 0

        with cls._registry_lock:
            skills = list(cls._skills.values())
        for skill in skills:
            if not skill.active:
                continue

            score = 0
            s_name_words = re.findall(r"[a-z0-9]+", skill.name.lower())
            s_name_stems = {stem(w) for w in s_name_words if w not in stopwords and len(w) > 2}

            s_desc_words = re.findall(r"[a-z0-9]+", skill.description.lower())
            s_desc_stems = {stem(w) for w in s_desc_words if w not in stopwords and len(w) > 2}

            # Explicit trigger phrases defined by the feature
            for trig in getattr(skill, "triggers", []):
                if contains_term(q_lower, trig):
                    score += 200
                    break

            # Explicit aliases defined by the feature
            for alias in getattr(skill, "aliases", []):
                if contains_term(q_lower, alias):
                    score += 180
                    break

            # Exact skill name match or clean underscore-replaced match
            if contains_term(q_lower, skill.name):
                score += 150

            # Direct phrase match in query (e.g. 'internet speed', 'speed test')
            for phrase_len in (3, 2):
                for i in range(len(q_words) - phrase_len + 1):
                    phrase = " ".join(q_words[i:i + phrase_len])
                    if contains_term(skill.name.replace("_", " ").lower(), phrase) and not all(pw in stopwords for pw in q_words[i:i + phrase_len]):
                        score += 60
                    elif contains_term(skill.description.lower(), phrase) and not all(pw in stopwords for pw in q_words[i:i + phrase_len]):
                        score += 25

            # Skill name stem overlap
            name_hits = s_name_stems.intersection(q_stems)
            score += len(name_hits) * 35
            if s_name_stems and name_hits == s_name_stems:
                score += 50

            # Description stem overlap (only meaningful stems!)
            desc_hits = s_desc_stems.intersection(q_stems)
            score += len(desc_hits) * 6

            # Acronym / token check for meaningful words only
            for word in q_meaningful:
                if len(word) >= 2 and word in skill.name.lower().split("_"):
                    score += 25

            if score > best_score and score >= 35:
                best_score = score
                best_skill = skill

        if not best_skill:
            return None

        # 3. Dynamic parameter extraction from skill manifest
        props = best_skill.parameters.get("properties", {})
        args: Dict[str, Any] = {}
        for p_name, p_spec in props.items():
            p_desc = str(p_spec.get("description", ""))
            p_type = str(p_spec.get("type", "STRING")).upper()

            # Location / Area extraction based on semantic description
            if any(w in p_desc.lower() for w in ("city", "district", "region", "area", "location")):
                for prep in ("around ", "in ", "near ", "over ", "for ", "at "):
                    if prep in q_lower:
                        cand = text[q_lower.find(prep) + len(prep):].strip()
                        cand = re.split(r"[,.?!;]|(?:and\s+)|(?:with\s+)", cand)[0].strip()
                        if cand:
                            args[p_name] = cand
                        break

            # Quoted options / tickers from description e.g. 'AAPL', 'BMW.DE', 'robot', 'floss'
            for opt in re.findall(r"'([a-zA-Z0-9_.-]+)'", p_desc):
                if opt.lower() in q_words:
                    args[p_name] = opt
                    break

            # Label to symbol mapping from description e.g. 'AAPL' for Apple
            examples = re.findall(r"'([A-Za-z0-9_.-]+)'\s+for\s+([A-Za-z0-9]+)", p_desc)
            for code, label in examples:
                if label.lower() in q_words:
                    args[p_name] = code
                    break

            # Number / duration extraction for numeric parameters
            if p_type in ("INTEGER", "NUMBER"):
                m_num = re.search(r"\b(\d+)\s*(?:seconds?|sec|mins?|points?|hours?|kline|bars?|times?)\b", q_lower)
                if m_num:
                    args[p_name] = int(m_num.group(1))

        return best_skill.name, args


    @classmethod
    def list_skills(cls) -> List[Dict[str, Any]]:
        """Returns metadata for all installed skills for UI presentation."""
        if not cls._initialized:
            cls.initialize()
        results = []
        seen = set()
        with cls._registry_lock:
            skills = list(cls._skills.values())
        for s in skills:
            if id(s) in seen:
                continue
            seen.add(id(s))
            results.append({
                "name": s.name,
                "description": s.description,
                "version": s.version,
                "active": s.active,
                "created_at": s.created_at,
                "invocations": s.invocations,
                "path": str(s.skill_dir),
                "last_error": s.last_error,
            })
        return results

    @classmethod
    def toggle_skill(cls, name: str, active: Optional[bool] = None) -> bool:
        """Enables or disables a persisted skill without mutating native repository files."""
        skill = cls.get_skill(name)
        if not skill or not skill.skill_path.is_dir():
            return False
        try:
            vault_root = APPDATA_SKILLS_DIR.resolve()
            raw_vault = APPDATA_SKILLS_DIR.absolute()
            if os.path.normcase(os.path.normpath(str(vault_root))) != os.path.normcase(os.path.normpath(str(raw_vault))):
                return False
            skill_root = skill.skill_path.resolve()
            skill_root.relative_to(vault_root)
            manifest_path = skill_root / "manifest.json"
            if not manifest_path.is_file() or manifest_path.is_symlink():
                return False
        except (OSError, ValueError):
            return False

        with cls._registry_lock:
            new_status = not skill.active if active is None else bool(active)
            updated_manifest = dict(skill.manifest)
            updated_manifest["active"] = new_status
            temp_path = manifest_path.with_name(
                f".{manifest_path.name}.{os.getpid()}-{uuid.uuid4().hex}.tmp"
            )
            try:
                temp_path.write_text(
                    json.dumps(updated_manifest, indent=4, ensure_ascii=False),
                    encoding="utf-8",
                )
                os.replace(temp_path, manifest_path)
            except Exception as e:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                logger.error(f"[Registry] Failed to save toggle status for '{name}': {_redact_text(e)}")
                return False
            skill.manifest = updated_manifest
            skill.active = new_status
            return True

    @classmethod
    def delete_skill(cls, name: str) -> bool:
        """Permanently removes only a persisted synthetic skill from the user skill vault."""
        skill = cls.get_skill(name)
        if not skill or not skill.skill_path.is_dir():
            return False

        try:
            vault_root = APPDATA_SKILLS_DIR.resolve()
            raw_vault = APPDATA_SKILLS_DIR.absolute()
            if os.path.normcase(os.path.normpath(str(vault_root))) != os.path.normcase(os.path.normpath(str(raw_vault))):
                return False
            skill_root = skill.skill_path.resolve()
            skill_root.relative_to(vault_root)
            if skill_root == vault_root or skill_root.is_symlink():
                return False
            shutil.rmtree(skill_root)
            with cls._registry_lock:
                if name in cls._skills:
                    del cls._skills[name]
            return True
        except Exception as e:
            logger.error(f"[Registry] Failed to delete skill '{name}': {_redact_text(e)}")
            return False

    @classmethod
    def execute_sync(cls, name: str, args: Dict[str, Any]) -> Any:
        """Dispatches execution synchronously (safe for background worker threads)."""
        skill = cls.get_skill(name)
        if not skill or not skill.active:
            return f"Error: Feature '{name}' is unavailable or deactivated."

        try:
            return skill.execute_sync(**args)
        except Exception as e:
            safe_error = _redact_text(e)
            skill.last_error = safe_error
            logger.error(f"[Registry] Execution error in feature '{name}': {safe_error}")
            try:
                import traceback
                from actions.auto_heal_engine import AutoHealEngine
                AutoHealEngine.record_last_error(traceback.format_exc())
            except Exception:
                pass
            raise

    @classmethod
    async def execute_async(cls, name: str, args: Dict[str, Any]) -> Any:
        """Dispatches an async execution to a loaded synthetic skill."""
        skill = cls.get_skill(name)
        if not skill or not skill.active:
            return f"Error: Synthetic skill '{name}' is unavailable or deactivated."

        try:
            result = await skill.execute_async(**args)
            return result
        except Exception as e:
            safe_error = _redact_text(e)
            skill.last_error = safe_error
            logger.error(f"[Registry] Execution error in skill '{name}': {safe_error}")
            # Route to auto heal engine if available
            try:
                import traceback
                from actions.auto_heal_engine import AutoHealEngine
                AutoHealEngine.record_last_error(traceback.format_exc())
            except Exception:
                pass
            raise
