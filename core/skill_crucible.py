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
import tempfile
import re

logger = logging.getLogger("SkillCrucible")

_CREDENTIAL_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
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


def _sandbox_environment(root: Path) -> dict[str, str]:
    """Keep generated skills away from host credentials and user-scoped files."""
    blocked = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH", "PRIVATE", "COOKIE", "SESSION", "PIN")
    env = {
        key: value for key, value in os.environ.items()
        if not any(part in key.upper() for part in blocked)
    }
    root = root.resolve()
    env["BRAHMA_CRUCIBLE_ROOT"] = str(root)
    env["LOCALAPPDATA"] = str(root)
    env["APPDATA"] = str(root)
    env["HOME"] = str(root)
    env["USERPROFILE"] = str(root)
    env["TEMP"] = str(root)
    env["TMP"] = str(root)
    env["PYTHONNOUSERSITE"] = "1"
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE", "PYTHONSTARTUP", "PYTHONBREAKPOINT"):
        env.pop(key, None)
    return env


def _normalize_distribution_name(value: object) -> str:
    """Normalize a Python distribution name according to PyPI naming rules."""
    return re.sub(r"[-_.]+", "-", str(value or "").strip()).casefold()


def _approved_auto_install_packages() -> set[str]:
    """Return only distributions explicitly declared by Brahma's runtime."""
    requirements = Path(__file__).resolve().parents[1] / "requirements.txt"
    try:
        lines = requirements.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()

    approved: set[str] = set()
    for raw_line in lines:
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http://", "https://")):
            continue
        distribution = re.split(r"[<>=!~;@]", line, maxsplit=1)[0].split("[", 1)[0].strip()
        normalized = _normalize_distribution_name(distribution)
        if normalized:
            approved.add(normalized)
    return approved

# Dangerous calls and patterns that synthetic skills must NEVER execute
BANNED_AST_PATTERNS = [
    # Destructive disk / partition commands
    "format c:",
    "format d:",
    "format /fs",
    "diskpart",
    "vssadmin",
    "bcdedit",
    # System32 destruction
    "system32",
    "syswow64",
    # Self-destruction of Brahma AI core
    "boot_sentry",
    "auto_heal_engine",
    "install_wizard",
    "build_exe",
]

BANNED_IMPORT_MODULES = {
    "subprocess",
    "ctypes",
    "multiprocessing",
    "importlib",
    "runpy",
    "pty",
    "sqlite3",
    "mmap",
    # Native/C/device modules cannot be safely confined by Python-level Crucible hooks.
    "cffi", "numpy", "cv2", "mss", "psutil", "pyautogui", "pygetwindow", "pywinauto",
    "sounddevice", "pyaudio", "comtypes", "pycaw", "mediapipe", "send2trash",
    "inspect", "operator", "gc", "pydoc", "pkgutil",
    "playwright", "browser_harness", "PyQt6", "win10toast", "pocketsphinx",
    "webbrowser", "pickle", "marshal", "zipimport", "faulthandler", "shelve", "dbm",
    # Native Windows modules can open files/processes or query protected system state.
    "winreg",
    "_winreg",
    "win32api",
    "win32file",
    "win32security",
    "win32crypt",
    "win32cred",
    "win32process",
    "win32service",
    "win32event",
    "win32com",
    "pythoncom",
    "pywintypes",
    "msvcrt",
}

# Dotted-module/function paths that expose process-launch capabilities even when
# the top-level import is an otherwise safe standard-library module.
BANNED_IMPORT_PATHS = {
    "asyncio.subprocess",
}
BANNED_ASYNCIO_SUBPROCESS_NAMES = {
    "create_subprocess_exec",
    "create_subprocess_shell",
}

BANNED_CALLS = {
    ("os", "system"), ("os", "popen"), ("os", "remove"), ("os", "unlink"),
    ("os", "rmdir"), ("os", "removedirs"), ("os", "replace"), ("os", "rename"),
    ("os", "startfile"), ("os", "listdir"), ("os", "scandir"), ("os", "walk"),
    ("os", "fwalk"), ("os", "stat"), ("os", "lstat"), ("os", "access"),
    ("os", "readlink"), ("os", "spawnl"), ("os", "spawnle"), ("os", "spawnv"),
    ("os", "spawnve"), ("os", "posix_spawn"), ("os", "posix_spawnp"),
    ("os", "execl"), ("os", "execle"), ("os", "execlp"), ("os", "execv"),
    ("os", "execve"), ("os", "execvp"), ("os", "execvpe"), ("os", "_exit"),
    ("os", "abort"), ("os", "kill"), ("os", "killpg"),
    ("os", "chmod"), ("os", "chown"), ("os", "lchown"), ("os", "utime"),
    ("os", "truncate"), ("os", "ftruncate"), ("os", "chdir"), ("os", "fchdir"),
    ("os", "seteuid"), ("os", "setuid"), ("os", "setgid"), ("os", "setgroups"),
    ("os", "initgroups"),
    ("os.path", "realpath"), ("os.path", "abspath"), ("os.path", "exists"),
    ("os.path", "lexists"), ("os.path", "getsize"), ("os.path", "getmtime"),
    ("os.path", "getatime"), ("os.path", "getctime"), ("os.path", "getmode"),
    ("os.path", "samefile"),
    ("shutil", "rmtree"), ("shutil", "copytree"), ("shutil", "make_archive"),
    ("subprocess", "*"),
    ("pathlib.Path", "unlink"), ("pathlib.Path", "rmdir"), ("pathlib.Path", "replace"),
    ("pathlib.Path", "rename"), ("pathlib.Path", "iterdir"), ("pathlib.Path", "glob"),
    ("pathlib.Path", "rglob"), ("pathlib.Path", "walk"), ("pathlib.Path", "resolve"),
    ("pathlib.Path", "absolute"), ("pathlib.Path", "stat"), ("pathlib.Path", "lstat"),
    ("pathlib.Path", "exists"), ("pathlib.Path", "is_file"), ("pathlib.Path", "is_dir"),
    ("pathlib.Path", "is_symlink"), ("pathlib.Path", "readlink"),
    ("pathlib.Path", "owner"), ("pathlib.Path", "group"),
    ("pathlib.Path", "chmod"), ("pathlib.Path", "lchmod"), ("pathlib.Path", "touch"),
    ("concurrent.futures", "ProcessPoolExecutor"),
    ("webbrowser", "open"), ("webbrowser", "open_new"), ("webbrowser", "open_new_tab"),
    ("builtins", "__import__"), ("builtins", "eval"), ("builtins", "exec"),
    ("builtins", "compile"), ("sys", "_getframe"), ("sys", "_current_frames"),
    ("object", "__subclasses__"), ("type", "__subclasses__"),
    ("ssl", "_create_unverified_context"),
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
        """Parses syntax and blocks destructive patterns."""
        if not code_str or not code_str.strip():
            return False, "Code cannot be empty."

        try:
            tree = ast.parse(code_str)
        except SyntaxError as e:
            return False, f"Syntax Error on line {e.lineno}: {e.msg}"
        except Exception as e:
            return False, f"Validation Error: {e}"

        # Ensure it defines the mandatory execute function
        has_execute = False
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "execute":
                    has_execute = True
                    break

        if not has_execute:
            return False, "Skill code must define an 'execute(**kwargs)' or 'async def execute(**kwargs)' function."

        imported_dangerous_names: set[str] = set()
        module_aliases: dict[str, str] = {}
        symbol_aliases: dict[str, str] = {}
        sensitive_modules = {"os", "shutil", "ssl", "pathlib", "sys", "builtins"}
        banned_attributes = {
            ("sys", "modules"),
            ("sys", "_getframe"),
            ("sys", "_current_frames"),
            ("builtins", "__import__"),
            ("builtins", "eval"),
            ("builtins", "exec"),
            ("builtins", "compile"),
        }
        dangerous_dunder_attributes = {
            "__dict__", "__class__", "__base__", "__bases__", "__mro__", "__subclasses__",
            "__globals__", "__builtins__", "__code__", "__closure__", "__func__",
            "__self__", "__getattribute__", "__getattr__", "__setattr__", "__delattr__",
            "__reduce__", "__reduce_ex__", "f_globals", "f_builtins", "f_locals",
            "f_code", "gi_code", "gi_frame", "cr_code", "cr_frame",
        }

        dangerous_names = {
            "system", "popen", "remove", "unlink", "rmdir", "removedirs",
            "replace", "rename", "startfile", "_create_unverified_context",
            "rmtree", "copytree", "make_archive",
            "listdir", "scandir", "walk", "fwalk", "stat", "lstat", "access",
            "readlink", "realpath", "abspath", "exists", "lexists", "getsize",
            "getmtime", "getatime", "getctime", "getmode", "samefile",
            "symlink", "link", "mkdir", "makedirs", "chmod", "chown", "lchown",
            "utime", "truncate", "ftruncate", "chdir", "fchdir", "seteuid",
            "setuid", "setgid", "setgroups", "initgroups", "__import__", "eval",
            "exec", "compile", "_getframe", "_current_frames", "__subclasses__",
            "listdir", "scandir", "walk", "fwalk", "stat", "lstat", "access",
            "readlink", "realpath", "abspath", "exists", "lexists", "getsize",
            "getmtime", "getatime", "getctime", "getmode", "samefile",
            "symlink", "link", "mkdir", "makedirs", "chmod", "chown", "lchown",
            "utime", "truncate", "ftruncate", "spawnl", "spawnle", "spawnv",
            "spawnve", "posix_spawn", "posix_spawnp", "execl", "execle",
            "execlp", "execv", "execve", "execvp", "execvpe", "_exit", "abort",
            "kill", "killpg",
        }

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in sensitive_modules:
                        module_aliases[alias.asname or root] = root

            elif isinstance(node, ast.ImportFrom):
                module_root = (node.module or "").split(".")[0]
                if module_root in sensitive_modules:
                    for alias in node.names:
                        if alias.name == "*":
                            return False, f"Security Violation: wildcard import from '{module_root}' is prohibited."
                        symbol_aliases[alias.asname or alias.name] = f"{module_root}.{alias.name}"
                        if alias.name in dangerous_names:
                            imported_dangerous_names.add(alias.asname or alias.name)

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module_root = (node.module or "").split(".")[0]
                dangerous_names = {
                    "system", "popen", "remove", "unlink", "rmdir", "removedirs",
                    "replace", "rename", "startfile", "_create_unverified_context",
                    "rmtree", "copytree", "make_archive",
            "listdir", "scandir", "walk", "fwalk", "stat", "lstat", "access",
            "readlink", "realpath", "abspath", "exists", "lexists", "getsize",
            "getmtime", "getatime", "getctime", "getmode", "samefile",
            "symlink", "link", "mkdir", "makedirs", "chmod", "chown", "lchown",
            "utime", "truncate", "ftruncate", "chdir", "fchdir", "seteuid",
            "setuid", "setgid", "setgroups", "initgroups", "__import__", "eval",
            "exec", "compile", "_getframe", "_current_frames", "__subclasses__",
                    "listdir", "scandir", "walk", "fwalk", "stat", "lstat", "access",
                    "readlink", "realpath", "abspath", "exists", "lexists", "getsize",
                    "getmtime", "getatime", "getctime", "getmode", "samefile",
                    "symlink", "link", "mkdir", "makedirs", "chmod", "chown", "lchown",
                    "utime", "truncate", "ftruncate", "spawnl", "spawnle", "spawnv",
                    "spawnve", "posix_spawn", "posix_spawnp", "execl", "execle",
                    "execlp", "execv", "execve", "execvp", "execvpe", "_exit", "abort",
                    "kill", "killpg",
                }
                if module_root in {"os", "shutil", "ssl"}:
                    for alias in node.names:
                        if alias.name in dangerous_names:
                            imported_dangerous_names.add(alias.asname or alias.name)

        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                if node.attr in dangerous_dunder_attributes:
                    return False, f"Security Violation: prohibited attribute access '{node.attr}'."
                if isinstance(node.value, ast.Name):
                    owner = module_aliases.get(node.value.id, node.value.id)
                    if (owner, node.attr) in banned_attributes:
                        return False, f"Security Violation: prohibited attribute '{owner}.{node.attr}'."

        # Reject dangerous runtime/builtin names even when referenced indirectly,
        # such as assigning getattr/eval to an alias or indexing __builtins__.
        blocked_runtime_names = {
            "getattr", "eval", "exec", "__import__", "globals", "locals", "vars",
            "compile", "__builtins__", "__loader__", "__spec__",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in blocked_runtime_names:
                return False, f"Security Violation: prohibited runtime name '{node.id}'."

        # Block powerful runtime primitives that would let a generated skill
        # escape the Crucible's intended safety boundary. Skills can still use
        # ordinary Python, network clients, and deterministic local processing.
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in BANNED_IMPORT_PATHS:
                        return False, f"Security Violation: prohibited import '{alias.name}'."
                    root = alias.name.split(".")[0]
                    if root in BANNED_IMPORT_MODULES:
                        return False, f"Security Violation: prohibited import '{root}'."
            elif isinstance(node, ast.ImportFrom):
                module_name = node.module or ""
                root = module_name.split(".")[0]
                if root in BANNED_IMPORT_MODULES:
                    return False, f"Security Violation: prohibited import '{root}'."
                for alias in node.names:
                    imported_path = f"{module_name}.{alias.name}" if module_name else alias.name
                    if imported_path in BANNED_IMPORT_PATHS:
                        return False, f"Security Violation: prohibited import '{imported_path}'."
                    if module_name == "asyncio" and alias.name in BANNED_ASYNCIO_SUBPROCESS_NAMES:
                        return False, f"Security Violation: prohibited import '{module_name}.{alias.name}'."
                if node.module == "concurrent.futures":
                    for alias in node.names:
                        if alias.name == "ProcessPoolExecutor":
                            return False, "Security Violation: ProcessPoolExecutor is prohibited."
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "getattr":
                    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                        attr_name = str(node.args[1].value)
                        if (
                            attr_name in dangerous_dunder_attributes
                            or attr_name in dangerous_names
                            or attr_name.startswith("__")
                            or attr_name.endswith("__")
                        ):
                            return False, f"Security Violation: prohibited dynamic attribute access '{attr_name}'."
                    elif len(node.args) >= 2:
                        return False, "Security Violation: dynamic getattr targets are not permitted."
                if isinstance(func, ast.Name) and func.id in {
                    "eval", "exec", "__import__", "globals", "locals", "vars"
                }:
                    return False, f"Security Violation: prohibited dynamic execution '{func.id}'."
                if isinstance(func, ast.Name) and func.id in imported_dangerous_names:
                    return False, f"Security Violation: prohibited imported call '{func.id}'."
                if isinstance(func, ast.Call) and isinstance(func.func, ast.Name) and func.func.id == "getattr":
                    if len(func.args) >= 2 and isinstance(func.args[0], ast.Name) and isinstance(func.args[1], ast.Constant):
                        module_root = module_aliases.get(func.args[0].id)
                        symbol_root = symbol_aliases.get(func.args[0].id)
                        attr_name = str(func.args[1].value)
                        if module_root in sensitive_modules and attr_name in dangerous_names:
                            return False, f"Security Violation: prohibited dynamic access '{module_root}.{attr_name}'."
                        if symbol_root == "pathlib.Path" and attr_name in {
                            "unlink", "rmdir", "replace", "rename", "iterdir", "glob", "rglob",
                            "walk", "resolve", "absolute", "stat", "lstat", "exists", "is_file",
                            "is_dir", "is_symlink", "readlink", "owner", "group",
                        }:
                            return False, f"Security Violation: prohibited dynamic access 'pathlib.Path.{attr_name}'."
                if isinstance(func, ast.Attribute):
                    if func.attr in dangerous_dunder_attributes:
                        return False, f"Security Violation: prohibited attribute access '{func.attr}'."
                    def _expression_symbol(expr: ast.AST) -> str:
                        if isinstance(expr, ast.Name):
                            return symbol_aliases.get(
                                expr.id,
                                module_aliases.get(expr.id, expr.id),
                            )
                        if isinstance(expr, ast.Attribute):
                            base = _expression_symbol(expr.value)
                            return f"{base}.{expr.attr}" if base else expr.attr
                        if isinstance(expr, ast.Call):
                            return _expression_symbol(expr.func)
                        return ""

                    owner = _expression_symbol(func.value)
                    call_key = (owner, func.attr)
                    if call_key in BANNED_CALLS or (owner, "*") in BANNED_CALLS:
                        return False, f"Security Violation: prohibited call '{owner}.{func.attr}'."
                    if owner == "asyncio" and func.attr in BANNED_ASYNCIO_SUBPROCESS_NAMES:
                        return False, f"Security Violation: prohibited call 'asyncio.{func.attr}'."
                    if owner == "pathlib.Path" and func.attr in {"unlink", "rmdir", "replace", "rename"}:
                        return False, f"Security Violation: prohibited Path.{func.attr} call."
                for keyword in node.keywords:
                    if keyword.arg == "verify" and isinstance(keyword.value, ast.Constant):
                        if keyword.value.value is False:
                            return False, "Security Violation: TLS certificate verification cannot be disabled."

        # Reject common hard-coded credential formats before a generated skill can be persisted.
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if any(pattern.search(node.value) for pattern in _CREDENTIAL_PATTERNS):
                    return False, "Security Violation: hard-coded credential material is prohibited."

        # Safety scans for banned keywords in string literals or function calls
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                val_lower = node.value.lower()
                for banned in BANNED_AST_PATTERNS:
                    if banned in val_lower:
                        return False, f"Security Violation: Prohibited term or system path '{banned}' detected."

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
        """Installs missing dependencies into the Python environment."""
        if not dependencies:
            return True, "No external dependencies required."

        py_exe = _get_python_executable()
        installed_any = []

        approved_packages = _approved_auto_install_packages()
        for dep in dependencies:
            pip_name = IMPORT_TO_PIP.get(dep, dep)
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", str(pip_name or "")):
                return False, f"Rejected unsafe dependency name '{pip_name}'."
            if _normalize_distribution_name(pip_name) not in approved_packages:
                return False, f"Dependency '{pip_name}' is not an approved Brahma runtime package."
            # Check if importable and healthy
            is_healthy = False
            try:
                check_script = f"import {dep}"
                if dep == "speedtest":
                    check_script = "import speedtest; assert hasattr(speedtest, 'Speedtest')"
                elif dep == "PIL":
                    check_script = "import PIL.Image"
                elif dep == "cv2":
                    check_script = "import cv2; assert hasattr(cv2, 'imread')"
                check_cmd = [py_exe, "-P", "-c", check_script]
                with tempfile.TemporaryDirectory(prefix=".crucible-import-") as import_root:
                    proc = subprocess.run(
                        check_cmd,
                        capture_output=True,
                        timeout=5,
                        cwd=import_root,
                        env=_sandbox_environment(Path(import_root)),
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                if proc.returncode == 0:
                    is_healthy = True
            except Exception:
                is_healthy = False

            if is_healthy:
                continue

            logger.info(f"[Crucible] Installing missing or repairing approved dependency: {pip_name}")
            try:
                if dep == "speedtest":
                    subprocess.run(
                        [py_exe, "-m", "pip", "--isolated", "uninstall", "-y", "speedtest"],
                        capture_output=True,
                        timeout=30,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                install_cmd = [
                    py_exe, "-m", "pip", "--isolated", "install",
                    "--index-url", "https://pypi.org/simple",
                    "--disable-pip-version-check", "--no-input",
                    "--no-deps", "--only-binary=:all:",
                    pip_name, "--quiet",
                ]
                proc = subprocess.run(install_cmd, capture_output=True, text=True, timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if proc.returncode != 0:
                    err_sample = proc.stderr.strip()[:180] or "Unknown pip error"
                    return False, f"Failed to install dependency '{pip_name}': {err_sample}"
                installed_any.append(pip_name)
            except subprocess.TimeoutExpired:
                return False, f"Timeout installing '{pip_name}'."
            except Exception as e:
                return False, f"Exception installing '{pip_name}': {e}"

        msg = f"Installed: {', '.join(installed_any)}" if installed_any else "All dependencies satisfied."
        return True, msg

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
        safe, reason = cls.validate_ast(skill_code)
        if not safe:
            return False, f"Sandbox static validation failed: {_redact_text(reason)}", {"results": []}

        py_exe = _get_python_executable()

        # Build runner harness
        harness_script = f"""
import sys
import json
import traceback
import asyncio
import inspect
import os
import builtins as _builtins
import io as _io
from pathlib import Path as _SandboxPath
import shutil as _sandbox_shutil
import asyncio as _sandbox_asyncio

_SANDBOX_ROOT = _SandboxPath(os.environ["BRAHMA_CRUCIBLE_ROOT"]).resolve()
_REAL_OS_REALPATH = os.path.realpath
_REALPATH_RESOLVING = False

def _sandbox_path(value):
    global _REALPATH_RESOLVING
    if isinstance(value, (str, bytes, os.PathLike)):
        candidate = _SandboxPath(value)
        if not candidate.is_absolute():
            candidate = _SANDBOX_ROOT / candidate
        _REALPATH_RESOLVING = True
        try:
            realpath_value = _REAL_OS_REALPATH(os.fspath(candidate))
        finally:
            _REALPATH_RESOLVING = False
        resolved = _SandboxPath(realpath_value)
        try:
            resolved.relative_to(_SANDBOX_ROOT)
        except ValueError as exc:
            raise PermissionError("Crucible sandbox denied filesystem access outside its temporary root.") from exc
        return resolved
    if isinstance(value, int):
        raise PermissionError("Crucible sandbox denied direct file-descriptor access.")
    return value

_real_open = _builtins.open
_real_io_open = _io.open
_real_os_open = os.open
_real_os_stat = os.stat
_real_os_lstat = os.lstat
_real_os_access = os.access
_real_os_listdir = os.listdir
_real_os_scandir = os.scandir
_real_os_walk = os.walk
_real_os_fwalk = getattr(os, "fwalk", None)
_real_os_readlink = os.readlink
_real_os_remove = os.remove
_real_os_unlink = os.unlink
_real_os_rmdir = os.rmdir
_real_os_removedirs = os.removedirs
_real_os_replace = os.replace
_real_os_rename = os.rename
_real_os_mkdir = os.mkdir
_real_os_makedirs = os.makedirs
_real_os_symlink = os.symlink
_real_os_link = os.link
_real_os_chdir = os.chdir
_real_os_startfile = getattr(os, "startfile", None)

def _sandbox_open(file, *args, **kwargs):
    return _real_open(_sandbox_path(file), *args, **kwargs)

def _sandbox_io_open(file, *args, **kwargs):
    return _real_io_open(_sandbox_path(file), *args, **kwargs)

def _sandbox_os_open(path, flags, mode=0o777, *, dir_fd=None):
    if dir_fd is not None:
        raise PermissionError("Crucible sandbox denied dir_fd filesystem access.")
    return _real_os_open(_sandbox_path(path), flags, mode)

def _sandbox_stat(path, *args, **kwargs):
    if kwargs.get("dir_fd") is not None or (len(args) >= 2 and args[1] is not None):
        raise PermissionError("Crucible sandbox denied dir_fd filesystem access.")
    if _REALPATH_RESOLVING:
        return _real_os_stat(path, *args[:1], **{{k: v for k, v in kwargs.items() if k != "dir_fd"}})
    return _real_os_stat(_sandbox_path(path), *args[:1], **{{k: v for k, v in kwargs.items() if k != "dir_fd"}})

def _sandbox_lstat(path, *args, **kwargs):
    if kwargs.get("dir_fd") is not None or (len(args) >= 2 and args[1] is not None):
        raise PermissionError("Crucible sandbox denied dir_fd filesystem access.")
    if _REALPATH_RESOLVING:
        return _real_os_lstat(path, *args[:1], **{{k: v for k, v in kwargs.items() if k != "dir_fd"}})
    return _real_os_lstat(_sandbox_path(path), *args[:1], **{{k: v for k, v in kwargs.items() if k != "dir_fd"}})

def _sandbox_access(path, *args, **kwargs):
    if kwargs.get("dir_fd") is not None or (len(args) >= 2 and args[1] is not None):
        raise PermissionError("Crucible sandbox denied dir_fd filesystem access.")
    if _REALPATH_RESOLVING:
        return _real_os_access(path, *args[:1], **{{k: v for k, v in kwargs.items() if k != "dir_fd"}})
    return _real_os_access(_sandbox_path(path), *args[:1], **{{k: v for k, v in kwargs.items() if k != "dir_fd"}})

def _sandbox_listdir(path="."):
    return _real_os_listdir(_sandbox_path(path))

def _sandbox_scandir(path="."):
    return _real_os_scandir(_sandbox_path(path))

def _sandbox_walk(top, *args, **kwargs):
    return _real_os_walk(_sandbox_path(top), *args, **kwargs)

def _sandbox_path_query(real_fn):
    def guarded(path, *args, **kwargs):
        return real_fn(_sandbox_path(path), *args, **kwargs)
    return guarded

def _sandbox_path_pair_query(real_fn):
    def guarded(first, second, *args, **kwargs):
        return real_fn(_sandbox_path(first), _sandbox_path(second), *args, **kwargs)
    return guarded

def _sandbox_realpath(path, *args, **kwargs):
    return os.fspath(_sandbox_path(path))

def _sandbox_fwalk(top=".", *args, **kwargs):
    if _real_os_fwalk is None:
        raise PermissionError("Crucible sandbox fwalk is unavailable on this platform.")
    if kwargs.get("dir_fd") is not None:
        raise PermissionError("Crucible sandbox denied dir_fd filesystem access.")
    return _real_os_fwalk(_sandbox_path(top), *args, **kwargs)

def _sandbox_chdir(path):
    return _real_os_chdir(_sandbox_path(path))

def _sandbox_readlink(path, *args, **kwargs):
    if kwargs.get("dir_fd") is not None or (len(args) >= 1 and args[0] is not None):
        raise PermissionError("Crucible sandbox denied dir_fd filesystem access.")
    link_path = _sandbox_path(path)
    target = _real_os_readlink(link_path)
    target_path = _SandboxPath(target)
    if not target_path.is_absolute():
        target_path = link_path.parent / target_path
    try:
        target_path.resolve().relative_to(_SANDBOX_ROOT)
    except ValueError as exc:
        raise PermissionError("Crucible sandbox denied symlink targets outside its temporary root.") from exc
    return target

def _sandbox_mutation(real_fn):
    def guarded(*args, **kwargs):
        if not args:
            raise PermissionError("Crucible sandbox denied filesystem mutation without a path.")
        safe_args = list(args)
        path_indices = (0, 1) if real_fn in (_real_os_replace, _real_os_rename, _real_os_link, _real_os_symlink) else (0,)
        for index in path_indices:
            if index < len(safe_args):
                safe_args[index] = _sandbox_path(safe_args[index])
        for key in ("src_dir_fd", "dst_dir_fd", "dir_fd"):
            if key in kwargs and kwargs[key] is not None:
                raise PermissionError("Crucible sandbox denied dir_fd filesystem access.")
        return real_fn(*safe_args, **kwargs)
    return guarded

def _sandbox_blocked(*_args, **_kwargs):
    raise PermissionError("Crucible sandbox blocked a process-launch or shell escape.")

# Guard both asyncio's convenience helpers and the event-loop subprocess APIs.
_sandbox_asyncio.create_subprocess_exec = _sandbox_blocked
_sandbox_asyncio.create_subprocess_shell = _sandbox_blocked
for _loop_cls_name in ("BaseEventLoop", "AbstractEventLoop"):
    _loop_cls = getattr(_sandbox_asyncio, _loop_cls_name, None)
    if _loop_cls is not None:
        _loop_cls.subprocess_exec = _sandbox_blocked
        _loop_cls.subprocess_shell = _sandbox_blocked

_builtins.open = _sandbox_open
_io.open = _sandbox_io_open
os.open = _sandbox_os_open
os.stat = _sandbox_stat
os.lstat = _sandbox_lstat
os.access = _sandbox_access
os.listdir = _sandbox_listdir
os.scandir = _sandbox_scandir
os.walk = _sandbox_walk
if _real_os_fwalk is not None:
    os.fwalk = _sandbox_fwalk
os.readlink = _sandbox_readlink
os.chdir = _sandbox_chdir
os.remove = _sandbox_mutation(_real_os_remove)
os.unlink = _sandbox_mutation(_real_os_unlink)
os.rmdir = _sandbox_mutation(_real_os_rmdir)
os.removedirs = _sandbox_mutation(_real_os_removedirs)
os.replace = _sandbox_mutation(_real_os_replace)
os.rename = _sandbox_mutation(_real_os_rename)
os.mkdir = _sandbox_mutation(_real_os_mkdir)
os.makedirs = _sandbox_mutation(_real_os_makedirs)
os.symlink = _sandbox_mutation(_real_os_symlink)
os.link = _sandbox_mutation(_real_os_link)
os.path.realpath = _sandbox_realpath
for _name in ("exists", "lexists", "isfile", "isdir", "islink", "ismount", "getsize", "getmtime", "getatime", "getctime", "getmode"):
    _real_path_fn = getattr(os.path, _name, None)
# Guard filesystem-querying os.path helpers individually, without replacing the os.path module.
for _name in ("exists", "lexists", "isfile", "isdir", "islink", "ismount", "getsize", "getmtime", "getatime", "getctime", "getmode"):
    _real_path_query = getattr(os.path, _name, None)
    if _real_path_query is not None:
        setattr(os.path, _name, _sandbox_path_query(_real_path_query))
_real_path_samefile = getattr(os.path, "samefile", None)
if _real_path_samefile is not None:
    os.path.samefile = _sandbox_path_pair_query(_real_path_samefile)
os.system = _sandbox_blocked
os.popen = _sandbox_blocked
if _real_os_startfile is not None:
    os.startfile = _sandbox_blocked
try:
    _sandbox_shutil.rmtree
    _sandbox_shutil.rmtree = _sandbox_blocked
    _sandbox_shutil.copytree = _sandbox_blocked
    _sandbox_shutil.make_archive = _sandbox_blocked
except Exception:
    pass

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
            serialized_result = None
            try:
                candidate = json.dumps(res, ensure_ascii=False)
                if len(candidate.encode("utf-8")) <= 256 * 1024:
                    serialized_result = candidate
            except Exception:
                serialized_result = None

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

            entry = {"index": i, "success": not is_err, "output": str(res)[:300]}
            if serialized_result is not None:
                entry["result_json"] = serialized_result
            if is_err:
                entry["error"] = err_msg
            results.append(entry)
        except BaseException as exc:
            tb = traceback.format_exc()
            results.append({{"index": i, "success": False, "error": str(exc), "traceback": tb}})
            
    print(json.dumps(results))

if __name__ == '__main__':
    run_tests()
"""
        start_time = time.time()
        try:
            sandbox_root = Path(tempfile.mkdtemp(prefix="brahma-crucible-"))
            try:
                proc = subprocess.run(
                    [py_exe, "-c", harness_script],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=str(sandbox_root),
                    env=_sandbox_environment(sandbox_root),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            finally:
                import shutil
                shutil.rmtree(sandbox_root, ignore_errors=True)
            elapsed = time.time() - start_time
            if proc.returncode != 0:
                err_text = _redact_text(proc.stderr.strip() or proc.stdout.strip() or "Process exited with error code.")
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
                return False, (
                    "Sandbox execution produced no authoritative test result; "
                    "completion cannot be inferred from a zero exit code."
                ), {"results": [], "elapsed_s": elapsed}

            safe_results = []
            for item in test_results:
                if isinstance(item, dict):
                    safe_results.append({
                        key: (_redact_text(value) if isinstance(value, str) else value)
                        for key, value in item.items()
                    })
                else:
                    safe_results.append(_redact_text(item))
            all_passed = all(t.get("success", False) for t in safe_results if isinstance(t, dict))
            if not all_passed:
                first_err = _redact_text(next(
                    (t.get("error", "Unknown test failure") for t in safe_results
                     if isinstance(t, dict) and not t.get("success")),
                    "Test failed",
                ))
                return False, f"Test Verification Failed: {first_err}", {"results": safe_results, "elapsed_s": elapsed}

            return True, f"Passed {len(safe_results)}/{len(safe_results)} tests in {elapsed:.2f}s", {
                "results": safe_results,
                "elapsed_s": elapsed
            }

        except subprocess.TimeoutExpired:
            return False, f"Sandbox execution timed out after {timeout} seconds.", {"elapsed_s": timeout}
        except Exception as e:
            return False, f"Sandbox execution error: {_redact_text(e)}", {"elapsed_s": 0}
