# Brahma Evo Accounts & Integrations

This document describes the shared account integration framework, dynamic custom-provider onboarding UI, and generic OAuth/OpenAPI connector layer. Provider presence in a catalog does not imply live credentials or working operations.

## Current verified implementation boundary

| Provider | Current state | Implemented operations |
|---|---|---|
| GitHub | Limited support; live OAuth flow is implemented | OAuth device authorization; validate account identity; test authorization; read the connected profile; list up to 30 public owned repositories |
| Roblox | Limited support; OAuth2/PKCE adapter implemented (live account not yet verified) | Authorization-code + PKCE, exact loopback callback/state validation, userinfo identity validation, connection test, basic profile read when scope is granted, refresh-token rotation, and documented token revocation |
| Amazon consumer account | Unsupported | Ordinary consumer login is not an API integration; no password collection or browser-session workaround is provided |
| Amazon Selling Partner API (SP-API) | Not implemented | Requires an approved SP-API application and authorized seller account; Login with Amazon authorization plus API roles are required. The current connector does not implement Amazon-specific request signing or seller workflows |
| Google Workspace | Existing legacy path; not migrated | Existing Gmail, Calendar, and Drive paths remain separate |
| YouTube | Existing legacy path; not migrated | Existing video features remain separate; this connector does not claim authorized-channel or publishing operations |
| Instagram | Existing legacy path; not migrated | Existing browser/Instagram code remains separate; no shared-account adapter is claimed |
| Outlook / Microsoft 365 | Unavailable in the shared connector | No adapter is implemented |

A provider appearing in the catalog is not evidence that it is connected. A capability is exposed only if an executable adapter declares it. A connected status is stored only after provider authentication and identity validation pass.

## Provider manifest contract

Every executable provider manifest carries its stable ID/name, official documentation references, declared capabilities and scope requirements, required user configuration fields, support status, identity-validation method, supported token expiry/refresh/revocation features, pagination strategy, timeout, rate-limit and retry behavior, cancellation behavior, confirmation/verification policy, setup requirements, and explicit limitations. Individual operations declare risk, idempotency, required scopes, and whether they are supported. The provider-discovery response exposes this metadata so the assistant can explain a limitation without inventing an operation.

Manifest lifecycle fields describe what an adapter implements, not what every token/account is guaranteed to receive. For example, a connector may support refresh-token exchange but a specific OAuth grant may not issue a refresh token. The execution path must still verify the actual credential state and provider response.

## Architecture

- `core/account_integrations.py` defines the provider manifest, capability/risk taxonomy, result and status contracts, typed errors, registry, bounded activity history, and secure-store protocol.
- `core/universal_integrations.py` adds reusable OAuth2 authorization-code/PKCE, optional OIDC ID-token verification, OIDC discovery validation, header-based API-key/bearer authentication with account identity validation, configuration-field manifests, and a constrained OpenAPI 3.0/3.1 JSON preview that proposes authenticated GET operations while keeping mutation operations blocked. Query-string and cookie API-key authentication are intentionally disabled.
- WindowsCredentialManager uses Windows Credential Manager through pywin32. If the store is unavailable, account connection fails closed. There is intentionally no plaintext token-file fallback. MemoryCredentialStore is for tests only and is never selected as a production fallback.
- GitHubConnector implements GitHub OAuth device authorization with the minimum read:user scope. It exposes read-only identity/profile, connection-test, and public-owned-repository operations. API results are verified from actual HTTPS responses. Public repository output is filtered so records marked private are never returned.
- `core/account_integrations_ui.py` provides the Accounts & Integrations page and embeds the dynamic `core/custom_account_integrations_ui.py` provider form. `ui.py` adds it to the existing Settings Hub navigation.
- `features/account_integrations.py` exposes provider/account/capability inspection and read-only provider operations for exact registered action IDs. It retains explicit UI-only connection/disconnection routing and asks the user to identify an account when multiple matches exist.
- `core/custom_integration_registry.py` atomically persists non-secret provider configuration and reviewed OpenAPI JSON. OAuth client secrets are stored separately through the secure credential store. The default manager restores saved adapters at startup; missing secrets or malformed configuration do not become false connected states.
- `tests/test_account_integrations.py`, `tests/test_universal_integrations.py`, and `tests/test_custom_integration_registry.py` use deterministic HTTP and credential-store fakes. These tests validate logic only and are not proof of live provider availability.

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

## Custom provider onboarding (OAuth/OIDC and OpenAPI)

The generic module and Settings UI support documented OAuth2/OIDC and header-based API-key/bearer services that fit the current adapter contracts. This is not a universal magic login: each provider needs official API access, a client ID or credential, explicit trusted HTTPS hosts, the provider's actual scopes, an authoritative identity endpoint, and reviewed operations.

- OIDC discovery verifies the configured issuer exactly and validates trusted HTTPS endpoints. The generic ID-token validator supports the explicitly advertised RS256 and ES256 algorithms and checks the issuer, audience/authorized party, time claims, nonce, subject, JWKS signature, and matching UserInfo subject.
- OpenAPI onboarding currently accepts JSON OpenAPI 3.0/3.1 only. External references are blocked; one explicit HTTPS server must be declared; read candidates need a single explicit OAuth2/OIDC security scheme and scopes; deprecated or ambiguous/unparameterized operations are skipped. Mutation endpoints are previewed as unsupported and are never executable through the generic connector.
- Generated GET candidates are previews, not auto-enabled operations. Review the API terms, exact endpoint behavior, permissions, response schema, and provider rate limits; register only reviewed operations. Result verification means the provider returned a successful read response from the configured endpoint, not that every semantic claim in its data is independently true.
- The Settings → Accounts & Integrations page includes **Add a documented provider**. Enter its provider ID/name, authentication type, trusted hosts, documented identity endpoint (API key/bearer), official OAuth endpoints or OIDC issuer, exact registered redirect URI, minimum scopes, and OpenAPI JSON. Validate and preview, review candidate GET operations, explicitly approve them, and save. Configuration is restored after restart; only registered and declared operations appear as available actions. API keys and bearer tokens are entered separately in a masked field and stored only after provider identity validation. API keys are sent raw in their declared header; bearer credentials use the bearer scheme.
- Do not point the generic adapter at user-provided private/local addresses or untrusted hosts, and do not use generic discovery to guess endpoints that do not appear in provider documentation.

Example developer workflow:

1. Obtain the provider's official API specification and OAuth/OIDC setup instructions.
2. Validate issuer metadata with `discover_oidc_metadata` where the service is OIDC-compatible.
3. Use `analyze_openapi_spec` for a constrained read-only candidate preview, review the generated actions and required scopes, then convert only reviewed candidates with `operations_from_openapi`. For a single call that validates the explicit provider configuration, previews the supplied OpenAPI JSON, and builds the appropriate connector, use `configure_provider_connector(config, openapi_spec)`; this function still returns an unregistered preview, so review the manifest and explicitly register the connector only after approval.
4. Construct `OAuth2PKCEConnector` with the exact metadata and trusted hosts, register it via `IntegrationManager.register`, and run the generic contract tests plus provider-specific tests before shipping.
5. Add an adapter-specific settings/onboarding path that stores provider client configuration in appropriate secure/local settings and tokens only in the secure credential store. Do not ship user-supplied secrets in manifests, source, or logs.

## Adding an adapter

Create a provider-specific class implementing the `Connector` contract, or configure `OAuth2PKCEConnector` / `APIKeyConnector` when the service fits one of those safe standard paths:

1. Add a stable provider_id, display name, official authorization method, documentation URL, and accurate support status to an IntegrationManifest.
2. Declare only operations supported by the provider's official API and document minimum scopes on each Capability. Keep read operations separate from reversible changes, messages/publications, financial actions, and destructive/security-sensitive actions.
3. Implement validate_credentials so it proves an account identity through an authoritative provider response. Never mark the account connected on the basis of a browser opening or a local token merely existing.
4. Implement health_check with provider evidence and stable typed errors. Implement execute only for manifest-declared operations. Do not expose a capability until the connector actually implements it.
5. Use request_json or an equivalent HTTPS client with bounded responses, strict timeouts, redaction, provider-specific rate limits, and safe error mapping. Never put secrets in exceptions, logs, test fixtures, or model prompts.
6. Store tokens only through the injected SecureCredentialStore; don't write tokens to ordinary config files. Implement a provider's official refresh/revoke method only if documented and testable. Local deletion must not be described as remote revocation.
7. Add deterministic tests for registration, auth denial/expiry, missing scope, malformed response, network/rate-limit handling, identity validation, permission enforcement, cancellation, uncertain outcomes, redaction, disconnect, and the connector's actual read-only operation. Use a provider sandbox or a dedicated, authorized test account for any live tests. Clearly label a live test skipped when no test account is configured.
8. Add the test module to .github/workflows/quality.yml, document setup and support limitations here, and verify the feature on the final candidate commit in Windows CI before claiming it works in a packaged build.

## Amazon and provider-specific prerequisites

Amazon consumer accounts cannot be connected with an ordinary account password. Amazon SP-API is a separate integration requiring application registration, authorized selling-partner consent, permitted API roles, the current Login with Amazon token contract, and service-specific request handling. The generic header-token adapter does not implement SP-API authorization, token lifecycle, role-specific operations, restricted-operation requirements, or a seller workflow and must not be used as an SP-API substitute. Do not advertise Amazon as ready to connect until a specific official Amazon API/account type and authorized test environment have been implemented and verified. Official references: https://developer-docs.amazon/sp-api/lang-en_us/docs/authorizing-selling-partner-api-applications and https://developer-docs.amazon/sp-api/docs/connecting-to-the-selling-partner-api.

## Known limitations

- This does not provide universal access to every website. Custom onboarding works when a documented API fits OAuth2/PKCE, OIDC, or header API-key/bearer authentication and reviewed read-only GET operations. APIs needing custom signing, nonstandard authentication, write operations, or multi-step workflows require a reviewed provider-specific adapter.
- GitHub authorization uses user-supplied OAuth App configuration. Automated tests use deterministic fake HTTP responses; CI did not use a real third-party credential and does not claim live provider verification.
- The Accounts & Integrations UI is a Windows Qt interface and its production secret store requires Windows Credential Manager. A platform without that facility fails closed instead of storing secrets unprotected.
- GitHub public repository listing is a bounded read (up to 30 results) rather than a fully paginated repository explorer.
- Existing Google Workspace, YouTube, and Instagram code must be audited and migrated separately before their features can be represented as unified connected accounts.
