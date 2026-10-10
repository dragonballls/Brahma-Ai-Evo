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
- `features/account_integrations.py` exposes provider/account/capability inspection and exact registered reads. State-changing operations are directed to the trusted Accounts & Integrations UI confirmation flow, rather than approved automatically by a natural-language route.
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

## Custom provider onboarding (OAuth/OIDC, OpenAPI and GraphQL)

The built-in generic connector is designed for services with a documented HTTPS API, not for consumer website passwords or arbitrary browser scraping. It uses one reusable account manager and adapter contract, so an ordinary API fitting the supported contract can be configured without editing the central application's Python source.

- **Authentication:** documented OAuth 2.0 authorization-code/PKCE, configured OIDC discovery/ID-token verification, header API keys, or bearer tokens. API-key values are sent raw in their declared header; bearer-token auth is explicitly different. Providers requiring custom signatures or grants need a dedicated adapter. Refresh and revocation are advertised only where implemented by that adapter and provider.
- **OpenAPI import:** supports OpenAPI 3.0/3.1 JSON and YAML from pasted text, local files, or an explicitly approved HTTPS schema URL. YAML uses PyYAML's safe loader and rejects duplicate/non-string mapping keys and unsafe tags; duplicate JSON keys are rejected too. URL imports require exact hostname membership in `trusted_hosts`, are bounded to 2 MiB, and do not follow redirects or accept query-bearing URLs. Schema fetches never invoke operations. The document must declare one HTTPS API server and explicitly authenticated operations. External/cyclic references, anonymous operations, ambiguous authentication alternatives, query/cookie API keys, deprecated actions, unsafe paths, unsupported parameter styles, non-JSON write bodies, schema compositions, and dynamic write-body fields are rejected or skipped.
- **Review and approval:** preview is inert. Before saving, the UI presents all supported operation IDs, HTTP methods, risk classes, paths, unsupported mutations, and importer warnings in a scrollable review dialog. Saving enables only the reviewed capabilities; it neither connects an account nor makes an API request. Registered capabilities are sourced from the adapter manifest.
- **Arguments and execution:** path, query, and JSON-body arguments are validated against the declared schemas before network I/O. Undeclared fields and missing scopes are rejected. Reads and state-changing actions route through the same manager and provider adapter. Each state-changing action enters the manager's explicit confirmation queue and shows a separate confirmation dialog with the provider, operation, risk, and a redacted view of its exact arguments. Selecting No does not call the provider operation.
- **Results:** connection is not claimed until the configured identity endpoint verifies the provider-issued credential. HTTP write success is classified as accepted-but-unverified unless a separate supported readback proves the resulting state. Timeouts after sending a write can leave an uncertain remote outcome; Brahma avoids automatically repeating non-idempotent actions. Provider-side permissions and scope restrictions remain authoritative.
- **Persistence:** account credentials remain in protected credential storage; public custom-provider configuration and its reviewed OpenAPI JSON/YAML text are stored separately. On restart, connectors are restored from validated saved definitions and credentials are revalidated on connection test—saved configuration is not authentication evidence. A damaged/missing secret results in an attention/error state rather than a false connected state.
- **Supported operation methods:** GET, POST, PUT, PATCH and DELETE when the operation's auth scheme and bounded JSON schemas fit the generic contract. Operation risk is conservatively classified and the manager confirms non-read methods before sending them.
- **GraphQL:** Set protocol to graphql, configure the explicit HTTPS graphql_endpoint_url, supply SDL or introspection JSON, and list named query/mutation documents in graphql_operations. graphql-core validates the schema, operation documents and runtime variables before execution. Only explicitly declared operations are exposed; importing a schema never runs operations or fetches introspection automatically. Queries/mutations use bounded HTTPS JSON POST; mutations enter the same manager/UI confirmation queue as REST writes. OAuth2/OIDC PKCE and API-key/bearer headers reuse the authentication framework, with an explicit identity/UserInfo endpoint required for account validation.
- **Remaining protocol scope:** SOAP/WSDL, multipart/media upload, provider-specific signing, alternative OAuth grants and nonstandard response contracts require a separately tested adapter/plugin. Compatible services do not need vendor-specific UI rewrites.

Example custom API workflow:

1. Use the service's official documentation to obtain the API base URL, identity endpoint, authentication prerequisites, minimum scopes, and OpenAPI JSON/YAML description. Add only the documented hostnames to the explicit trusted-host list.
2. Configure the correct auth type and its documented fields. Do not use a consumer account password or assume an API key is a bearer token.
3. Select load a JSON/YAML schema file, paste schema text, or choose **Import from trusted HTTPS URL** after adding its exact host to `trusted_hosts`; select **Validate and preview**. The request preview does not call imported operations. Read the supported methods, scopes, input schemas, risk, and blocked-operation warnings.
4. Select **Approve listed capabilities** only after reviewing the exact list and provider requirements. This registers the selected spec; it does not connect credentials.
5. Connect the account with the official OAuth flow or provider-issued API credential. Brahma validates the provider's identity response and stores the credential in the protected credential store.
6. Choose a declared operation and supply a JSON object matching its schema. Non-read actions will first queue and display an explicit confirmation. After confirmation, Brahma shows the provider result as verified only when the connector has evidence that supports that status.

GitHub and Roblox first-party integrations remain limited to the precise operations and authentication details actually implemented by their adapters; capabilities are not inferred from the provider name alone. Automated CI uses controlled fixtures and does not imply successful live OAuth with a real account. Amazon consumer sign-in is distinct from Selling Partner API (SP-API); SP-API is not implemented as a ready connector. Its proper implementation needs registered application credentials, authorized seller/vendor consent, roles/permissions, LWA lifecycle, marketplace-specific endpoints, and service-specific signing/authorization. See the official [SP-API onboarding overview](https://developer-docs.amazon.com/sp-api/docs/onboarding-overview) and [connection guide](https://developer-docs.amazon.com/sp-api/docs/connecting-to-the-selling-partner-api).

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

Amazon consumer accounts cannot be connected with an ordinary account password. Amazon SP-API is a separate integration requiring application registration, authorized selling-partner consent, permitted API roles, the current Login with Amazon token contract, and service-specific request handling. The generic header-token adapter does not implement SP-API authorization, token lifecycle, role-specific operations, restricted-operation requirements, or a seller workflow and must not be used as an SP-API substitute. Do not advertise Amazon as ready to connect until a specific official Amazon API/account type and authorized test environment have been implemented and verified. Official references: https://developer-docs.amazon.com/sp-api/docs/authorizing-selling-partner-api-applications and https://developer-docs.amazon.com/sp-api/docs/connecting-to-the-selling-partner-api.

## Known limitations

- There is no honest promise of access to every website. The generic connector supports authenticated OpenAPI JSON/YAML APIs with GET/POST/PUT/PATCH/DELETE where auth, parameters, and bounded JSON schemas fit the implemented contract. OAuth2/PKCE, OIDC, header API-key, and bearer authentication are supported. SOAP/WSDL, multipart uploads, nonstandard signing, and unsupported schema compositions require a dedicated adapter.
- A write's HTTP response is reported as accepted-but-unverified unless a separate trustworthy readback verifies the changed state. A timeout after sending a non-idempotent request can leave the outcome unknown; Brahma does not blindly replay it.
- GitHub authorization uses user-supplied OAuth App configuration. Automated tests use deterministic fake HTTP responses; CI did not use a real third-party credential and does not claim live provider verification.
- The Accounts & Integrations UI is a Windows Qt interface and its production secret store requires Windows Credential Manager. A platform without that facility fails closed instead of storing secrets unprotected.
- GitHub public repository listing is a bounded read (up to 30 results) rather than a fully paginated repository explorer.
- Existing Google Workspace, YouTube, and Instagram code must be audited and migrated separately before their features can be represented as unified connected accounts.


## Cursor pagination and account recovery

Custom OpenAPI providers can opt into cursor pagination with a reviewed `x-brahma-pagination` extension on a documented GET operation. It declares an optional cursor query parameter, the JSON pointer for the item array, the next cursor, and optionally a boolean `has_more` pointer. A documented optional page-size parameter can also be configured. Cursor and page-size parameters are internal, cannot be overridden by callers, and are validated before registration. Each page is checked against the response schema and is fetched from the configured HTTPS origin without redirects. Invalid pointers, repeated cursors, malformed item arrays, and missing continuation tokens fail closed. Safety ceilings are 50 pages, 1,000 items per page and 5,000 total items; reaching a ceiling returns `truncated=true` and `has_more=true` instead of claiming a complete listing.

Operation discovery is bounded by the imported specification's 2 MiB and structural-node safety limits rather than a separate small provider/action count. Schema properties, GraphQL input fields, and action manifests use those shared structural limits; array, string, request-body and aggregate-response byte ceilings still prevent unbounded payloads. The custom-provider registry permits multiple definitions within a 32 MiB aggregate resource ceiling.

Persisted `Connected` status is not treated as current authentication after a restart. A restored account shows “Authorization not revalidated” until the provider validates the same saved identity in the current process. The first attempted operation triggers that check before any provider operation or state-changing confirmation is sent. Rejected or mismatched identities stop execution. Provider and operation pickers have text filters for names, IDs and capabilities.
