"""Local OmniRoute gateway lifecycle for Brahma Evo."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from core.user_paths import get_user_data_dir
from .omniroute_setup import OmniRouteProvisioner, default_data_dir


class OmniRouteGateway:
    """Lazily starts OmniRoute and exposes the local OpenAI-compatible base URL."""

    def __init__(self, base_url: str | None = None) -> None:
        import os

        self.base_url = (
            base_url
            or os.environ.get("BRAHMA_OMNIROUTE_BASE_URL", "")
            or "http://127.0.0.1:20128/v1"
        ).rstrip("/")
        self.provisioner = OmniRouteProvisioner(self.base_url)
        self._lock = threading.Lock()
        self._ready = False
        self._credentials_synced = False

    @property
    def enabled(self) -> bool:
        import os

        value = os.environ.get("BRAHMA_OMNIROUTE_ENABLED", "1").strip().lower()
        return value not in {"0", "false", "no", "off"}

    def ensure_ready(self) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            if self.provisioner.probe_only():
                self._ready = True
                self._sync_credentials_once()
                return True
            try:
                ready = self.provisioner.ensure_running(wait_seconds=15.0)
            except Exception:
                self._ready = False
                return False
            self._ready = bool(ready)
            if self._ready:
                self._sync_credentials_once()
            return self._ready

    def _sync_credentials_once(self) -> None:
        if self._credentials_synced:
            return
        self._credentials_synced = True
        try:
            self.provisioner.sync_existing_provider_keys(
                get_user_data_dir() / "config" / "api_keys.json"
            )
        except Exception:
            pass

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
        self.provisioner.ensure_running(wait_seconds=15.0)
        return self.provisioner.configure_provider(provider, api_key)

    def sync_credentials(self) -> dict[str, object]:
        """Re-sync the current user provider-key file into the running gateway."""
        self._credentials_synced = False
        self._sync_credentials_once()
        return {"ok": True, "synced": True}

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


_gateway = OmniRouteGateway()


def gateway() -> OmniRouteGateway:
    return _gateway
