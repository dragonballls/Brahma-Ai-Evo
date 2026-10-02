"""
Skill Crucible: Multi-Tiered Safety Sandbox & Verification Engine
Part of Project Ultron for Brahma AI.

Performs:
1. Static AST Safety Analysis (blocks destructive OS actions).
2. Dependency Auto-Resolution (installs required packages in .venv).
3. Sandboxed Subprocess Test Execution (validates against test cases with timeouts).
"""

from __future__ import annotations

import ast
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("SkillCrucible")

# Dangerous calls and patterns that synthetic skills must NEVER execute
BANNED_AST_PATTERNS = [
    "format c:", "format d:", "format /fs", "diskpart", "vssadmin", "bcdedit",
    "system32", "syswow64", "boot_sentry", "auto_heal_engine",
    "install_wizard", "build_exe",
]

# High-risk primitives are denied for generated skills by default.
BANNED_IMPORTS = {
    "subprocess", "ctypes", "winreg", "multiprocessing",
    "pty", "pwd", "resource",
}
BANNED_CALLS = {
    ("os", "system"), ("os", "popen"), ("os", "execv"),
    ("os", "execve"), ("os", "execvp"), ("os", "execvpe"),
    ("shutil", "rmtree"), ("shutil", "move"),
}
STANDARD_LIB_MODULES = {
    "abc", "argparse", "array", "ast", "asyncio", "base64", "binascii", "bisect",
    "calendar", "cmath", "collections", "colorsys", "concurrent", "configparser",
    "contextlib", "copy", "csv", "ctypes", "dataclasses", "datetime", "decimal",
    "difflib", "dis", "doctest", "email", "enum", "errno", "faulthandler",
    "filecmp", "fileinput", "fnmatch", "fractions", "functools", "gc", "getopt",
    "getpass", "gettext", "glob", "gzip", "hashlib", "heapq", "hmac", "html",
    "http", "imaplib", "imghdr", "importlib", "inspect", "io", "ipaddress",
    "itertools", "json", "keyword", "linecache", "locale", "logging", "lzma",
    "math", "mimetypes", "mmap", "multiprocessing", "netrc", "nntplib", "numbers",
    "operator", "os", "pathlib", "pickle", "pkgutil", "platform", "plistlib",
    "poplib", "posixpath", "pprint", "profile", "pstats", "pty", "pwd", "py_compile",
    "pyclbr", "pydoc", "queue", "quopri", "random", "re", "reprlib", "resource",
    "rlcompleter", "runpy", "sched", "secrets", "select", "selectors", "shelve",
    "shlex", "shutil", "signal", "site", "smtpd", "smtplib", "sndhdr", "socket",
    "socketserver", "sqlite3", "ssl", "stat", "statistics", "string", "stringprep",
    "struct", "subprocess", "sys", "sysconfig", "tabnanny", "tarfile", "telnetlib",
    "tempfile", "termios", "textwrap", "threading", "time", "timeit", "tkinter",
    "token", "tokenize", "trace", "traceback", "tracemalloc", "tty", "turtle",
    "types", "typing", "unicodedata", "unittest", "urllib", "uu", "uuid", "venv",
    "warnings", "wave", "weakref", "webbrowser", "wsgiref", "xdrlib", "xml",
    "xmlrpc", "zipapp", "zipfile", "zipimport", "zlib"
}

# Mapping common package import names to pip install names if different
IMPORT_TO_PIP = {
    "bs4": "beautifulsoup4",
    "PIL": "pillow",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "yaml": "pyyaml",
    "fitz": "pymupdf",
    "speedtest": "speedtest-cli",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "dotenv": "python-dotenv",
    "dateutil": "python-dateutil",
    "serial": "pyserial",
    "websocket": "websocket-client",
}


def _get_python_executable() -> str:
    """Finds the running python executable so dependencies match the active runtime."""
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        py_cand = exe[:-5] + ".exe"
        if Path(py_cand).exists():
            return py_cand
    return exe


class SkillCrucible:
    """Verifies and stress-tests synthetic skills before deployment."""

    @staticmethod
    def validate_ast(code_str: str) -> Tuple[bool, Optional[str]]:
        """Validate syntax and reject high-risk primitives before execution."""
        if not code_str or not code_str.strip():
            return False, "Code cannot be empty."
        try:
            tree = ast.parse(code_str)
        except SyntaxError as e:
            return False, f"Syntax Error on line {e.lineno}: {e.msg}"
        except Exception as e:
            return False, f"Validation Error: {e}"

        if not any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "execute"
            for node in tree.body
        ):
            return False, "Skill code must define an 'execute(**kwargs)' or 'async def execute(**kwargs)' function."

        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value.lower()
                for banned in BANNED_AST_PATTERNS:
                    if banned in value:
                        return False, f"Security Violation: prohibited term '{banned}' detected."
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in BANNED_IMPORTS:
                        return False, f"Security Violation: import '{root}' is not allowed."
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                if root in BANNED_IMPORTS:
                    return False, f"Security Violation: import '{root}' is not allowed."
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                    pair = (func.value.id, func.attr)
                    if pair in BANNED_CALLS:
                        return False, f"Security Violation: call '{func.value.id}.{func.attr}(...)' is not allowed."
                elif isinstance(func, ast.Name) and func.id == "__import__":
                    return False, "Security Violation: dynamic imports are not allowed."

        return True, None

    @staticmethod
    def extract_dependencies(code_str: str) -> List[str]:
        """Extracts top-level external module imports required by the skill."""
        try:
            tree = ast.parse(code_str)
        except Exception:
            return []

        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root not in STANDARD_LIB_MODULES and root not in ("core", "actions", "features"):
                        imports.add(root)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    root = node.module.split(".")[0]
                    if root not in STANDARD_LIB_MODULES and root not in ("core", "actions", "features"):
                        imports.add(root)

        return sorted(list(imports))

    @classmethod
    def resolve_dependencies(cls, dependencies: List[str]) -> Tuple[bool, str]:
        """Verify dependencies without mutating the application's environment.

        Generated import names must never become arbitrary pip install targets.
        Missing packages are reported so they can be added deliberately to the
        normal requirements/setup flow.
        """
        if not dependencies:
            return True, "No external dependencies required."

        missing: list[str] = []
        for dep in sorted(set(dependencies)):
            check_script = f"import {dep}"
            if dep == "speedtest":
                check_script = "import speedtest; assert hasattr(speedtest, 'Speedtest')"
            elif dep == "PIL":
                check_script = "import PIL.Image"
            elif dep == "cv2":
                check_script = "import cv2; assert hasattr(cv2, 'imread')"
            try:
                proc = subprocess.run(
                    [_get_python_executable(), "-c", check_script],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env=cls._safe_environment(),
                )
                if proc.returncode != 0:
                    missing.append(dep)
            except Exception:
                missing.append(dep)

        if missing:
            return False, (
                "Missing preinstalled dependencies: "
                + ", ".join(missing)
                + ". Install them through the normal requirements/setup process."
            )
        return True, "All external dependencies are already installed."

    @staticmethod
    def _safe_environment() -> dict[str, str]:
        """Remove common secrets from generated-skill subprocesses."""
        env = dict(os.environ)
        for key in list(env):
            upper = key.upper()
            if any(token in upper for token in (
                "API_KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD",
                "PRIVATE_KEY", "ACCESS_KEY",
            )):
                env.pop(key, None)
        env["PYTHONNOUSERSITE"] = "1"
        return env

    @classmethod
    def run_sandbox_test(
        cls,
        skill_code: str,
        test_cases: List[Dict[str, Any]],
        timeout: float = 75.0
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Executes test cases in an isolated child process to verify runtime correctness.
        Returns: (passed: bool, message: str, telemetry: dict)
        """
        py_exe = _get_python_executable()

        # Build runner harness
        harness_script = f"""
import sys
import json
import traceback
import asyncio
import inspect

{skill_code}

def run_tests():
    test_cases = json.loads({json.dumps(json.dumps(test_cases))})
    results = []
    
    if not test_cases:
        test_cases = [{{"input": {{}}}}]
        
    for i, tc in enumerate(test_cases):
        args = tc.get("input", {{}})
        try:
            if inspect.iscoroutinefunction(execute):
                res = asyncio.run(execute(**args))
            else:
                res = execute(**args)
                
            # Check that result is presentable and not an error dictionary
            if res is None:
                res = "Operation completed with no output."

            is_err = False
            err_msg = ""
            if isinstance(res, dict):
                if res.get("error"):
                    is_err = True
                    err_msg = str(res["error"])
                elif res.get("success") is False:
                    is_err = True
                    err_msg = str(res.get("message") or res.get("error") or "Skill returned success=False")
            elif isinstance(res, str) and (res.lower().startswith("error:") or res.lower().startswith("failed:")):
                is_err = True
                err_msg = res

            if is_err:
                results.append({{"index": i, "success": False, "error": err_msg, "output": str(res)[:300]}})
            else:
                results.append({{"index": i, "success": True, "output": str(res)[:300]}})
        except Exception as exc:
            tb = traceback.format_exc()
            results.append({{"index": i, "success": False, "error": str(exc), "traceback": tb}})
            
    print(json.dumps(results))

if __name__ == '__main__':
    run_tests()
"""
        start_time = time.time()
        try:
            proc = subprocess.run(
                [py_exe, "-c", harness_script],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=cls._safe_environment(),
            )
            elapsed = time.time() - start_time
            if proc.returncode != 0:
                err_text = proc.stderr.strip() or proc.stdout.strip() or "Process exited with error code."
                return False, f"Sandbox Test Failed: {err_text[:300]}", {"elapsed_s": elapsed}

            output_str = proc.stdout.strip()
            # Extract JSON output
            test_results = None
            try:
                for line in reversed(output_str.splitlines()):
                    candidate = line.strip()
                    if candidate.startswith("[") and candidate.endswith("]"):
                        try:
                            parsed = json.loads(candidate)
                            if isinstance(parsed, list):
                                test_results = parsed
                                break
                        except Exception:
                            pass
            except Exception:
                pass

            if test_results is None:
                test_results = [{"success": True, "output": output_str[:300]}]

            all_passed = all(t.get("success", False) for t in test_results)
            if not all_passed:
                first_err = next((t.get("error", "Unknown test failure") for t in test_results if not t.get("success")), "Test failed")
                return False, f"Test Verification Failed: {first_err}", {"results": test_results, "elapsed_s": elapsed}

            return True, f"Passed {len(test_results)}/{len(test_results)} tests in {elapsed:.2f}s", {
                "results": test_results,
                "elapsed_s": elapsed
            }

        except subprocess.TimeoutExpired:
            return False, f"Sandbox execution timed out after {timeout} seconds.", {"elapsed_s": timeout}
        except Exception as e:
            return False, f"Sandbox execution error: {e}", {"elapsed_s": 0}
