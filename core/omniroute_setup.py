"""Lazy, headless OmniRoute runtime support for Brahma Evo.

This mirrors the previously verified JARVIS integration: packaged runtime first,
then a user-local OmniRoute install, then an existing system install. The
runtime stays local and is never launched with a visible console window.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import socket
import urllib.error
import urllib.parse
import urllib.request
from urllib.request import HTTPRedirectHandler

from core.runtime_contract import OMNIROUTE_VERSION
from core.runtime_paths import API_CONFIG_PATH, OMNIROUTE_DEFAULT_BASE_URL, OMNIROUTE_DEFAULT_PORT


OMNIROUTE_PACKAGE = f"omniroute@{OMNIROUTE_VERSION}"
DEFAULT_PORT = OMNIROUTE_DEFAULT_PORT
PROVISION_TIMEOUT_SECONDS = max(
    30, int(os.environ.get("BRAHMA_OMNIROUTE_PROVISION_TIMEOUT", "300"))
)


def _is_windows() -> bool:
    return sys.platform == "win32"


def _hidden_creationflags() -> int:
    """Keep OmniRoute helper subprocesses invisible in GUI builds."""
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _split_command(value: str) -> list[str]:
    return shlex.split(value, posix=not _is_windows())


def default_data_dir() -> Path:
    override = os.environ.get("BRAHMA_OMNIROUTE_DATA_DIR")
    if override:
        return Path(override).expanduser()
    root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return root / "BrahmaAI" / "OmniRoute"


def _packaged_runtime_root() -> Path | None:
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    root = Path(base) / "omniroute_runtime"
    return root if root.is_dir() else None


_KEY_PREFIX_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("sk-ant-", "anthropic"),
    ("sk-or-v1-", "openrouter"),
    ("gsk_", "groq"),
    ("xai-", "xai"),
    ("AIza", "gemini"),
    ("csk-", "cerebras"),
    ("sk-proj-", "openai"),
    ("sk-svcacct-", "openai"),
)


def detect_provider_from_key(api_key: str) -> str | None:
    key = str(api_key or "").strip()
    if not key:
        return None
    for prefix, provider in _KEY_PREFIX_PROVIDERS:
        if key.lower().startswith(prefix.lower()):
            return provider
    return None


@dataclass(frozen=True)
class OmniRouteRuntimeStatus:
    available: bool
    source: str
    version: str | None
    base_url: str
    data_dir: str
    running: bool
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "source": self.source,
            "version": self.version,
            "base_url": self.base_url,
            "data_dir": self.data_dir,
            "running": self.running,
            "reason": self.reason,
        }


class OmniRouteProvisioner:
    def __init__(
        self,
        base_url: str = OMNIROUTE_DEFAULT_BASE_URL,
        data_dir: Path | None = None,
    ) -> None:
        from core.local_endpoint import validate_local_endpoint
        self.base_url = validate_local_endpoint(base_url)
        self.data_dir = (data_dir or default_data_dir()).expanduser()
        self._resolved: tuple[str, ...] | None = None
        self._source = "unavailable"
        self.probe_timeout_seconds = max(
            0.25, float(os.environ.get("BRAHMA_OMNIROUTE_PROBE_TIMEOUT_SECONDS", "0.75"))
        )
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def port(self) -> int:
        parsed = urllib.parse.urlparse(self.base_url)
        return int(parsed.port or DEFAULT_PORT)

    def _select_loopback_port(self) -> None:
        """Silently move OmniRoute to a free loopback port when its default is occupied."""
        parsed = urllib.parse.urlparse(self.base_url)
        host = parsed.hostname or "127.0.0.1"
        current = int(parsed.port or DEFAULT_PORT)
        if host not in {"127.0.0.1", "localhost", "::1"} or current != DEFAULT_PORT:
            return
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", current))
            finally:
                probe.close()
            return
        except OSError:
            pass

        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.bind(("127.0.0.1", 0))
            free_port = int(probe.getsockname()[1])
            probe.close()
            self.base_url = urllib.parse.urlunparse(
                (parsed.scheme or "http", f"127.0.0.1:{free_port}", parsed.path or "/v1", "", "", "")
            ).rstrip("/")
        except OSError:
            return

    def _packaged_command(self) -> tuple[str, ...] | None:
        root = _packaged_runtime_root()
        if root is None:
            return None
        node = root / "node.exe" if _is_windows() else root / "bin" / "node"
        entry = root / "node_modules" / "omniroute" / "bin" / "omniroute.mjs"
        if not node.is_file() or not entry.is_file():
            return None
        return (str(node), str(entry))

    def _local_install_command(self) -> tuple[str, ...] | None:
        entry = self.data_dir / "node_modules" / "omniroute" / "bin" / "omniroute.mjs"
        node = os.environ.get("BRAHMA_NODE_COMMAND") or shutil.which("node")
        if entry.is_file() and node:
            # Invoke the Node entrypoint directly. This avoids relying on
            # Windows .cmd shims, which are not reliable with direct Popen().
            return (str(node), str(entry))
        return None

    def _system_command(self) -> tuple[str, ...] | None:
        configured = os.environ.get("BRAHMA_OMNIROUTE_COMMAND", "").strip()
        if configured:
            argv = _split_command(configured)
            return tuple(argv) if argv else None
        found = shutil.which("omniroute")
        return (found,) if found else None

    def _node_and_npm(self) -> tuple[str, str] | None:
        node = os.environ.get("BRAHMA_NODE_COMMAND") or shutil.which("node")
        npm = os.environ.get("BRAHMA_NPM_COMMAND")
        if not npm:
            npm = shutil.which("npm.cmd" if _is_windows() else "npm") or shutil.which("npm")
        return (node, npm) if node and npm else None

    @staticmethod
    def _version(argv: tuple[str, ...], env: dict[str, str]) -> str | None:
        try:
            result = subprocess.run(
                [*argv, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                env=env,
                creationflags=_hidden_creationflags(),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        text = (result.stdout or "").strip()
        return text.splitlines()[-1].strip() if text else None

    def _npm_install(self) -> tuple[str, ...] | None:
        tools = self._node_and_npm()
        if tools is None:
            return None
        _node, npm = tools
        self.data_dir.mkdir(parents=True, exist_ok=True)
        package_json = self.data_dir / "package.json"
        env = {**os.environ, "DATA_DIR": str(self.data_dir), "NODE_ENV": "production"}
        result = subprocess.run(
            [
                npm,
                "install",
                "--prefix",
                str(self.data_dir),
                "--no-fund",
                "--no-audit",
                "--omit=dev",
                OMNIROUTE_PACKAGE,
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROVISION_TIMEOUT_SECONDS,
            env=env,
            creationflags=_hidden_creationflags(),
            check=False,
        )
        if result.returncode != 0 or not package_json.is_file():
            return None
        return self._local_install_command()

    def resolve_command(self, *, install_if_missing: bool = True) -> tuple[str, ...]:
        if self._resolved:
            return self._resolved

        for command, source in (
            (self._packaged_command(), "bundled"),
            (self._local_install_command(), "user-local"),
            (self._system_command(), "system"),
        ):
            if command:
                self._resolved = command
                self._source = source
                return command

        if install_if_missing:
            local = self._npm_install()
            if local:
                self._resolved = local
                self._source = "user-local"
                return local

        raise RuntimeError(
            f"OmniRoute {OMNIROUTE_VERSION} is unavailable. "
            "Install Node/npm or provide a configured OmniRoute command."
        )

    def command_argv(
        self,
        *,
        install_if_missing: bool = True,
        for_start: bool = False,
    ) -> list[str]:
        command = list(self.resolve_command(install_if_missing=install_if_missing))
        if for_start and "--no-open" not in command:
            command.append("--no-open")
        return command

    def environment(self) -> dict[str, str]:
        return {
            **os.environ,
            "DATA_DIR": str(self.data_dir),
            "PORT": str(self.port),
            "OMNIROUTE_TELEMETRY": os.environ.get("OMNIROUTE_TELEMETRY", "false"),
        }

    def _probe(self) -> bool:
        # Validate the OpenAI-compatible /models contract rather than trusting a
        # generic health endpoint on the same loopback port.
        try:
            class _NoRedirect(HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    raise urllib.error.URLError("Redirects are not permitted for OmniRoute probes.")

            opener = urllib.request.build_opener(_NoRedirect)
            with opener.open(
                urllib.request.Request(
                    self.base_url + "/models",
                    headers={"Accept": "application/json"},
                ),
                timeout=self.probe_timeout_seconds,
            ) as response:
                if not 200 <= int(response.status) < 300:
                    return False
                payload = json.loads(response.read().decode("utf-8"))
                return isinstance(payload, dict) and isinstance(payload.get("data"), list)
        except urllib.error.HTTPError:
            # Only a successful OpenAI-compatible /models response proves that the
            # loopback service is the OmniRoute gateway. Authentication or method
            # errors can be produced by an unrelated process occupying the port.
            return False
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, UnicodeDecodeError):
            return False

    def probe_only(self) -> bool:
        return self._probe()

    def ensure_running(self, *, wait_seconds: float = 15.0) -> bool:
        if self._probe():
            return True
        total_wait = max(0.5, float(wait_seconds))
        self._resolved = None
        self._source = "unavailable"
        command = self.command_argv(for_start=True)

        # Bind a candidate immediately before launch, then retry with a fresh
        # ephemeral loopback port if another process wins the race.
        for attempt in range(3):
            self._select_loopback_port()
            if self._process is not None and self._process.poll() is None:
                self.stop()
            try:
                self._process = subprocess.Popen(
                    command + ["--port", str(self.port)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=self.environment(),
                    creationflags=_hidden_creationflags(),
                    close_fds=True,
                )
            except OSError:
                self.stop()
                if attempt == 2:
                    return False
                continue

            deadline = time.monotonic() + total_wait
            while time.monotonic() < deadline:
                if self._probe():
                    return True
                if self._process.poll() is not None:
                    break
                time.sleep(0.25)
            self.stop()

        return False

    def stop(self) -> None:
        """Stop only the OmniRoute process owned by this application."""
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=3)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=2)
            except Exception:
                pass

    def status(self) -> OmniRouteRuntimeStatus:
        try:
            argv = self.resolve_command(install_if_missing=False)
        except RuntimeError as exc:
            return OmniRouteRuntimeStatus(
                False, "unavailable", None, self.base_url,
                str(self.data_dir), False, str(exc)
            )
        version = self._version(argv, self.environment())
        return OmniRouteRuntimeStatus(
            bool(version == OMNIROUTE_VERSION),
            self._source,
            version,
            self.base_url,
            str(self.data_dir),
            bool(self._process and self._process.poll() is None),
            None if version == OMNIROUTE_VERSION else "OmniRoute version validation failed",
        )

    def configure_provider(self, provider: str, api_key: str) -> dict[str, object]:
        normalized = str(provider or "").strip().lower()
        key = str(api_key or "").strip()
        if normalized in {"", "auto", "detect"}:
            normalized = detect_provider_from_key(key) or ""
        if not normalized or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_-." for ch in normalized):
            raise ValueError("Invalid OmniRoute provider identifier")
        if len(key) < 8:
            raise ValueError("Provider API key is too short")
        command = self.command_argv()
        result = subprocess.run(
            [*command, "--non-interactive", "providers", "add", normalized, "--credential-stdin"],
            input=key + "\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROVISION_TIMEOUT_SECONDS,
            env=self.environment(),
            creationflags=_hidden_creationflags(),
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("OmniRoute rejected the provider credential")
        return {"ok": True, "provider": normalized, "configured": True}

    def sync_existing_provider_keys(self, api_key_path: Path) -> dict[str, object]:
        if not api_key_path.is_file():
            return {"configured": [], "skipped": []}
        try:
            payload = json.loads(api_key_path.read_text(encoding="utf-8"))
        except Exception:
            return {"configured": [], "skipped": ["invalid api key file"]}
        configured: list[str] = []
        skipped: list[str] = []
        fields = {
            "openai_api_key": "openai",
            "anthropic_api_key": "anthropic",
            "gemini_api_key": "gemini",
            "openrouter_api_key": "openrouter",
            "groq_api_key": "groq",
            "xai_api_key": "xai",
            "cerebras_api_key": "cerebras",
            "deepseek_api_key": "deepseek",
            "mistral_api_key": "mistral",
            "cohere_api_key": "cohere",
        }
        for field, provider in fields.items():
            key = str(payload.get(field) or "").strip()
            if not key:
                continue
            try:
                self.configure_provider(provider, key)
                configured.append(provider)
            except Exception:
                skipped.append(provider)
        return {"configured": configured, "skipped": skipped}
