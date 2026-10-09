# Brahma Evo Accounts & Integrations

This document describes the shared account integration framework added in core/account_integrations.py. It is designed to become the common boundary between natural-language commands, provider adapters, credentials, and result reporting. It does not imply that every provider already has a live connector.

## Current verified implementation boundary

| Provider | Current state | Implemented operations |
|---|---|---|
| GitHub | Limited support; live OAuth flow is implemented | OAuth device authorization; validate account identity; test authorization; read the connected profile; list up to 30 public owned repositories |
| Roblox | Limited support; informational catalog only | No authenticated connector is claimed in this release |
| Amazon | Limited support; informational catalog only | No private account, order, checkout, or purchase operations in the shared account connector |
| Google Workspace | Existing legacy path; not migrated | Existing Gmail, Calendar, and Drive paths remain separate |
| YouTube | Existing legacy path; not migrated | Existing video features remain separate; this connector does not claim authorized-channel or publishing operations |
| Instagram | Existing legacy path; not migrated | Existing browser/Instagram code remains separate; no shared-account adapter is claimed |
| Outlook / Microsoft 365 | Unavailable in the shared connector | No adapter is implemented |

A provider appearing in the catalog is not evidence that it is connected. A capability is exposed only if an executable adapter declares it. A connected status is stored only after provider authentication and identity validation pass.

## Architecture

- core/account_integrations.py defines the provider manifest, capability/risk taxonomy, result and status contracts, typed errors, registry, bounded activity history, and secure-store protocol.
- WindowsCredentialManager uses Windows Credential Manager through pywin32. If the store is unavailable, account connection fails closed. There is intentionally no plaintext token-file fallback. MemoryCredentialStore is for tests only and is never selected as a production fallback.
- GitHubConnector implements GitHub OAuth device authorization with the minimum read:user scope. It exposes read-only identity/profile, connection-test, and public-owned-repository operations. API results are verified from actual HTTPS responses. Public repository output is filtered so records marked private are never returned.
- core/account_integrations_ui.py provides the Accounts & Integrations page. ui.py adds it to the existing Settings Hub navigation.
- features/account_integrations.py exposes safe provider/account/capability inspection and read-only GitHub queries to Brahma's existing dynamic feature registry. Starting OAuth and deleting saved credentials are routed back through explicit UI actions rather than model-controlled side effects.
- tests/test_account_integrations.py provides deterministic tests using injected requester and credential-store fakes. Mocked provider responses test logic, not live provider availability.

## Connect a GitHub account

1. Create or use a GitHub OAuth App in GitHub Developer Settings.
2. Enable Device Flow in that OAuth App's settings. Configure the app according to GitHub's current documentation: https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps.
3. In Brahma, open **Settings → Accounts & Integrations** and enter the OAuth App's public Client ID. The Client Secret field is optional and is only needed for supported refresh-token exchange; it is stored only in Windows Credential Manager along with the account record.
4. Select **Connect GitHub**. Brahma asks GitHub to issue a device code, then displays the short user code locally. Open https://github.com/login/device and enter that code directly on GitHub. Do not put passwords or authentication codes into Brahma chat.
5. Brahma polls only at the interval returned by GitHub, honors authorization denial and expiry, validates the token against GET /user, checks the granted profile scope, then stores the connected account identity in Windows Credential Manager.
6. Select the account and run **Test connection**, **Read profile**, or **List public repositories**. The UI displays the actual operation status and response evidence.
7. To disconnect, choose the account and confirm **Disconnect**. This deletes Brahma's local credential record. It does not revoke the provider-side grant; use the provider's Authorized Apps settings to revoke access remotely if desired.

GitHub device authorization requires a configured OAuth App with Device Flow enabled. Brahma does not create OAuth applications for the user, bypass MFA, reuse browser cookies, or accept passwords in chat. The connector does not request repo scope and does not provide private-repository access, issue/PR writes, workflow changes, releases, or arbitrary actions.

## Status and result semantics

- **Connected**: the adapter authenticated and validated the account identity.
- **Authentication Required**: the provider rejected or expired the saved authorization.
- **Missing Permission**: the account can be known but a requested operation's declared scope is not available.
- **Limited Support**: the provider has a catalog entry, but some or all described capabilities are not implemented by the shared executable adapter.
- **Unavailable**: there is no supported connector in the current build.
- **Disconnected**: the local account credential has been removed.

Action statuses distinguish rejected-before-execution, waiting for user authorization, waiting for confirmation, running, provider-accepted-but-unverified, succeeded-and-verified, failed, outcome-unknown, and unsupported. A result is never verified simply because a request was attempted. If a provider request may have succeeded but no trustworthy evidence was returned, do not blindly repeat a non-idempotent action.

Credentials and sensitive values are excluded from the bounded activity history. Provider-controlled result fields whose names look like token, secret, password, cookie, credential, authorization, or private-key data are removed before the result is returned to the conversation or UI.

## Adding an adapter

Create a provider-specific class implementing the Connector contract:

1. Add a stable provider_id, display name, official authorization method, documentation URL, and accurate support status to an IntegrationManifest.
2. Declare only operations supported by the provider's official API and document minimum scopes on each Capability. Keep read operations separate from reversible changes, messages/publications, financial actions, and destructive/security-sensitive actions.
3. Implement validate_credentials so it proves an account identity through an authoritative provider response. Never mark the account connected on the basis of a browser opening or a local token merely existing.
4. Implement health_check with provider evidence and stable typed errors. Implement execute only for manifest-declared operations. Do not expose a capability until the connector actually implements it.
5. Use request_json or an equivalent HTTPS client with bounded responses, strict timeouts, redaction, provider-specific rate limits, and safe error mapping. Never put secrets in exceptions, logs, test fixtures, or model prompts.
6. Store tokens only through the injected SecureCredentialStore; don't write tokens to ordinary config files. Implement a provider's official refresh/revoke method only if documented and testable. Local deletion must not be described as remote revocation.
7. Add deterministic tests for registration, auth denial/expiry, missing scope, malformed response, network/rate-limit handling, identity validation, permission enforcement, cancellation, uncertain outcomes, redaction, disconnect, and the connector's actual read-only operation. Use a provider sandbox or a dedicated, authorized test account for any live tests. Clearly label a live test skipped when no test account is configured.
8. Add the test module to .github/workflows/quality.yml, document setup and support limitations here, and verify the feature on the final candidate commit in Windows CI before claiming it works in a packaged build.

## Known limitations

- This is a foundation, not universal access to every website. Providers without an adapter remain limited/unavailable rather than receiving invented capabilities.
- GitHub authorization uses user-supplied OAuth App configuration. This implementation's automated tests use deterministic fake HTTP responses; a live account was not exercised by those tests.
- The Accounts & Integrations UI is a Windows Qt interface and its production secret store requires Windows Credential Manager. A platform without that facility fails closed instead of storing secrets unprotected.
- GitHub public repository listing is a bounded read (up to 30 results) rather than a fully paginated repository explorer.
- Existing Google Workspace, YouTube, and Instagram code must be audited and migrated separately before their features can be represented as unified connected accounts.
