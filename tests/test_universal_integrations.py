from __future__ import annotations

import base64
import json
import time
import urllib.parse
from typing import Any, Mapping

import pytest

from core.account_integrations import (
    ActionStatus,
    HttpResponse,
    IntegrationError,
    IntegrationErrorCode,
    IntegrationManager,
    MemoryCredentialStore,
)
from core.universal_integrations import (
    APIKeyConnector,
    OAuth2PKCEConnector,
    OAuthProviderMetadata,
    analyze_openapi_spec,
    discover_oidc_metadata,
    operations_from_openapi,
)


def _openapi():
    return {
        "openapi": "3.1.0",
        "servers": [{"url": "https://api.example.test/v1"}],
        "components": {
            "securitySchemes": {
                "accountOAuth": {
                    "type": "oauth2",
                    "flows": {
                        "authorizationCode": {
                            "authorizationUrl": "https://auth.example.test/oauth/authorize",
                            "tokenUrl": "https://auth.example.test/oauth/token",
                            "scopes": {"people:read": "Read people"},
                        }
                    },
                }
            }
        },
        "security": [{"accountOAuth": ["people:read"]}],
        "paths": {
            "/users/{user_id}": {
                "parameters": [
                    {"name": "user_id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "get": {
                    "operationId": "getUser",
                    "summary": "Read a user",
                    "parameters": [
                        {"name": "fields", "in": "query", "schema": {"type": "string"}}
                    ],
                    "responses": {"200": {"description": "OK"}},
                },
                "patch": {
                    "operationId": "updateUser",
                    "summary": "Update a user",
                    "responses": {"200": {"description": "OK"}},
                },
            },
            "/unsafe": {
                "get": {
                    "operationId": "anonymousRead",
                    "summary": "No explicit auth",
                    "security": [],
                    "responses": {"200": {"description": "OK"}},
                }
            },
        },
    }


class FakeOAuthRequester:
    def __init__(self):
        self.calls: list[tuple[str, str, dict[str, Any], str | None]] = []
        self.token_scopes = "people:read"
        self.userinfo_status = 200
        self.userinfo = {"sub": "person-123", "name": "Demo Person"}
        self.api_status = 200
        self.jwks: dict[str, Any] = {"keys": []}

    def __call__(self, url, *, method="GET", form=None, token=None, timeout=8.0):
        values = dict(form or {})
        self.calls.append((url, method, values, token))
        if url.endswith("/oauth/token") and values.get("grant_type") == "authorization_code":
            return HttpResponse(200, {}, {
                "access_token": "test-access-token",
                "refresh_token": "test-refresh-token-1",
                "token_type": "Bearer",
                "expires_in": 900,
                "scope": self.token_scopes,
            })
        if url.endswith("/oauth/token") and values.get("grant_type") == "refresh_token":
            return HttpResponse(200, {}, {
                "access_token": "test-access-token-2",
                "refresh_token": "test-refresh-token-2",
                "token_type": "Bearer",
                "expires_in": 900,
                "scope": self.token_scopes,
            })
        if url.endswith("/oauth/revoke"):
            return HttpResponse(200, {}, {})
        if url.endswith("/oauth/userinfo"):
            return HttpResponse(self.userinfo_status, {}, dict(self.userinfo))
        if url.endswith("/.well-known/openid-configuration"):
            return HttpResponse(200, {}, {
                "issuer": "https://auth.example.test",
                "authorization_endpoint": "https://auth.example.test/oauth/authorize",
                "token_endpoint": "https://auth.example.test/oauth/token",
                "userinfo_endpoint": "https://auth.example.test/oauth/userinfo",
                "jwks_uri": "https://auth.example.test/oauth/jwks",
                "code_challenge_methods_supported": ["S256"],
                "response_types_supported": ["code"],
                "scopes_supported": ["openid", "profile"],
            })
        if url.endswith("/oauth/jwks"):
            return HttpResponse(200, {}, self.jwks)
        if "/users/" in url:
            return HttpResponse(self.api_status, {}, {"id": "target-user", "name": "Target User"})
        return HttpResponse(404, {}, {})


def _connector(requester=None, *, use_oidc=False, metadata=None, scopes=("people:read",)):
    requester = requester or FakeOAuthRequester()
    metadata = metadata or OAuthProviderMetadata(
        issuer="",
        authorization_endpoint="https://auth.example.test/oauth/authorize",
        token_endpoint="https://auth.example.test/oauth/token",
        userinfo_endpoint="https://auth.example.test/oauth/userinfo",
        revocation_endpoint="https://auth.example.test/oauth/revoke",
    )
    preview = analyze_openapi_spec(
        _openapi(),
        provider_id="sample",
        trusted_server_hosts=("api.example.test",),
    )
    operations = operations_from_openapi(preview)
    connector = OAuth2PKCEConnector(
        provider_id="sample",
        display_name="Sample Service",
        client_id="public-client",
        client_secret="private-client-secret",
        redirect_uri="http://127.0.0.1:8765/oauth/callback",
        metadata=metadata,
        api_base_url=preview["server_url"],
        operations=operations,
        requested_scopes=scopes,
        trusted_hosts=("auth.example.test", "api.example.test"),
        requester=requester,
        use_oidc=use_oidc,
    )
    return connector, requester


def _authorize(connector, requester):
    flow = connector.begin_authorization()
    query = urllib.parse.urlencode({"code": "single-use-code", "state": flow["state"]})
    credentials = connector.complete_authorization(flow, flow["redirect_uri"] + "?" + query)
    return flow, credentials


def test_openapi_preview_only_offers_explicitly_authenticated_get_operations():
    result = analyze_openapi_spec(_openapi(), provider_id="sample", trusted_server_hosts=("api.example.test",))
    assert [item["action"] for item in result["read_only_candidates"]] == ["sample.getUser"]
    assert result["read_only_candidates"][0]["required_scopes"] == ["people:read"]
    assert result["mutation_candidates"][0]["supported"] is False
    assert result["mutation_candidates"][0]["method"] == "PATCH"
    assert result["registered"] is False
    assert any("anonymousRead" in warning for warning in result["warnings"])


def test_openapi_preview_rejects_external_refs_and_insecure_servers():
    spec = _openapi()
    spec["components"]["schemas"] = {"User": {"$ref": "https://evil.example/schema.json"}}
    with pytest.raises(IntegrationError) as external:
        analyze_openapi_spec(spec, provider_id="sample", trusted_server_hosts=("api.example.test",))
    assert external.value.code == IntegrationErrorCode.INVALID_REQUEST

    spec = _openapi()
    spec["servers"] = [{"url": "http://api.example.test/v1"}]
    with pytest.raises(IntegrationError) as insecure:
        analyze_openapi_spec(spec, provider_id="sample")
    assert insecure.value.code == IntegrationErrorCode.INVALID_REQUEST


def test_openapi_preview_rejects_private_server_and_duplicate_operation_ids():
    spec = _openapi()
    spec["servers"] = [{"url": "https://127.0.0.1/v1"}]
    with pytest.raises(IntegrationError):
        analyze_openapi_spec(spec, provider_id="sample")

    spec = _openapi()
    spec["paths"]["/other"] = {
        "get": {"operationId": "getUser", "security": [{"accountOAuth": ["people:read"]}]}
    }
    result = analyze_openapi_spec(spec, provider_id="sample")
    assert len(result["read_only_candidates"]) == 1
    assert any("duplicate operationId" in warning for warning in result["warnings"])


def test_oidc_discovery_checks_issuer_pkce_and_trusted_endpoint_hosts():
    requester = FakeOAuthRequester()
    metadata = discover_oidc_metadata(
        "https://auth.example.test",
        trusted_hosts=("auth.example.test",),
        requester=requester,
    )
    assert metadata.issuer == "https://auth.example.test"
    assert metadata.jwks_uri == "https://auth.example.test/oauth/jwks"
    assert any(call[0].endswith("/.well-known/openid-configuration") for call in requester.calls)

    class MismatchRequester:
        def __call__(self, url, *, method="GET", form=None, token=None, timeout=8.0):
            return HttpResponse(200, {}, {
                "issuer": "https://wrong.example",
                "authorization_endpoint": "https://auth.example.test/oauth/authorize",
                "token_endpoint": "https://auth.example.test/oauth/token",
                "userinfo_endpoint": "https://auth.example.test/oauth/userinfo",
                "jwks_uri": "https://auth.example.test/oauth/jwks",
            })
    with pytest.raises(IntegrationError) as mismatch:
        discover_oidc_metadata("https://auth.example.test", requester=MismatchRequester())
    assert mismatch.value.code == IntegrationErrorCode.INVALID_AUTH_RESPONSE


def test_generic_oauth_pkce_connects_validates_identity_and_executes_only_reviewed_get():
    connector, requester = _connector()
    flow, credentials = _authorize(connector, requester)
    assert len(flow["code_verifier"]) >= 43
    authorization_query = urllib.parse.parse_qs(urllib.parse.urlparse(flow["authorization_url"]).query)
    assert authorization_query["code_challenge_method"] == ["S256"]
    assert authorization_query["state"] == [flow["state"]]
    assert "code_verifier" not in flow["authorization_url"]

    manager = IntegrationManager(store=MemoryCredentialStore(), adapters=[connector])
    account = manager.connect("sample", credentials)
    assert account.identity == "person-123"
    result = manager.execute(account.account_id, "sample.getUser", {"user_id": "target-user", "fields": "id,name"})
    assert result.status == ActionStatus.SUCCEEDED_VERIFIED
    assert result.result["id"] == "target-user"
    assert result.verification_evidence["http_status"] == 200
    assert any(url == "https://api.example.test/v1/users/target-user?fields=id%2Cname" for url, *_ in requester.calls)


def test_generic_oauth_rejects_forged_state_duplicate_codes_and_redirect_mismatch_before_exchange():
    connector, requester = _connector()
    flow = connector.begin_authorization()
    token_count = lambda: len([c for c in requester.calls if c[0].endswith("/oauth/token")])
    before = token_count()
    with pytest.raises(IntegrationError):
        connector.complete_authorization(flow, flow["redirect_uri"] + "?code=x&state=forged")
    with pytest.raises(IntegrationError):
        connector.complete_authorization(flow, flow["redirect_uri"] + "?code=x&code=y&state=" + flow["state"])
    with pytest.raises(IntegrationError):
        connector.complete_authorization(flow, "http://127.0.0.1:8766/oauth/callback?code=x&state=" + flow["state"])
    assert token_count() == before


def test_generic_oauth_rejects_undeclared_arguments_and_missing_scopes():
    connector, requester = _connector()
    _flow, credentials = _authorize(connector, requester)
    with pytest.raises(IntegrationError) as extra:
        connector.execute("sample.getUser", {"user_id": "target-user", "admin": True}, credentials)
    assert extra.value.code == IntegrationErrorCode.INVALID_REQUEST

    credentials["scopes"] = ["other:scope"]
    with pytest.raises(IntegrationError) as scope:
        connector.execute("sample.getUser", {"user_id": "target-user"}, credentials)
    assert scope.value.code == IntegrationErrorCode.MISSING_PERMISSION


def test_generic_oauth_maps_auth_permission_and_rate_limit_errors():
    connector, requester = _connector()
    _flow, credentials = _authorize(connector, requester)

    requester.userinfo_status = 401
    with pytest.raises(IntegrationError) as auth:
        connector.validate_credentials(credentials)
    assert auth.value.code == IntegrationErrorCode.AUTHENTICATION_REQUIRED

    requester.userinfo_status = 403
    with pytest.raises(IntegrationError) as permission:
        connector.validate_credentials(credentials)
    assert permission.value.code == IntegrationErrorCode.MISSING_PERMISSION

    requester.userinfo_status = 429
    with pytest.raises(IntegrationError) as rate:
        connector.validate_credentials(credentials)
    assert rate.value.code == IntegrationErrorCode.RATE_LIMITED


def test_generic_oauth_refresh_rotates_tokens_and_revokes_only_when_declared():
    connector, requester = _connector()
    _flow, credentials = _authorize(connector, requester)
    updated = connector.refresh_credentials(credentials)
    assert updated["access_token"] == "test-access-token-2"
    assert updated["refresh_token"] == "test-refresh-token-2"
    connector.revoke_credentials(updated)
    assert any(call[0].endswith("/oauth/revoke") for call in requester.calls)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def test_oidc_id_token_signature_audience_issuer_expiry_and_nonce_validation():
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_numbers = private_key.public_key().public_numbers()
    modulus = _b64url(public_numbers.n.to_bytes((public_numbers.n.bit_length() + 7) // 8, "big"))
    exponent = _b64url(public_numbers.e.to_bytes((public_numbers.e.bit_length() + 7) // 8, "big"))
    requester = FakeOAuthRequester()
    requester.jwks = {"keys": [{"kid": "signing-key", "kty": "RSA", "use": "sig", "alg": "RS256", "n": modulus, "e": exponent}]}
    metadata = OAuthProviderMetadata(
        issuer="https://auth.example.test",
        authorization_endpoint="https://auth.example.test/oauth/authorize",
        token_endpoint="https://auth.example.test/oauth/token",
        userinfo_endpoint="https://auth.example.test/oauth/userinfo",
        jwks_uri="https://auth.example.test/oauth/jwks",
    )
    connector, _ = _connector(requester, use_oidc=True, metadata=metadata, scopes=("openid", "profile", "people:read"))
    now = time.time()
    header = _b64url(json.dumps({"alg": "RS256", "kid": "signing-key"}).encode("utf-8"))
    claims = _b64url(json.dumps({
        "iss": metadata.issuer,
        "aud": "public-client",
        "sub": "person-123",
        "exp": now + 300,
        "iat": now,
        "nonce": "expected-nonce",
    }).encode("utf-8"))
    signing_input = (header + "." + claims).encode("ascii")
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    token = header + "." + claims + "." + _b64url(signature)
    assert connector._validate_id_token(token, "expected-nonce")["sub"] == "person-123"
    with pytest.raises(IntegrationError):
        connector._validate_id_token(token, "wrong-nonce")


def test_natural_language_router_invokes_only_registered_read_operations():
    from features.account_integrations import execute_with_manager

    connector, requester = _connector()
    _flow, credentials = _authorize(connector, requester)
    manager = IntegrationManager(store=MemoryCredentialStore(), adapters=[connector])
    account = manager.connect("sample", credentials)
    api_reads_before = len([call for call in requester.calls if "/users/" in call[0]])
    result = execute_with_manager(
        manager,
        action="execute",
        provider_id="sample",
        account_id=account.account_id,
        operation="sample.getUser",
        arguments={"user_id": "target-user", "fields": "name"},
    )
    assert result["status"] == "succeeded_and_verified"
    assert result["provider_id"] == "sample"

    unsupported = execute_with_manager(
        manager,
        action="execute",
        provider_id="sample",
        account_id=account.account_id,
        operation="sample.updateUser",
        arguments={"user_id": "target-user"},
    )
    assert unsupported["status"] == "unsupported"
    api_reads_after = len([call for call in requester.calls if "/users/" in call[0]])
    assert api_reads_after == api_reads_before


def _api_key_openapi():
    document = _openapi()
    document["components"]["securitySchemes"] = {
        "apiKeyHeader": {"type": "apiKey", "in": "header", "name": "X-API-Key"}
    }
    document["security"] = [{"apiKeyHeader": []}]
    return document


class FakeAPIKeyRequester:
    def __init__(self):
        self.calls = []
        self.status = 200

    def __call__(self, url, *, method="GET", api_key="", api_key_header="", timeout=8.0):
        self.calls.append((url, method, api_key_header, api_key))
        if url.endswith("/whoami"):
            return HttpResponse(self.status, {}, {"id": "api-key-user", "name": "Key User"})
        if "/users/" in url:
            return HttpResponse(self.status, {}, {"id": "target-user", "name": "Target User"})
        return HttpResponse(404, {}, {})


def test_openapi_preview_supports_header_api_keys_but_skips_query_api_keys():
    document = _api_key_openapi()
    preview = analyze_openapi_spec(document, provider_id="sample", trusted_server_hosts=("api.example.test",))
    assert [item["action"] for item in preview["read_only_candidates"]] == ["sample.getUser"]
    candidate = preview["read_only_candidates"][0]
    assert candidate["auth_type"] == "api_key"
    assert candidate["api_key_header"] == "X-API-Key"

    document["components"]["securitySchemes"]["apiKeyHeader"] = {
        "type": "apiKey", "in": "query", "name": "api_key"
    }
    rejected = analyze_openapi_spec(document, provider_id="sample", trusted_server_hosts=("api.example.test",))
    assert rejected["read_only_candidates"] == []
    assert any("query/cookie API keys" in warning for warning in rejected["warnings"])


def test_header_api_key_connector_validates_identity_and_runs_only_declared_get():
    requester = FakeAPIKeyRequester()
    preview = analyze_openapi_spec(
        _api_key_openapi(),
        provider_id="sample",
        trusted_server_hosts=("api.example.test",),
    )
    connector = APIKeyConnector(
        provider_id="sample",
        display_name="Sample API key service",
        identity_url="https://api.example.test/v1/whoami",
        api_base_url=preview["server_url"],
        api_key_header="X-API-Key",
        trusted_hosts=("api.example.test",),
        operations=operations_from_openapi(preview),
        identity_field="id",
        documentation_url="https://api.example.test/docs",
        requester=requester,
    )
    manager = IntegrationManager(store=MemoryCredentialStore(), adapters=[connector])
    account = manager.connect("sample", {"api_key": "do-not-log-this"})
    assert account.identity == "api-key-user"
    assert account.status.value == "Connected"

    result = manager.execute(
        account.account_id,
        "sample.getUser",
        {"user_id": "target-user", "fields": "name"},
    )
    assert result.status == ActionStatus.SUCCEEDED_VERIFIED
    assert result.result["name"] == "Target User"
    assert all(call[2] == "X-API-Key" for call in requester.calls)
    assert all(call[3] == "do-not-log-this" for call in requester.calls)
    assert "do-not-log-this" not in str(manager.activity_history())

    requester.status = 401
    failed_check = manager.test_connection(account.account_id)
    assert failed_check["ok"] is False
    assert failed_check["evidence"]["error_code"] == IntegrationErrorCode.AUTHENTICATION_REQUIRED.value
    assert failed_check["account"]["status"] == "Authentication Required"
