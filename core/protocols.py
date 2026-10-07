"""
Brahma Protocol Execution Engine (v2)
High-level macro orchestrator that reconfigures the user's digital environment
across PC and connected mobile devices with a single directive.
"""

import os
import sys
import subprocess
import ctypes
from typing import Dict, Any, List, Optional
import psutil

from core.identity import identity

user32 = ctypes.windll.user32 if sys.platform == "win32" else None


class ProtocolEngine:
    def __init__(self):
        self.active_protocol: Optional[str] = None
        self.registered_protocols: Dict[str, Any] = {
            "deep_work": self.execute_deep_work,
            "redline": self.execute_redline,
            "lockdown": self.execute_lockdown,
            "nightfall": self.execute_nightfall,
        }

    def execute(self, protocol_name: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Executes a recognized high-level protocol."""
        key = protocol_name.strip().lower().replace(" ", "_")
        if key not in self.registered_protocols:
            return {
                "success": False,
                "error": f"Unknown protocol '{protocol_name}'. Available: {list(self.registered_protocols.keys())}"
            }

        fn = self.registered_protocols[key]
        try:
            result = fn(params or {})
            self.active_protocol = key
            return {"success": True, "protocol": key, "result": result}
        except Exception as e:
            return {"success": False, "protocol": key, "error": str(e)}

    # ── PROTOCOL: DEEP WORK ──────────────────────────────────────────────
    def execute_deep_work(self, params: Dict[str, Any]) -> str:
        """
        Maximizes focus:
        - Sets Brahma behavior mode to minimal.
        - Minimizes distracting applications.
        """
        identity.set_behavior_mode("minimal")

        distractions = ["steam.exe", "discord.exe", "telegram.exe", "spotify.exe", "epicgameslauncher.exe"]
        minimized_count = 0

        # Minimize known distraction windows
        def enum_windows_callback(hwnd, extra):
            nonlocal minimized_count
            if user32 and user32.IsWindowVisible(hwnd):
                pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                try:
                    proc = psutil.Process(pid.value)
                    if proc.name().lower() in distractions:
                        user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE = 6
                        minimized_count += 1
                except Exception:
                    pass
            return True

        if user32:
            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
            user32.EnumWindows(WNDENUMPROC(enum_windows_callback), 0)

        return f"Protocol Deep Work engaged. Persona switched to minimal. {minimized_count} distraction windows minimized."

    # ── PROTOCOL: REDLINE (MAX PERFORMANCE) ──────────────────────────────
    def execute_redline(self, params: Dict[str, Any]) -> str:
        """
        Frees memory and sets Windows to High/Ultimate Performance power plan.
        """
        if sys.platform != "win32":
            raise RuntimeError("Redline protocol requires Windows.")
        # GUID for High Performance: 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c
        try:
            powercfg = subprocess.run(
                ["powercfg", "/setactive", "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise RuntimeError(f"Unable to run powercfg: {exc}") from exc
        if powercfg.returncode != 0:
            detail = (powercfg.stderr or powercfg.stdout or "").strip()
            raise RuntimeError(f"High Performance power plan was not applied{': ' + detail if detail else '.'}")

        # Memory compaction / purge idle working sets
        ram_before = psutil.virtual_memory().percent
        try:
            if sys.platform == "win32":
                # Flush empty working sets
                ctypes.windll.psapi.EmptyWorkingSet(ctypes.windll.kernel32.GetCurrentProcess())
        except Exception:
            pass

        ram_after = psutil.virtual_memory().percent
        return f"Protocol Redline active. Power plan set to High Performance. RAM load at {ram_after}%."

    # ── PROTOCOL: LOCKDOWN (GHOST PRIVACY) ───────────────────────────────
    def execute_lockdown(self, params: Dict[str, Any]) -> str:
        """
        Instantly locks Windows session and mutes speakers.
        """
        # Mute audio via Windows master volume key simulation
        if not user32:
            raise RuntimeError("Lockdown protocol requires Windows.")
        VK_VOLUME_MUTE = 0xAD
        KEYEVENTF_KEYUP = 0x0002
        user32.keybd_event(VK_VOLUME_MUTE, 0, 0, 0)
        user32.keybd_event(VK_VOLUME_MUTE, 0, KEYEVENTF_KEYUP, 0)

        # LockWorkStation returns TRUE when Windows accepted the lock request.
        # It does not expose remote proof that the workstation has finished locking,
        # so do not claim the final locked state as verified.
        locked_request = bool(user32.LockWorkStation())
        if not locked_request:
            raise RuntimeError("Windows rejected the workstation lock request.")
        return "Protocol Lockdown request accepted by Windows; final lock/mute state is not independently observable."

    # ── PROTOCOL: NIGHTFALL (WRAP UP DAY) ────────────────────────────────
    def execute_nightfall(self, params: Dict[str, Any]) -> str:
        """
        Sets assistant to casual/night mode, switches theme if available, and gives a closing debrief.
        """
        identity.set_behavior_mode("minimal")
        return "Protocol Nightfall initialized. Minimal behavior mode enabled; no session-log entry was claimed."


# Global singleton instance
protocols = ProtocolEngine()
