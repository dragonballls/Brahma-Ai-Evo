# Security Policy

## Supported Versions

Security fixes should be applied to the latest maintained version of this repository.

## Reporting a Vulnerability

If you discover a security issue, do not post secrets or exploit details in public issues.

Please report security concerns through a private channel or repository owner contact method.

## Responsible Disclosure

- Do not share API keys, bot tokens, or access credentials publicly.
- Revoke any exposed credentials immediately.
- Update local configuration files after rotating secrets.

## Brahma Evo Security Overview

This repository includes a security overview for Brahma Evo's current runtime model, gateway exposure, and authentication flow.

### Local credential handling

- API keys are persisted in the local config/api_keys.json configuration file (or the configured user-data location).
- New and migrated Windows API-key values use the dpapi: format and Windows DPAPI protection at rest.
- Non-Windows values use the portable:v1: format, backed by Fernet and a local .brahma-secret.key file with restrictive permissions. This is application-level local protection, not Windows DPAPI.
- Legacy unprefixed API-key values are upgraded during configuration loading on supported platforms. Migration uses the same temporary-file-and-replace write path as normal configuration updates; if migration cannot be written, the existing file is retained and the migration is retried on a later load.
- Keep this file private and never commit it to source control. Legacy plaintext values can remain on disk if a migration repeatedly fails.

### AI provider access

- Brahma Evo uses Gemini as a primary provider and OpenRouter as a fallback where configured.
- Provider keys are decoded in process when needed and sent to the respective provider clients.
- The on-disk file is not universally plaintext: protected prefixes and migration behavior differ by platform, and older unprefixed values may remain if migration fails.

### Brahma Connect gateway exposure

Brahma Connect is the local device gateway layer for Brahma Evo.

#### Configuration

- Default gateway config: `config/brahma_connect.json`
- Default host: `0.0.0.0`
- Default port: `8765`
- Default advertise: `true`
- Default pairing TTL: `300` seconds

Because the gateway binds to `0.0.0.0`, it is reachable from any interface on the host unless OS firewall rules block it.

#### Discovery

- Optional mDNS discovery is provided through `brahma_connect.gateway.discovery.GatewayDiscovery` and Zeroconf.
- Discovery advertises service `_BRAHMA._tcp.local.` only when the Zeroconf library is installed and `advertise` is enabled.

#### Local network consideration

- The gateway is designed as a local network transport.
- The gateway websocket uses TLS by default (`wss://`) with a per-installation certificate. Android pairing carries the certificate SHA-256 fingerprint and pins that certificate.

### Pairing and authentication flow

#### Pairing

- Pairing uses a temporary pairing offer created by `brahma_connect.gateway.pairing.PairingManager`.
- Each offer includes a `pairing_token`, a 6-digit `pairing_code`, and an expiration timestamp.
- Pairing offers expire after `pairing_ttl_seconds` (default 300 seconds).

#### Approval

- Incoming device connections begin with `HELLO` and create a pending request.
- A user must explicitly approve or reject each pending pairing request through the app.
- Approved devices are added to the registry and issued a permanent `device_secret`.

#### Device credentials

- Device records are stored in `config/brahma_connect/devices.json`.
- Device secrets are not stored plaintext; the repository stores a `secret_hash`.
- The `secret_hash` is computed using `hashlib.sha256(secret.encode('utf-8')).hexdigest()`.
- Authentication uses constant-time comparison (`hmac.compare_digest`) to avoid timing attacks.

#### Authentication

- Authenticated device connections use the websocket `/ws` endpoint and send an `AUTHENTICATE` message with `device_id` and `device_secret`.
- On successful authentication, the device is marked online and registered in the connection hub.
- Revoked devices are rejected by `DeviceManager.authenticate`.

### Gateway request handling

- The device-facing WebSocket endpoint is /ws and shares the gateway listener, which defaults to 0.0.0.0:8765.
- /health and /gateway/info do not apply the loopback peer restriction, so these informational endpoints may be reachable from other interfaces subject to TLS and firewall configuration.
- Device and pairing management endpoints—including /gateway/pair, device list/revoke/forget, logs, and pending-request approval/rejection—check the actual socket peer address and allow only loopback addresses.
- These management routes do not have a separate bearer-token layer. The loopback peer check is a boundary for local administration, not a substitute for OS account security or firewall controls.

### Firewall behavior

#### Dashboard firewall helper

- The local dashboard (`dashboard/server.py`) includes `_ensure_network_access`.
- This helper attempts to open a Windows firewall rule for dashboard ports and includes cross-platform stubs for macOS/Linux.
- The dashboard uses port `8000` by default and a legacy HTTPS alias on `8001`.

#### Gateway firewall behavior

- The gateway does not automatically open OS firewall ports.
- Because it binds to `0.0.0.0:8765`, administrators should verify and restrict firewall access manually if needed.

### Security strengths

- Pairing is explicit and requires user approval.
- Device secret handling uses hashed secrets and constant-time comparison.
- Temporary pairing codes expire quickly.
- Device revocation and forgetting are supported.
- The dashboard encrypts local commands using AES-256-CBC with a session-derived key.

### Security limitations

- Gateway TLS defaults to enabled (tls_enabled: true) and is configured on the shared listener, so it protects both WebSocket and REST traffic when enabled. Configuration can disable it; verify the active setting rather than assuming TLS is always on.
- The gateway device-facing listener defaults to 0.0.0.0:8765 and can be reached through host network interfaces unless firewall rules restrict it.
- Administrative and pairing-management routes use a loopback peer-address check, but no separate bearer-token authentication. /health and /gateway/info are informational exceptions and are not loopback-restricted.
- Windows API keys are protected at rest with DPAPI after migration. Non-Windows systems use the portable encrypted format and local key file; legacy plaintext can remain if migration fails.
- Device-facing security still depends on TLS remaining enabled, explicit pairing, device credentials, and appropriate OS firewall policy.

### Recommendations

- Keep `config/api_keys.json` private and out of version control.
- Use OS firewall rules to restrict access to port `8765` when Brahma Connect is enabled.
- Disable `advertise` in `config/brahma_connect.json` unless discovery is needed.
- Revoke lost or untrusted devices using `/gateway/devices/{device_id}/revoke`.
- Run Brahma Evo on a trusted local network.
- Keep tls_enabled enabled in config/brahma_connect.json; TLS applies to the shared gateway listener, including REST responses, when enabled.
