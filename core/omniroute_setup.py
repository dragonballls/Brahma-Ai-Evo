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
import urllib.error
import urllib.parse
import urllib.request

from core.runtime_contract import NODE_VERSION, OMNIROUTE_VERSION
from core.runtime_paths import API_CONFIG_PATH, OMNIROUTE_DEFAULT_BASE_URL, OMNIROUTE_DEFAULT_PORT


OMNIROUTE_PACKAGE = f"omniroute@{OMNIROUTE_VERSION}"
DEFAULT_PORT = OMNIROUTE_DEFAULT_PORT
PROVISION_TIMEOUT_SECONDS = max(
    30, int(os.environ.get("BRAHMA_OMNIROUTE_PROVISION_TIMEOUT", "300"))
)


def _is_windows() -> bool:
    return sys.platform == "win32"


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
        self.base_url = base_url.rstrip("/")
        self.data_dir = (data_dir or default_data_dir()).expanduser()
        self._resolved: tuple[str, ...] | None = None
        self._source = "unavailable"
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def port(self) -> int:
        parsed = urllib.parse.urlparse(self.base_url)
        return int(parsed.port or DEFAULT_PORT)

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
        binary = self.data_dir / "node_modules" / ".bin" / (
            "omniroute.cmd" if _is_windows() else "omniroute"
        )
        return (str(binary),) if binary.is_file() else None

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
        parsed = urllib.parse.urlparse(self.base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        urls = (
            self.base_url + "/models",
            origin + "/api/monitoring/health",
            origin + "/healthz",
        )
        for url in urls:
            try:
                with urllib.request.urlopen(
                    urllib.request.Request(url, headers={"Accept": "application/json"}),
                    timeout=1.5,
                ) as response:
                    if 200 <= int(response.status) < 300:
                        return True
            except urllib.error.HTTPError as exc:
                if exc.code in {401, 403, 405}:
                    return True
            except (urllib.error.URLError, TimeoutError, OSError):
                pass
        return False

    def probe_only(self) -> bool:
        return self._probe()

    def ensure_running(self, *, wait_seconds: float = 15.0) -> bool:
        if self._probe():
            return True
        command = self.command_argv(for_start=True)
        if self._process is None or self._process.poll() is not None:
            self._process = subprocess.Popen(
                command + ["--port", str(self.port)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self.environment(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                close_fds=True,
            )
        deadline = time.monotonic() + max(0.5, float(wait_seconds))
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


class OmniRouteGateway:
    """Lazily starts OmniRoute and exposes the local OpenAI-compatible base URL."""

    def __init__(self, base_url: str | None = None) -> None:
        import os

        self.base_url = (
            base_url
            or os.environ.get("BRAHMA_OMNIROUTE_BASE_URL", "")
            or OMNIROUTE_DEFAULT_BASE_URL
        ).rstrip("/")
        self.provisioner = OmniRouteProvisioner(self.base_url)
        self._lock = threading.Lock()
        self._ready = False
        self._credentials_synced = False
        self._last_check_at = 0.0
        self._retry_after = 0.0
        self._check_cache_seconds = max(
            1.0, float(__import__("os").environ.get("BRAHMA_OMNIROUTE_CHECK_CACHE_SECONDS", "5"))
        )
        self._failure_cooldown_seconds = max(
            5.0, float(__import__("os").environ.get("BRAHMA_OMNIROUTE_FAILURE_COOLDOWN_SECONDS", "30"))
        )

    @property
    def enabled(self) -> bool:
        import os

        value = os.environ.get("BRAHMA_OMNIROUTE_ENABLED", "1").strip().lower()
        return value not in {"0", "false", "no", "off"}

    def ensure_ready(self, *, force: bool = False) -> bool:
        """Return gateway readiness without repeatedly reprovisioning a failed runtime."""
        if not self.enabled:
            self._ready = False
            return False

        now = time.monotonic()
        with self._lock:
            if not force and self._ready and (now - self._last_check_at) < self._check_cache_seconds:
                return True
            if not force and not self._ready and now < self._retry_after:
                return False

            if self.provisioner.probe_only():
                self._ready = True
                self._last_check_at = now
                self._retry_after = 0.0
                self._sync_credentials_once()
                return True

            try:
                ready = self.provisioner.ensure_running(wait_seconds=15.0)
            except Exception:
                self._ready = False
                self._last_check_at = now
                self._retry_after = now + self._failure_cooldown_seconds
                return False

            self._ready = bool(ready)
            self._last_check_at = now
            self._retry_after = 0.0 if self._ready else now + self._failure_cooldown_seconds
            if self._ready:
                self._sync_credentials_once()
            return self._ready

    def _sync_credentials_once(self) -> None:
        if self._credentials_synced:
            return
        try:
            result = self.provisioner.sync_existing_provider_keys(
                API_CONFIG_PATH
            )
            self._credentials_synced = not bool(result.get("skipped"))
        except Exception:
            # Leave the flag false so a later healthy gateway can retry the sync.
            self._credentials_synced = False

    def status(self) -> dict[str, Any]:
        try:
            data = self.provisioner.status().as_dict()
        except Exception as exc:
            data = {
                "available": False,
                "source": "error",
                "version": None,
                "base_url": self.base_url,
                "data_dir": str(default_data_dir()),
                "running": False,
                "reason": str(exc),
            }
        data["enabled"] = self.enabled
        return data

    def configure_provider(self, provider: str, api_key: str) -> dict[str, object]:
        if not self.provisioner.ensure_running(wait_seconds=15.0):
            raise RuntimeError("OmniRoute is not ready; provider configuration was not applied.")
        with self._lock:
            result = self.provisioner.configure_provider(provider, api_key)
            self._credentials_synced = False
            self._ready = True
            self._last_check_at = time.monotonic()
            return result

    def mark_credentials_stale(self) -> None:
        """Invalidate the cached provider-key sync without starting the gateway."""
        with self._lock:
            self._credentials_synced = False

    def sync_credentials(self) -> dict[str, object]:
        """Re-sync the current user provider-key file into the running gateway."""
        with self._lock:
            self._credentials_synced = False
            try:
                result = self.provisioner.sync_existing_provider_keys(
                    API_CONFIG_PATH
                )
            except Exception as exc:
                return {"ok": False, "synced": False, "error": str(exc)}
            skipped = list(result.get("skipped") or [])
            self._credentials_synced = not skipped
            return {"ok": not skipped, "synced": not skipped, **result}

    def test_provider(self, provider: str) -> dict[str, object]:
        command = self.provisioner.command_argv()
        import subprocess
        result = subprocess.run(
            [*command, "--non-interactive", "providers", "test", str(provider).strip().lower()],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=self.provisioner.environment(),
            check=False,
        )
        return {
            "ok": result.returncode == 0,
            "provider": str(provider).strip().lower(),
            "tested": True,
            "message": "Provider test passed" if result.returncode == 0 else "Provider test failed",
        }

    def stop(self) -> None:
        """Stop OmniRoute if this application launched the process."""
        with self._lock:
            self.provisioner.stop()
            self._ready = False
            self._credentials_synced = False
            self._last_check_at = 0.0
            self._retry_after = 0.0


_gateway = OmniRouteGateway()


def gateway() -> OmniRouteGateway:
    return _gateway
