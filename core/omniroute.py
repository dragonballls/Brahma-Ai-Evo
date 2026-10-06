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
        self._lifecycle_lock = threading.Lock()
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
        """Return gateway readiness without holding state locks across slow I/O or startup."""
        if not self.enabled:
            with self._lock:
                self._ready = False
            return False

        def _cached_ready(now: float) -> bool | None:
            with self._lock:
                if not force and self._ready and (now - self._last_check_at) < self._check_cache_seconds:
                    return True
                if not force and not self._ready and now < self._retry_after:
                    return False
            return None

        now = time.monotonic()
        cached = _cached_ready(now)
        if cached is not None:
            return cached

        # Only one thread performs the slow gateway probe/start sequence at a time.
        # Gateway state readers remain free because the state lock is never held while
        # probe_only()/ensure_running() performs loopback I/O or process startup.
        with self._lifecycle_lock:
            now = time.monotonic()
            cached = _cached_ready(now)
            if cached is not None:
                return cached

            try:
                if self.provisioner.probe_only():
                    ready = True
                else:
                    ready = bool(self.provisioner.ensure_running(wait_seconds=15.0))
            except Exception:
                with self._lock:
                    self._ready = False
                    self._last_check_at = now
                    self._retry_after = now + self._failure_cooldown_seconds
                return False

            with self._lock:
                # The provisioner may have moved to an automatic free loopback port.
                # Keep every Brahma caller on the same internal endpoint.
                self.base_url = self.provisioner.base_url.rstrip("/")
                self._ready = bool(ready)
                self._last_check_at = now
                self._retry_after = 0.0 if self._ready else now + self._failure_cooldown_seconds
                sync_needed = self._ready

        # Credential registration can launch slow subprocesses; keep it off both
        # the gateway state and lifecycle locks so UI/status/request coordination stays responsive.
        if sync_needed:
            self._sync_credentials_once()
        with self._lock:
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
        # Serialize lifecycle-changing work with startup/stop so provider
        # configuration can never race a gateway process transition.
        with self._lifecycle_lock:
            if not self.provisioner.ensure_running(wait_seconds=15.0):
                raise RuntimeError("OmniRoute is not ready; provider configuration was not applied.")
            current_base_url = self.provisioner.base_url.rstrip("/")

            # Keep the lifecycle lock through credential registration so stop()
            # cannot terminate or race the gateway while its provider state changes.
            with self._credentials_lock:
                result = self.provisioner.configure_provider(provider, api_key)

        with self._lock:
            self.base_url = current_base_url
            self._credentials_synced = False
            self._ready = True
            self._last_check_at = time.monotonic()
        return result

    def mark_credentials_stale(self) -> None:
        """Invalidate the cached provider-key sync without starting the gateway."""
        with self._lock:
            self._credentials_synced = False

    def sync_credentials(self) -> dict[str, object]:
        """Re-sync provider credentials without racing gateway process shutdown."""
        with self._lifecycle_lock:
            with self._credentials_lock:
                with self._lock:
                    self._credentials_synced = False
                try:
                    result = self.provisioner.sync_existing_provider_keys(API_CONFIG_PATH)
                except Exception as exc:
                    return {"ok": False, "synced": False, "error": str(exc)}
                skipped = list(result.get("skipped") or [])
                with self._lock:
                    self._credentials_synced = not skipped
                return {"ok": not skipped, "synced": not skipped, **result}

    def test_provider(self, provider: str) -> dict[str, object]:
        import subprocess

        if not self.ensure_ready():
            return {
                "ok": False,
                "provider": str(provider).strip().lower(),
                "tested": False,
                "message": "OmniRoute is not ready",
            }

        command = self.provisioner.command_argv()
        result = subprocess.run(
            [*command, "--non-interactive", "providers", "test", str(provider).strip().lower()],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=self.provisioner.environment(),
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
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
        with self._lifecycle_lock:
            try:
                self.provisioner.stop()
            finally:
                with self._lock:
                    self._ready = False
                    self._credentials_synced = False
                    self._last_check_at = 0.0
                    self._retry_after = 0.0


_gateway = OmniRouteGateway()
atexit.register(_gateway.stop)


def gateway() -> OmniRouteGateway:
    return _gateway
