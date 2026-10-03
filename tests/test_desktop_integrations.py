from __future__ import annotations

import subprocess
from unittest import TestCase
from unittest.mock import patch

from core.desktop.integrations import IntegrationInfo, ThirdPartyIntegrationHub


class IntegrationDiscoveryTests(TestCase):
    def test_status_exposes_all_external_backends(self):
        hub = ThirdPartyIntegrationHub()
        fake = {
            key: IntegrationInfo(key, project)
            for key, project in (
                ("procgovernor", "Prohect/ProcGovernor"),
                ("presentmon", "GameTechDev/PresentMon"),
                ("librehardwaremonitor", "LibreHardwareMonitor/LibreHardwareMonitor"),
                ("powertoys", "microsoft/PowerToys"),
                ("fancyzones", "microsoft/PowerToys"),
                ("fancywm", "FancyWM/fancywm"),
                ("scrcpy", "Genymobile/scrcpy"),
                ("adb", "Android platform tools"),
                ("pyatv", "postlund/pyatv"),
                ("bluetooth", "hbldh/bleak"),
                ("matter", "project-chip/connectedhomeip"),
                ("winsw", "winsw/winsw"),
            )
        }
        with patch.object(hub, "discover", return_value=fake):
            status = hub.status()
        self.assertEqual(set(status["integrations"]), set(fake))


class PresentMonTests(TestCase):
    def test_presentmon_csv_is_reduced_to_frame_metrics(self):
        hub = ThirdPartyIntegrationHub()
        csv_output = (
            "Application,ProcessID,MsBetweenPresents\n"
            "game.exe,123,16.0\n"
            "game.exe,123,17.0\n"
            "game.exe,123,18.0\n"
        )
        completed = subprocess.CompletedProcess(
            ["PresentMon.exe"], 0, stdout=csv_output, stderr=""
        )
        with patch("core.desktop.integrations._run_command", return_value=completed):
            result = hub._sample_presentmon("PresentMon.exe", 123)
        self.assertIsNotNone(result)
        self.assertEqual(result["frame_samples"], 3)
        self.assertAlmostEqual(result["frame_time_ms"], 17.0)
        self.assertGreater(result["fps"], 50.0)

    def test_missing_presentmon_fails_closed(self):
        hub = ThirdPartyIntegrationHub()
        self.assertIsNone(hub._sample_presentmon(None, 123))
        self.assertIsNone(hub._sample_presentmon("PresentMon.exe", 0))


class HardwareTelemetryTests(TestCase):
    def test_lhm_data_is_cached_and_mapped(self):
        hub = ThirdPartyIntegrationHub()
        lhm = {
            "Children": [
                {
                    "Type": "Temperature",
                    "SensorId": "/amdcpu/0/temperature/0",
                    "Text": "CPU Package",
                    "Value": "74.0",
                    "Children": [],
                },
                {
                    "Type": "Temperature",
                    "SensorId": "/nvidiagpu/0/temperature/0",
                    "Text": "GPU Core",
                    "Value": "68.0",
                    "Children": [],
                },
            ]
        }
        info = IntegrationInfo(
            "librehardwaremonitor",
            "LibreHardwareMonitor/LibreHardwareMonitor",
            installed=True,
            path="LibreHardwareMonitor.exe",
        )
        with patch.object(
            hub,
            "info",
            side_effect=lambda key: info if key == "librehardwaremonitor" else IntegrationInfo(key, key),
        ), patch(
            "core.desktop.integrations._http_json", return_value=lhm
        ) as fetch:
            first = hub.performance_telemetry(game_active=False, foreground_pid=None)
            second = hub.performance_telemetry(game_active=False, foreground_pid=None)
        self.assertEqual(first["cpu_temperature_c"], 74.0)
        self.assertEqual(first["gpu_temperature_c_lhm"], 68.0)
        self.assertEqual(second["gpu_temperature_c_lhm"], 68.0)
        fetch.assert_called_once()


class ExternalActionTests(TestCase):
    def test_fancyzones_command_is_only_run_when_available(self):
        hub = ThirdPartyIntegrationHub()
        info = IntegrationInfo(
            "fancyzones",
            "microsoft/PowerToys",
            "FancyZonesCLI.exe",
            installed=True,
            path="FancyZonesCLI.exe",
        )
        completed = subprocess.CompletedProcess(
            ["FancyZonesCLI.exe"], 0, stdout="ok", stderr=""
        )
        with patch.object(hub, "info", return_value=info), patch(
            "core.desktop.integrations._run_command", return_value=completed
        ) as run:
            result = hub.apply_fancyzones_layout("columns")
        self.assertTrue(result["ok"])
        run.assert_called_once_with(
            ["FancyZonesCLI.exe", "set", "columns", "--all"],
            timeout=5.0,
        )

    def test_android_parser_ignores_offline_devices(self):
        hub = ThirdPartyIntegrationHub()
        info = IntegrationInfo("adb", "Android platform tools", installed=True, path="adb.exe")
        completed = subprocess.CompletedProcess(
            ["adb.exe"],
            0,
            stdout=(
                "List of devices attached\n"
                "ABC123 device product:demo model:DemoDevice transport_id:1\n"
                "OFFLINE offline transport_id:2\n"
            ),
            stderr="",
        )
        with patch.object(hub, "info", return_value=info), patch(
            "core.desktop.integrations._run_command", return_value=completed
        ):
            devices = hub.android_devices()
        self.assertEqual(
            devices,
            [{
                "serial": "ABC123",
                "metadata": "product:demo model:DemoDevice transport_id:1",
            }],
        )

    def test_android_open_is_on_demand(self):
        hub = ThirdPartyIntegrationHub()
        scrcpy = IntegrationInfo("scrcpy", "Genymobile/scrcpy", installed=True, path="scrcpy.exe")
        adb = IntegrationInfo("adb", "Android platform tools", installed=False)
        with patch.object(
            hub, "info", side_effect=lambda key: scrcpy if key == "scrcpy" else adb
        ), patch("core.desktop.integrations.subprocess.Popen") as popen:
            result = hub.open_android("ABC123", title="Brahma • ABC123")
        self.assertTrue(result["ok"])
        popen.assert_called_once()

    def test_procgovernor_and_winsw_reject_missing_config_without_execution(self):
        hub = ThirdPartyIntegrationHub()
        proc = IntegrationInfo(
            "procgovernor", "Prohect/ProcGovernor", installed=True, path="ProcGovernor.exe"
        )
        win = IntegrationInfo("winsw", "winsw/winsw", installed=True, path="WinSW.exe")
        with patch.object(
            hub,
            "info",
            side_effect=lambda key: proc if key == "procgovernor" else win,
        ), patch("core.desktop.integrations._run_command") as run:
            self.assertFalse(hub.procgovernor_validate("")["ok"])
            self.assertFalse(hub.winsw_status("")["ok"])
            run.assert_not_called()

    def test_lhm_and_presentmon_have_low_polling_defaults(self):
        hub = ThirdPartyIntegrationHub()
        self.assertEqual(hub._DISCOVERY_TTL, 15.0)
        self.assertEqual(hub._last_presentmon_at, 0.0)
        self.assertEqual(hub._last_lhm_at, 0.0)
