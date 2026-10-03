from __future__ import annotations

import asyncio
import csv
import importlib.util
import io
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class IntegrationInfo:
    key: str
    project: str
    executable: str | None = None
    installed: bool = False
    path: str | None = None
    python_module: bool = False
    notes: str = ""


def _windows_executable(candidates: Iterable[str]) -> str | None:
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
    if os.name != "nt":
        return None

    roots = [
        Path(os.environ.get("LOCALAPPDATA", "")),
        Path(os.environ.get("PROGRAMFILES", "")),
        Path(os.environ.get("PROGRAMFILES(X86)", "")),
    ]
    patterns = []
    for root in roots:
        if not str(root):
            continue
        for name in candidates:
            patterns.extend(
                [
                    root / name,
                    root / "PowerToys" / name,
                    root / "LibreHardwareMonitor" / name,
                    root / "WinSW" / name,
                ]
            )
    for path in patterns:
        try:
            if path.is_file():
                return str(path)
        except Exception:
            pass
    return None


def _run_command(
    command: list[str],
    *,
    timeout: float = 3.0,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            command,
            capture_output=capture_output,
            text=True,
            timeout=max(0.1, float(timeout)),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except Exception:
        return None


def _http_json(url: str, timeout: float = 1.5) -> Any | None:
    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Brahma-Ai-Evo/desktop-integration"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return __import__("json").load(response)
    except Exception:
        return None


def _walk_sensors(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if not isinstance(node, dict):
        return found
    children = node.get("Children")
    if isinstance(children, list):
        for child in children:
            found.extend(_walk_sensors(child))
    if node.get("Type") and node.get("SensorId"):
        found.append(node)
    return found


class ThirdPartyIntegrationHub:
    """Optional bridge to mature external tools.

    External projects remain independently installed. Brahma never assumes one
    exists, never imports heavyweight runtimes at startup, and never invokes a
    mutating command unless that operation is explicitly requested.
    """

    _DISCOVERY_TTL = 15.0

    def __init__(self):
        self._lock = threading.RLock()
        self._cached_at = 0.0
        self._cache: dict[str, IntegrationInfo] = {}
        self._last_presentmon_at = 0.0
        self._last_presentmon: dict[str, Any] | None = None

    def _discover(self) -> dict[str, IntegrationInfo]:
        procgov = _windows_executable(["ProcGovernor.exe", "ProcGovernor"])
        presentmon = _windows_executable(["PresentMon.exe", "PresentMon"])
        lhm = _windows_executable(["LibreHardwareMonitor.exe", "LibreHardwareMonitor"])
        fancymw = _windows_executable(["FancyWM.exe", "FancyWM"])
        powertoys = _windows_executable(["PowerToys.exe", "PowerToys"])
        fancyzones = _windows_executable(["FancyZonesCLI.exe", "FancyZonesCLI"])
        powertoys_dsc = _windows_executable(["PowerToys.DSC.exe", "PowerToys.DSC"])
        scrcpy = _windows_executable(["scrcpy.exe", "scrcpy"])
        adb = _windows_executable(["adb.exe", "adb"])
        chip_tool = _windows_executable(["chip-tool.exe", "chip-tool"])
        winsw = _windows_executable(["WinSW-x64.exe", "WinSW.exe", "winsw.exe", "winsw"])
        pyatv_installed = importlib.util.find_spec("pyatv") is not None

        return {
            "procgovernor": IntegrationInfo(
                "procgovernor", "Prohect/ProcGovernor", "ProcGovernor.exe",
                bool(procgov), procgov, notes="Optional native Windows CPU/process governor.",
            ),
            "presentmon": IntegrationInfo(
                "presentmon", "GameTechDev/PresentMon", "PresentMon.exe",
                bool(presentmon), presentmon, notes="On-demand frame-time/graphics telemetry.",
            ),
            "librehardwaremonitor": IntegrationInfo(
                "librehardwaremonitor", "LibreHardwareMonitor/LibreHardwareMonitor",
                "LibreHardwareMonitor.exe", bool(lhm), lhm,
                notes="Optional sensor source; REST server is consumed when enabled.",
            ),
            "powertoys": IntegrationInfo(
                "powertoys", "microsoft/PowerToys", "PowerToys.exe",
                bool(powertoys), powertoys, notes="Optional Windows utility/desktop integration.",
            ),
            "fancyzones": IntegrationInfo(
                "fancyzones", "microsoft/PowerToys", "FancyZonesCLI.exe",
                bool(fancyzones), fancyzones, notes="Preferred external zone-layout backend.",
            ),
            "fancywm": IntegrationInfo(
                "fancywm", "FancyWM/fancywm", "FancyWM.exe",
                bool(fancymw), fancymw,
                notes="Detected for cooperative window-management mode.",
            ),
            "scrcpy": IntegrationInfo(
                "scrcpy", "Genymobile/scrcpy", "scrcpy.exe",
                bool(scrcpy), scrcpy, notes="On-demand Android display/control backend.",
            ),
            "adb": IntegrationInfo(
                "adb", "Android platform tools", "adb.exe",
                bool(adb), adb, notes="Used only when Android control is requested.",
            ),
            "pyatv": IntegrationInfo(
                "pyatv", "postlund/pyatv", python_module=pyatv_installed,
                notes="Lazy-loaded Apple TV/AirPlay discovery and remote control.",
            ),
            "matter": IntegrationInfo(
                "matter", "project-chip/connectedhomeip", "chip-tool.exe",
                bool(chip_tool), chip_tool,
                notes="On-demand Matter controller bridge.",
            ),
            "winsw": IntegrationInfo(
                "winsw", "winsw/winsw", "WinSW.exe",
                bool(winsw), winsw,
                notes="Optional Windows service wrapper for deployment.",
            ),
        }

    def discover(self, *, force: bool = False) -> dict[str, IntegrationInfo]:
        now = time.monotonic()
        with self._lock:
            if not force and self._cache and now - self._cached_at < self._DISCOVERY_TTL:
                return dict(self._cache)
            self._cache = self._discover()
            self._cached_at = now
            return dict(self._cache)

    def info(self, key: str) -> IntegrationInfo:
        return self.discover().get(
            key,
            IntegrationInfo(key, "unknown", notes="Integration is not registered."),
        )

    def status(self) -> dict[str, Any]:
        data = self.discover()
        return {
            "available": [key for key, item in data.items() if item.installed or item.python_module],
            "integrations": {
                key: asdict(item)
                for key, item in data.items()
            },
        }

    def performance_telemetry(
        self,
        *,
        game_active: bool,
        foreground_pid: int | None,
    ) -> dict[str, Any]:
        """Collect external performance data only when it has a clear payoff."""
        result: dict[str, Any] = {}

        lhm_data = _http_json("http://127.0.0.1:8085/data.json")
        if isinstance(lhm_data, dict):
            sensors = _walk_sensors(lhm_data)

            def sensor_match(predicate):
                matches = [
                    item for item in sensors
                    if predicate(
                        str(item.get("Type") or "").lower(),
                        str(item.get("Text") or item.get("Name") or "").lower(),
                        str(item.get("Identifier") or item.get("SensorId") or "").lower(),
                    )
                ]
                values = []
                for item in matches:
                    try:
                        value = float(item.get("Value"))
                    except (TypeError, ValueError):
                        continue
                    values.append(value)
                return values

            cpu_temp = sensor_match(
                lambda typ, name, ident: typ in {"temperature", "temperature sensor"}
                and ("cpu" in name or "cpu" in ident or "package" in name)
            )
            gpu_temp = sensor_match(
                lambda typ, name, ident: typ in {"temperature", "temperature sensor"}
                and ("gpu" in name or "gpu" in ident or "nvidia" in name or "amd" in name)
            )
            if cpu_temp:
                result["cpu_temperature_c"] = max(cpu_temp)
            if gpu_temp:
                result["gpu_temperature_c_lhm"] = max(gpu_temp)

        presentmon = self.info("presentmon")
        now = time.monotonic()
        if (
            game_active
            and foreground_pid
            and presentmon.installed
            and now - self._last_presentmon_at >= 20.0
        ):
            sampled = self._sample_presentmon(presentmon.path, int(foreground_pid))
            self._last_presentmon_at = now
            self._last_presentmon = sampled
        if self._last_presentmon:
            result.update(self._last_presentmon)
        return result

    @staticmethod
    def _sample_presentmon(path: str | None, pid: int) -> dict[str, Any] | None:
        if not path or pid <= 0:
            return None
        command = [
            path,
            "--process_id", str(pid),
            "--output_stdout",
            "--no_track_input",
            "--no_console_stats",
            "--qpc_time_ms",
            "--timed", "1",
            "--terminate_after_timed",
        ]
        completed = _run_command(command, timeout=3.0)
        if not completed or completed.returncode not in (0, 1):
            return None
        text = completed.stdout or ""
        header_index = -1
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if "Application" in line and "," in line and ("Frame" in line or "Present" in line):
                header_index = index
                break
        if header_index < 0:
            return None

        try:
            reader = csv.DictReader(io.StringIO("\n".join(lines[header_index:])))
            frame_times: list[float] = []
            for row in reader:
                for key in (
                    "MsBetweenPresents",
                    "FrameTime",
                    "FrameTimeMs",
                    "msBetweenPresents",
                ):
                    value = row.get(key)
                    if value is None:
                        continue
                    try:
                        number = float(str(value).strip())
                    except ValueError:
                        continue
                    if 0.05 <= number <= 1000.0:
                        frame_times.append(number)
                        break
            if not frame_times:
                return None
            frame_times.sort()
            avg = sum(frame_times) / len(frame_times)
            p95 = frame_times[min(len(frame_times) - 1, max(0, int(len(frame_times) * 0.95) - 1))]
            return {
                "frame_time_ms": round(avg, 3),
                "frame_time_p95_ms": round(p95, 3),
                "fps": round(1000.0 / avg, 2) if avg > 0 else None,
                "frame_samples": len(frame_times),
                "telemetry_source": "PresentMon",
            }
        except Exception:
            return None

    def apply_fancyzones_layout(self, layout: str, monitor: int | None = None) -> dict[str, Any]:
        info = self.info("fancyzones")
        if not info.installed or not info.path:
            return {"ok": False, "backend": "brahma", "error": "FancyZonesCLI is not installed."}
        args = [info.path, "set", str(layout)]
        if monitor is None:
            args.append("--all")
        else:
            args.extend(["--monitor", str(int(monitor))])
        completed = _run_command(args, timeout=5.0)
        if not completed:
            return {"ok": False, "backend": "fancyzones", "error": "FancyZonesCLI did not start."}
        return {
            "ok": completed.returncode == 0,
            "backend": "fancyzones",
            "layout": layout,
            "monitor": monitor,
            "stdout": (completed.stdout or "").strip()[-1000:],
            "stderr": (completed.stderr or "").strip()[-1000:],
        }

    def layout_backend(self) -> str:
        data = self.discover()
        if data.get("fancyzones", IntegrationInfo("", "")).installed:
            return "fancyzones"
        if data.get("fancywm", IntegrationInfo("", "")).installed:
            return "fancywm-cooperative"
        return "brahma"

    def android_devices(self) -> list[dict[str, str]]:
        info = self.info("adb")
        if not info.installed or not info.path:
            return []
        completed = _run_command([info.path, "devices", "-l"], timeout=3.0)
        if not completed or completed.returncode != 0:
            return []
        devices: list[dict[str, str]] = []
        for line in (completed.stdout or "").splitlines()[1:]:
            parts = line.strip().split()
            if len(parts) < 2 or parts[1] != "device":
                continue
            serial = parts[0]
            metadata = " ".join(parts[2:])
            devices.append({"serial": serial, "metadata": metadata})
        return devices

    def open_android(self, serial: str, *, title: str | None = None) -> dict[str, Any]:
        scrcpy = self.info("scrcpy")
        adb = self.info("adb")
        serial = str(serial or "").strip()
        if not serial or not scrcpy.installed or not scrcpy.path:
            return {"ok": False, "error": "scrcpy is not installed or no Android serial was supplied."}
        if adb.installed:
            known = {item["serial"] for item in self.android_devices()}
            if serial not in known:
                return {"ok": False, "error": f"Android device '{serial}' is not currently connected."}
        args = [scrcpy.path, "--serial", serial]
        if title:
            args.extend(["--window-title", title])
        try:
            subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return {"ok": True, "serial": serial, "backend": "scrcpy"}
        except Exception as exc:
            return {"ok": False, "backend": "scrcpy", "error": str(exc)}

    def apple_tv_scan(self) -> list[dict[str, Any]]:
        if importlib.util.find_spec("pyatv") is None:
            return []
        try:
            module = __import__("pyatv")
            atvs = asyncio.run(module.scan(asyncio.get_running_loop()))
        except RuntimeError:
            try:
                import pyatv
                loop = asyncio.new_event_loop()
                try:
                    atvs = loop.run_until_complete(pyatv.scan(loop))
                finally:
                    loop.close()
            except Exception:
                return []
        except Exception:
            return []
        results = []
        for config in atvs or []:
            results.append({
                "name": str(getattr(config, "name", "") or ""),
                "address": str(getattr(config, "address", "") or ""),
                "identifier": str(getattr(config, "identifier", "") or ""),
                "device_info": str(getattr(config, "device_info", "") or ""),
            })
        return results

    def matter_available(self) -> bool:
        return self.info("matter").installed

    def matter_help(self) -> dict[str, Any]:
        info = self.info("matter")
        if not info.installed or not info.path:
            return {"ok": False, "error": "chip-tool is not installed."}
        completed = _run_command([info.path], timeout=3.0)
        return {
            "ok": bool(completed and completed.returncode in (0, 1)),
            "backend": "chip-tool",
            "output": ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()[-5000:]
            if completed else "",
        }

    def procgovernor_validate(self, config_path: str) -> dict[str, Any]:
        info = self.info("procgovernor")
        if not info.installed or not info.path:
            return {"ok": False, "error": "ProcGovernor is not installed."}
        path = str(Path(config_path).expanduser())
        completed = _run_command(
            [info.path, "-validate", "-config", path],
            timeout=8.0,
        )
        return {
            "ok": bool(completed and completed.returncode == 0),
            "backend": "procgovernor",
            "config": path,
            "stdout": (completed.stdout or "").strip()[-2000:] if completed else "",
            "stderr": (completed.stderr or "").strip()[-2000:] if completed else "",
        }

    def winsw_status(self, config_path: str) -> dict[str, Any]:
        info = self.info("winsw")
        if not info.installed or not info.path:
            return {"ok": False, "error": "WinSW is not installed."}
        path = str(Path(config_path).expanduser())
        completed = _run_command([info.path, "status", path], timeout=5.0)
        return {
            "ok": bool(completed and completed.returncode == 0),
            "backend": "winsw",
            "config": path,
            "stdout": (completed.stdout or "").strip()[-2000:] if completed else "",
            "stderr": (completed.stderr or "").strip()[-2000:] if completed else "",
        }


integrations = ThirdPartyIntegrationHub()
