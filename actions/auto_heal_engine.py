"""
Auto-Heal & Self-Patching Engine for Brahma AI
Enables Brahma to detect its own bugs, tracebacks, and tool exceptions,
synthesize minimal surgical hotfixes, verify syntax in an isolated sandbox,
safely apply patches with atomic rollback guarantees, and record changelogs.
"""

from __future__ import annotations
from core.runtime_paths import API_CONFIG_PATH, CONFIG_DIR, PATCH_HISTORY_PATH, PATCH_BACKUPS_DIR
from core.efficiency_policy import EFFICIENCY_DIRECTIVE

import ast
import json
import logging
import os
import py_compile
import re
import shutil
import sys
import time
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("AutoHealEngine")

BASE_DIR = Path(__file__).resolve().parent.parent
PATCH_HISTORY_FILE = PATCH_HISTORY_PATH
BACKUPS_DIR = PATCH_BACKUPS_DIR


# Core files strictly protected from modification to prevent self-destruction
PROTECTED_CORE_FILES = {
    "boot_sentry.py",
    "auto_heal_engine.py",
    "setup.py",
    "requirements.txt",
    "version.txt",
    "install_wizard.py",
    # The crash-recovery layer must remain independently runnable; it cannot
    # safely patch its own supervisor/recovery mechanism at runtime.
    "process_supervisor.py",
    "crash_recovery.py",
}


# ── 1. Traceback Analyzer ───────────────────────────────────────────────────

class TracebackAnalyzer:
    """Parses tracebacks and pinpoints the responsible first-party codebase file and line."""

    @staticmethod
    def parse(tb_text: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "success": False,
            "target_file": None,
            "line_number": None,
            "function_name": None,
            "exception_type": None,
            "exception_message": None,
            "raw_traceback": tb_text,
        }

        if not tb_text:
            return result

        # Extract exception type and message from the last line
        lines = [line.strip() for line in tb_text.strip().splitlines() if line.strip()]
        if lines:
            last_line = lines[-1]
            if ":" in last_line:
                parts = last_line.split(":", 1)
                result["exception_type"] = parts[0].strip()
                result["exception_message"] = parts[1].strip()
            else:
                result["exception_type"] = last_line
                result["exception_message"] = ""

        # Match all File "path", line X, in func entries
        file_pattern = re.compile(r'File\s+["\']([^"\']+\.py)["\'],\s+line\s+(\d+)(?:,\s+in\s+([^\n\r]+))?', re.IGNORECASE)
        matches = file_pattern.findall(tb_text)

        # Iterate in reverse (innermost / latest frame first) to find first-party codebase file
        repo_root = BASE_DIR.resolve()
        for raw_path, line_str, func_name in reversed(matches):
            p = Path(raw_path)
            # Skip third-party packages or virtualenvs.
            if "site-packages" in raw_path.lower() or ".venv" in raw_path.lower() or "lib\\python" in raw_path.lower():
                continue

            # Traceback paths are untrusted input. Never allow an absolute path or
            # symlink to escape the first-party repository root.
            resolved = None
            candidates = []
            if p.is_absolute():
                candidates.append(p)
            else:
                candidates.append(repo_root / p)
                candidates.append(repo_root / p.name)

            for sub in ("actions", "core", "agent", "services"):
                candidates.append(repo_root / sub / p.name)

            for candidate in candidates:
                try:
                    candidate_resolved = candidate.resolve()
                    candidate_resolved.relative_to(repo_root)
                except (OSError, ValueError):
                    continue
                if candidate_resolved.is_file() and candidate_resolved.suffix.lower() == ".py":
                    resolved = candidate_resolved
                    break

            if resolved and resolved.exists():
                # Check immunity
                if resolved.name in PROTECTED_CORE_FILES:
                    logger.warning(f"[AutoHeal] File '{resolved.name}' is core-protected and cannot be patched.")
                    continue

                result["success"] = True
                result["target_file"] = str(resolved.resolve())
                result["line_number"] = int(line_str)
                result["function_name"] = func_name.strip() if func_name else None
                break

        return result


# ── 2. Safety Sandbox & Rollback Manager ────────────────────────────────────

class SafetySandbox:
    """Manages atomic backups, AST parsing, compilation tests, and instant rollback."""

    _history_lock = threading.RLock()

    @staticmethod
    def create_backup(file_path: Path) -> Path:
        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
        # Use nanosecond time plus entropy so rapid consecutive repairs to the
        # same file can never overwrite each other's rollback backup.
        stamp = f"{time.time_ns()}_{uuid.uuid4().hex[:12]}"
        backup_name = f"{file_path.stem}.bak_{stamp}{file_path.suffix}"
        backup_path = BACKUPS_DIR / backup_name
        temp_path = BACKUPS_DIR / f".{backup_name}.tmp"
        try:
            shutil.copy2(file_path, temp_path)
            os.replace(temp_path, backup_path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return backup_path

    @staticmethod
    def _restore_backup_atomically(backup: Path, target: Path) -> None:
        temp = target.with_name(
            f".{target.name}.restore-{os.getpid()}-{uuid.uuid4().hex}.tmp"
        )
        try:
            shutil.copy2(backup, temp)
            os.replace(temp, target)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def validate_code(code_str: str, file_name: str = "<staging>") -> Tuple[bool, Optional[str]]:
        """Validates that candidate code parses into a valid Python AST without syntax errors."""
        try:
            ast.parse(code_str, filename=file_name)
            return True, None
        except SyntaxError as e:
            return False, f"Syntax Error on line {e.lineno}: {e.msg}"
        except Exception as e:
            return False, f"Validation Error: {e}"

    @staticmethod
    def rollback_patch(patch_id: str) -> Dict[str, Any]:
        """Rolls back an applied patch by its ID."""
        with SafetySandbox._history_lock:
            history = SafetySandbox._load_history()
            for entry in reversed(history):
                if entry.get("patch_id") == patch_id or patch_id == "latest":
                    if entry.get("status") != "applied":
                        continue
                    target = Path(entry.get("target_file", ""))
                    backup = Path(entry.get("backup_path", ""))
                    try:
                        target = target.resolve()
                        backup = backup.resolve()
                        target.relative_to(BASE_DIR.resolve())
                        backup.relative_to(BACKUPS_DIR.resolve())
                    except (OSError, ValueError):
                        return {"success": False, "message": "Rollback paths are outside the protected auto-heal roots."}
                    if not backup.is_file() or not target.is_file():
                        return {"success": False, "message": f"Backup file '{backup}' missing."}

                    try:
                        SafetySandbox._restore_backup_atomically(backup, target)
                        entry["status"] = "rolled_back"
                        entry["rolled_back_at"] = time.time()
                        SafetySandbox._save_history(history)
                        return {
                            "success": True,
                            "message": f"Successfully rolled back patch {entry.get('patch_id')} on '{target.name}'.",
                            "target_file": str(target),
                        }
                    except Exception as e:
                        return {"success": False, "message": f"Rollback failed: {e}"}

            return {"success": False, "message": f"No active patch matching '{patch_id}' found to rollback."}

    @staticmethod
    def _load_history() -> List[Dict[str, Any]]:
        if not PATCH_HISTORY_FILE.exists():
            return []
        try:
            with open(PATCH_HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    @staticmethod
    def _save_history(history: List[Dict[str, Any]]) -> None:
        try:
            with SafetySandbox._history_lock:
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                temp = PATCH_HISTORY_FILE.with_name(
                    f".{PATCH_HISTORY_FILE.name}.{os.getpid()}-{uuid.uuid4().hex}.tmp"
                )
                try:
                    temp.write_text(
                        json.dumps(history, indent=4, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    os.replace(temp, PATCH_HISTORY_FILE)
                finally:
                    try:
                        temp.unlink(missing_ok=True)
                    except OSError:
                        pass
        except Exception as e:
            logger.error(f"[AutoHeal] Failed to save patch history: {e}")


# ── 3. Patch Synthesizer & Auto-Heal Controller ─────────────────────────────

class AutoHealEngine:
    """Orchestrates error analysis, hotfix synthesis, verification, and application."""

    _last_error: Optional[str] = None

    @classmethod
    def record_last_error(cls, tb_str: str) -> None:
        """Stores the most recent error traceback captured during runtime."""
        cls._last_error = tb_str

    @classmethod
    def get_last_error(cls) -> Optional[str]:
        """Returns the most recent error traceback if any."""
        return cls._last_error

    @classmethod
    def get_patch_history(cls, limit: int = 5) -> List[Dict[str, Any]]:
        """Returns recent patch history."""
        history = SafetySandbox._load_history()
        return list(reversed(history))[:limit]

    @classmethod
    def heal_traceback(
        cls,
        traceback_text: str,
        context_notes: str = "",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Analyzes a traceback, pinpoints the root cause, synthesizes a patch,
        verifies AST syntax in memory, and safely applies it with backup.
        """
        parsed = TracebackAnalyzer.parse(traceback_text)
        if not parsed.get("success"):
            return {
                "success": False,
                "message": "Could not identify a modifiable first-party source file from the traceback.",
                "parsed": parsed,
            }

        target_file_str = parsed["target_file"]
        target_path = Path(target_file_str)
        line_num = parsed["line_number"]

        try:
            with open(target_path, "r", encoding="utf-8") as f:
                full_source = f.read()
        except Exception as e:
            return {"success": False, "message": f"Unable to read target file '{target_path.name}': {e}"}

        # Extract code context around the failing line
        source_lines = full_source.splitlines(keepends=True)
        start_idx = max(0, line_num - 25)
        end_idx = min(len(source_lines), line_num + 25)
        code_context = "".join(source_lines[start_idx:end_idx])

        # Generate patch via LLM
        patch_spec = cls._synthesize_patch_code(
            file_name=target_path.name,
            line_num=line_num,
            exception_type=parsed.get("exception_type", "Error"),
            exception_msg=parsed.get("exception_message", ""),
            code_context=code_context,
            context_notes=context_notes,
        )

        if not patch_spec.get("success"):
            return {
                "success": False,
                "message": f"Failed to synthesize patch: {patch_spec.get('error')}",
                "parsed": parsed,
            }

        target_chunk = patch_spec["target_chunk"]
        replacement_chunk = patch_spec["replacement_chunk"]
        explanation = patch_spec.get("explanation", "Bug hotfix.")

        if target_chunk not in full_source:
            return {
                "success": False,
                "message": "Target code chunk could not be matched precisely in source file.",
                "parsed": parsed,
            }

        # Apply candidate patch in staging memory
        staged_source = full_source.replace(target_chunk, replacement_chunk, 1)

        # Pre-flight AST Syntax validation
        valid, syntax_err = SafetySandbox.validate_code(staged_source, file_name=target_path.name)
        if not valid:
            logger.error(f"[AutoHeal] Patch rejected by SafetySandbox: {syntax_err}")
            return {
                "success": False,
                "message": f"Safety Guard: Patch rejected due to syntax error: {syntax_err}",
                "parsed": parsed,
            }

        if dry_run:
            return {
                "success": True,
                "dry_run": True,
                "target_file": str(target_path),
                "explanation": explanation,
                "target_chunk": target_chunk,
                "replacement_chunk": replacement_chunk,
                "message": f"Dry-run passed syntax validation for {target_path.name}.",
            }

        # Create atomic backup
        backup_path = SafetySandbox.create_backup(target_path)

        # Publish the verified source atomically so a process crash cannot leave
        # a partially rewritten target before rollback history is durable.
        target_tmp = target_path.with_name(
            f".{target_path.name}.autopatch-{os.getpid()}-{uuid.uuid4().hex}.tmp"
        )
        try:
            target_tmp.write_text(staged_source, encoding="utf-8")
            os.replace(target_tmp, target_path)
        except Exception as e:
            return {"success": False, "message": f"Atomic file replacement failed; original source preserved: {e}"}
        finally:
            try:
                target_tmp.unlink(missing_ok=True)
            except OSError:
                pass

        # Verify on-disk compilation via py_compile
        try:
            py_compile.compile(str(target_path), doraise=True)
        except Exception as pyc_err:
            logger.error(f"[AutoHeal] py_compile failed after write, rolling back: {pyc_err}")
            try:
                SafetySandbox._restore_backup_atomically(backup_path, target_path)
            except Exception as restore_err:
                return {"success": False, "message": f"Post-write compilation failed and rollback failed: {restore_err}"}
            return {"success": False, "message": f"Post-write compilation failed, rolled back: {pyc_err}"}

        patch_id = str(uuid.uuid4())[:8]
        entry = {
            "patch_id": patch_id,
            "timestamp": time.time(),
            "target_file": str(target_path),
            "backup_path": str(backup_path),
            "line_number": line_num,
            "exception_fixed": f"{parsed.get('exception_type')}: {parsed.get('exception_message')}",
            "explanation": explanation,
            "status": "applied",
        }

        with SafetySandbox._history_lock:
            history = SafetySandbox._load_history()
            history.append(entry)
            SafetySandbox._save_history(history)

        # Publish only after all in-process verification and history persistence
        # succeed. Self-coding checkpoints remain separate and approval-gated.
        publication = {"published": False, "reason": "not_attempted"}
        try:
            from core.repository_sync import publish_verified_repair
            try:
                relative_target = str(target_path.relative_to(BASE_DIR))
            except ValueError:
                relative_target = None
            publication = publish_verified_repair(
                target_path,
                patch_id,
                explanation=explanation,
                repository_relative_path=relative_target,
            )
            logger.info("[AutoHeal] Repository publication: %s", publication)
        except Exception as publish_err:
            logger.warning("[AutoHeal] Repository publication skipped: %s", publish_err)
        return {
            "success": True,
            "patch_id": patch_id,
            "target_file": str(target_path),
            "backup_path": str(backup_path),
            "explanation": explanation,
            "repository_publication": publication,
            "message": f"Successfully auto-patched '{target_path.name}' at line {line_num} (Patch ID: {patch_id}). Backup preserved.",
        }

    @classmethod
    def _synthesize_patch_code(
        cls,
        file_name: str,
        line_num: int,
        exception_type: str,
        exception_msg: str,
        code_context: str,
        context_notes: str = "",
    ) -> Dict[str, Any]:
        """Calls LLM to generate the surgical exact target chunk and replacement chunk."""
        prompt = f"""You are an elite Python compiler and autonomous debugging engineer.
A Python bug occurred in file '{file_name}' around line {line_num}.
Exception: {exception_type}: {exception_msg}
Additional context: {context_notes}

Relevant source code context:
```python
{code_context}
```

Task: Provide a surgical, minimal fix to eliminate the exception (e.g. add None-checks, handle key errors, safe type casting, boundary check).
EFFICIENCY-FIRST ENGINEERING POLICY:
- Measure or inspect the expensive step before optimizing it; do not optimize by guesswork.
- Reuse work when the relevant inputs are unchanged. Prefer exact content/configuration hashes,
  manifests, or other deterministic cache keys.
- Validate cache hits before trusting them. Stale, partial, incompatible, or corrupted caches
  must be rejected and rebuilt rather than silently used.
- Prefer incremental verification: run the smallest authoritative checks for a local change,
  then retain broader release gates when they protect system integrity.
- Do not repeat the same scan, archive traversal, compilation, or verification twice in one path
  unless the second pass has a distinct correctness purpose.
- Stream large files and archives instead of loading whole payloads into memory.
- Bound retries, polling, waits, and recovery loops while preserving diagnostics.
- Keep expensive optional work lazy and off the hot path until it is actually needed.
- Never cache API keys, tokens, credentials, private data, browser profiles, or user secrets.
- Keep cache invalidation conservative and tied to every input that can affect correctness.
- Optimize total user-visible latency and resource usage, not merely one local step.
- Never trade away correctness, security, safety, voice quality, feature behavior, or verification
  coverage merely to make a task faster.
- When performance changes materially, add a regression test or measurable guard proving equivalence.
- For autonomous repairs, make the smallest safe change first and reuse valid evidence; rerun
  affected verification when that evidence is invalidated.
Output ONLY a strict JSON object with these exact keys:
{{
    "explanation": "One sentence explaining what was fixed",
    "target_chunk": "Exact verbatim string from code_context to replace (must match characters and whitespace exactly)",
    "replacement_chunk": "Replacement code to substitute in place of target_chunk"
}}
Do NOT include markdown fences outside the JSON. Return only the valid JSON object.
"""
        # 1. Primary: the same unified cloud route used by normal Brahma
        # conversation/tool execution. OmniRoute owns provider selection,
        # credential routing, retries, and model failover.
        try:
            from llm_client import client as unified_client
            resp_text = unified_client.chat(
                prompt,
                system="You are an expert Python auto-patching engineer. Return strict JSON.",
                model="auto",
                max_tokens=4096,
                temperature=0.1,
            )
            clean_json = resp_text.strip()
            if clean_json.startswith("```"):
                clean_json = re.sub(r"^```[a-zA-Z]*\\n?", "", clean_json)
                clean_json = re.sub(r"\\n?```$", "", clean_json).strip()
            data = json.loads(clean_json)
            if "target_chunk" in data and "replacement_chunk" in data:
                data["success"] = True
                return data
        except Exception as u_err:
            logger.warning(f"[AutoHeal] Unified AI/OmniRoute synthesis failed: {u_err}")

        # 2. Direct OpenRouter fallback preserves recovery when OmniRoute is
        # unavailable or its embedded runtime is not healthy.
        try:
            import or_client
            resp_text = or_client.chat(
                prompt,
                system="You are an expert Python auto-patching engineer. Return strict JSON.",
                model="auto",
                max_tokens=4096,
                temperature=0.1,
            )
            clean_json = re.sub(r"^```[a-zA-Z]*\\n?", "", resp_text.strip())
            clean_json = re.sub(r"\\n?```$", "", clean_json).strip()
            data = json.loads(clean_json)
            if "target_chunk" in data and "replacement_chunk" in data:
                data["success"] = True
                return data
        except Exception as or_err:
            logger.warning(f"[AutoHeal] OpenRouter fallback failed: {or_err}")

        # OmniRoute/unified cloud plus its bounded provider fallback is the complete
        # recovery path. Do not create a second direct Gemini client here; that would
        # bypass the canonical provider and credential-routing contract.
        return {
            "success": False,
            "error": "All unified AI synthesis backends failed. Check OmniRoute/provider connectivity."
        }


# ── 4. Unified MCP Tool Dispatcher ──────────────────────────────────────────

def auto_heal(
    parameters: Optional[Union[Dict[str, Any], str]] = None,
    player: Any = None,
    speak: Optional[Callable[[str], None]] = None,
) -> str:
    """
    Unified entrypoint for Autonomous Self-Healing and Self-Improvement.
    """
    if isinstance(parameters, str):
        params = {"action": parameters}
    else:
        params = parameters or {}
    action = (params.get("action") or params.get("command") or "status").lower().strip()

    if action in ("history", "log", "patches"):
        patches = AutoHealEngine.get_patch_history(limit=5)
        if not patches:
            msg = "No automatic patches applied yet. System is running clean."
            if speak:
                speak(msg)
            return msg

        lines = ["🛡️ AUTONOMOUS PATCH HISTORY:"]
        for p in patches:
            fn = Path(p.get("target_file", "")).name
            status = p.get("status", "unknown")
            lines.append(f"• [{p.get('patch_id')}] {fn} (line {p.get('line_number')}): {p.get('explanation')} — Status: {status}")

        result = "\n".join(lines)
        if speak:
            speak(f"You have {len(patches)} recent patches logged. Last patch was on {Path(patches[0].get('target_file', '')).name}.")
        return result

    elif action in ("rollback", "undo", "revert"):
        patch_id = params.get("patch_id") or "latest"
        res = SafetySandbox.rollback_patch(patch_id)
        msg = res.get("message", "Rollback completed.")
        if speak:
            speak(msg)
        return msg

    elif action in ("heal", "fix", "patch"):
        tb = params.get("traceback") or params.get("error") or params.get("error_traceback") or ""
        notes = params.get("notes") or params.get("context") or ""
        if not tb:
            tb = AutoHealEngine.get_last_error() or ""
        if not tb:
            # Check FATAL_CRASH.log if no traceback explicitly provided
            crash_log = BASE_DIR / "FATAL_CRASH.log"
            if crash_log.exists():
                try:
                    tb = crash_log.read_text(encoding="utf-8")
                except Exception:
                    pass
        if not tb:
            msg = "No recent error or traceback captured to heal. If an error just occurred, you can paste the traceback."
            if speak:
                speak(msg)
            return msg

        res = AutoHealEngine.heal_traceback(tb, context_notes=notes)
        msg = res.get("message", "Done.")
        if speak:
            speak(msg)
        return msg

    elif action in ("learn_rule", "add_rule", "remember_rule"):
        rule = params.get("rule") or params.get("directive") or params.get("text") or ""
        if not rule:
            return "Please specify a rule to learn (e.g. rule='Always use Chrome browser')."
        from core.learned_rules import LearnedRulesEngine
        res = LearnedRulesEngine.add_rule(rule)
        msg = res.get("message", "Rule saved.")
        if speak:
            speak(f"Understood, sir. I have committed that rule to my memory.")
        return msg

    elif action in ("list_rules", "rules"):
        from core.learned_rules import LearnedRulesEngine
        rules = LearnedRulesEngine.list_rules()
        if not rules:
            return "No learned rules stored."
        lines = ["🧠 LEARNED BEHAVIORAL DIRECTIVES:"]
        for r in rules:
            status = "ACTIVE" if r.get("active") else "INACTIVE"
            lines.append(f"• [{r.get('id')}] ({status}) {r.get('rule')}")
        return "\n".join(lines)

    else:  # status
        patches = AutoHealEngine.get_patch_history(limit=1)
        last_patch = patches[0] if patches else None
        from core.learned_rules import LearnedRulesEngine
        rule_count = len(LearnedRulesEngine.list_rules(active_only=True))

        lines = [
            "🛡️ AUTO-HEAL & SELF-IMPROVEMENT STATUS",
            "─────────────────────────────────────",
            "• Boot Sentry: Active (Automatic rollback enabled)",
            "• Safety Sandbox: Enabled (Pre-flight AST & Compilation validation)",
            f"• Learned Behavioral Directives: {rule_count} active rules",
            f"• Recent Hotfixes: {last_patch.get('explanation') if last_patch else 'None (Clean)'}",
        ]
        report = "\n".join(lines)
        if speak:
            speak("Auto-heal sentry and continuous self-improvement are fully active, sir.")
        return report
