"""Local OmniRoute gateway lifecycle for Brahma Evo."""

from __future__ import annotations

import atexit
import json
import threading
import time
from pathlib import Path
from typing import Any

from core.runtime_paths import API_CONFIG_PATH, OMNIROUTE_DEFAULT_BASE_URL
from .omniroute_setup import OmniRouteProvisioner, default_data_dir


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
        self._credentials_lock = threading.Lock()
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
        sync_needed = False
        with self._lock:
            if not force and self._ready and (now - self._last_check_at) < self._check_cache_seconds:
                return True
            if not force and not self._ready and now < self._retry_after:
                return False

            if self.provisioner.probe_only():
                self._ready = True
                self._last_check_at = now
                self._retry_after = 0.0
                sync_needed = True
                ready = True
            else:
                try:
                    ready = self.provisioner.ensure_running(wait_seconds=15.0)
                    # The provisioner may have moved to an automatic free loopback port.
                    # Keep every Brahma caller on the same internal endpoint.
                    self.base_url = self.provisioner.base_url.rstrip("/")
                except Exception:
                    self._ready = False
                    self._last_check_at = now
                    self._retry_after = now + self._failure_cooldown_seconds
                    return False

                self._ready = bool(ready)
                self._last_check_at = now
                self._retry_after = 0.0 if self._ready else now + self._failure_cooldown_seconds
                sync_needed = self._ready

        # Credential registration can launch slow subprocesses; keep it off the
        # gateway state lock so UI/status/request coordination stays responsive.
        if sync_needed:
            self._sync_credentials_once()
        return bool(self._ready)

    def _sync_credentials_once(self) -> None:
        """Synchronize saved provider keys once without serializing the gateway state lock."""
        with self._credentials_lock:
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
        with self._credentials_lock:
            result = self.provisioner.configure_provider(provider, api_key)
        with self._lock:
            self.base_url = self.provisioner.base_url.rstrip("/")
            self._credentials_synced = False
            self._ready = True
            self._last_check_at = time.monotonic()
        return result

    def mark_credentials_stale(self) -> None:
        """Invalidate the cached provider-key sync without starting the gateway."""
        with self._lock:
            self._credentials_synced = False

    def sync_credentials(self) -> dict[str, object]:
        """Re-sync the current user provider-key file without blocking gateway state operations."""
        with self._credentials_lock:
            with self._lock:
                self._credentials_synced = False
            try:
                result = self.provisioner.sync_existing_provider_keys(
                    API_CONFIG_PATH
                )
            except Exception as exc:
                return {"ok": False, "synced": False, "error": str(exc)}
            skipped = list(result.get("skipped") or [])
            with self._lock:
                self._credentials_synced = not skipped
            return {"ok": not skipped, "synced": not skipped, **result}

    def test_provider(self, provider: str) -> dict[str, object]:
        import subprocess

        command = self.provisioner.command_argv()
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
atexit.register(_gateway.stop)


def gateway() -> OmniRouteGateway:
    return _gateway
