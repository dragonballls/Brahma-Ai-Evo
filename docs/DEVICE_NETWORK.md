# Brahma Evo Device Network

Brahma Evo now exposes one persistent Device Manager for wireless device control.

## Architecture

The device network normalizes devices into a common record:

- device_id, name, device_type, status, mode
- network address, serial, optional MAC
- adapter backend and capabilities
- optional control_url
- auto-reconnect policy and timestamps

## Adapters

| Device | Backend | In-app screen | Control |
|---|---|---:|---:|
| Android phone/tablet | ADB + scrcpy | Yes | Yes |
| Apple TV / AirPlay | pyatv | No | Yes when paired/supported |
| Matter device | chip-tool capability bridge | Backend-specific | Backend-specific |
| Generic PC/TV | explicit web control URL | Yes when URL supplies a UI | Backend-specific |
| WOL-capable device | Wake-on-LAN | N/A | Wake |

The holographic workspace uses movable/resizable Brahma-owned panels. Android scrcpy windows are reparented into those panels when safe; Brahma does not intentionally leave a separate scrcpy window open after a successful embed.

## Modes

Visible means the device has an active in-app panel.

Background means the screen surface is hidden while a supported network connection may remain available. Closing a device panel is therefore a visual action, not an implicit forget/disconnect operation.

Connected / Standby / Waking / Offline are explicit states. A device is not reported as connected merely because it has a stored configuration.

## Automatic reconnect

Reconnect is intentionally limited to devices that were explicitly paired with a reconnectable network endpoint. Brahma does not perform continuous subnet-wide probing.

Android TCP/IP devices can be reconnected through ADB when the endpoint is known. Wake-on-LAN sends a single magic packet only when the user explicitly requests a wake action.

## Pairing

Android wireless debugging still requires normal device-side authorization. Brahma can issue the explicit ADB pair/connect commands but does not bypass Android authorization.

## AI command surface

The device_manager tool supports: status, scan, capabilities, pair, pair_android, connect, show, background, wake, rename, forget, command, and place.

Examples include "Show my phone.", "Keep my phone connected in the background.", "Show my tablet beside it.", "Wake the living room PC.", "Turn the TV volume up.", and "Move Phone 1 to the right and make it smaller."

## Reference projects and reuse policy

The implementation follows the repository's GitHub-first policy. scrcpy is the native Android display/control backend; its current connection documentation supports multiple devices selected by serial, including TCP/IP serials. Android-Web-Control is a reference for a browser-native WebRTC/WebCodecs/ADB surface and is not copied wholesale. pyatv provides Apple TV/AirPlay control but is not an Apple TV screen-mirroring backend. connectedhomeip/chip-tool is the Matter controller reference/backend. Home Assistant Core is the architectural reference for normalized device/entity abstraction.

No third-party project is automatically downloaded, elevated, or made a hard startup dependency.