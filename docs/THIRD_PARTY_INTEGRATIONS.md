# Brahma Evo third-party integrations

Brahma Evo uses a GitHub-first integration model for desktop capabilities. These projects are consumed through optional installed binaries, documented local interfaces, or lazy Python imports rather than copied wholesale into Brahma.

| Project | Brahma use | License |
|---|---|---|
| Prohect/ProcGovernor | Optional native process scheduling backend / config validation | CC0-1.0 |
| GameTechDev/PresentMon | On-demand frame-time and graphics telemetry | MIT |
| LibreHardwareMonitor/LibreHardwareMonitor | Optional local sensor telemetry through its JSON web interface | MPL-2.0 |
| microsoft/PowerToys | FancyZones layout backend and Windows utility coexistence | MIT |
| FancyWM/fancywm | Cooperative dynamic window-manager detection | MIT |
| Genymobile/scrcpy | On-demand Android mirroring/control | Apache-2.0 |
| postlund/pyatv | Lazy Apple TV/AirPlay discovery and control | MIT |
| project-chip/connectedhomeip | Optional chip-tool Matter controller bridge | Apache-2.0 |
| winsw/winsw | Optional Windows-service deployment wrapper | MIT |

## Safety and compatibility

Third-party integrations are optional. A missing binary, unavailable Python module, failed device scan, or unsupported OS path returns a safe failure and leaves the normal Brahma path intact.

Brahma does not automatically install or elevate these projects. Mutating external operations are only invoked by an explicit Brahma tool request. Performance telemetry is cached so the integration layer does not create an always-on polling burden.

The current Windows performance engine remains the source of truth for reversible process adjustments. ProcGovernor is exposed as a validation/capability backend rather than automatically running a second governor alongside Brahma.

## Upstream sources

- https://github.com/Prohect/ProcGovernor
- https://github.com/GameTechDev/PresentMon
- https://github.com/LibreHardwareMonitor/LibreHardwareMonitor
- https://github.com/microsoft/PowerToys
- https://github.com/FancyWM/fancywm
- https://github.com/Genymobile/scrcpy
- https://github.com/postlund/pyatv
- https://github.com/project-chip/connectedhomeip
- https://github.com/winsw/winsw
