from __future__ import annotations

import time
from typing import Any, Mapping

import pytest

from core.account_integrations import (
    ActionStatus,
    Capability,
    ConnectionStatus,
    GitHubConnector,
    HttpResponse,
    IntegrationError,
    IntegrationErrorCode,
    IntegrationManager,
    IntegrationManifest,
    MemoryCredentialStore,
    RiskLevel,
    request_json,
)


class FakeConnector:
    def __init__(self, provider_id="sample", *, risk=RiskLevel.READ_ONLY, requires_scope=()):
        self.calls = []
        self.identity_number = 0
        self.manifest = IntegrationManifest(
            provider_id=provider_id,
            display_name=provider_id.title(),
            auth_method="test",
            capabilities=(
                Capability(f"{provider_id}.read", "Read test data", RiskLevel.READ_ONLY, tuple(requires_scope)),
                Capability(f"{provider_id}.write", "Change test data", risk),
            ),
            status=ConnectionStatus.LIMITED_SUPPORT,
        )

    def validate_credentials(self, credentials: Mapping[str, Any]):
        if credentials.get("reject"):
            raise IntegrationError(IntegrationErrorCode.AUTHENTICATION_REQUIRED, "Token rejected.")
        return str(credentials.get("identity") or "test-user"), tuple(credentials.get("scopes") or ())

    def health_check(self, credentials):
        return True, {"verified": True, "source": "test", "identity": credentials.get("identity", "test-user"), "http_status": 200}

    def execute(self, action, arguments, credentials):
        self.calls.append((action, dict(arguments)))
        return {"value": "verified data"}, {"verified": True, "source": "fake provider", "http_status": 200}


def _connected_manager(connector=None):
    store = MemoryCredentialStore()
    connector = connector or FakeConnector()
    manager = IntegrationManager(store=store, adapters=[connector])
    return manager, store, connector


def test_register_rejects_duplicate_provider_and_empty_action():
    connector = FakeConnector()
    manager, _, _ = _connected_manager(connector)
    with pytest.raises(IntegrationError) as duplicate:
        manager.register(FakeConnector())
    assert duplicate.value.code == IntegrationErrorCode.INVALID_REQUEST

    malformed = FakeConnector()
    malformed.manifest = IntegrationManifest(
        provider_id="malformed",
        display_name="Malformed",
        auth_method="test",
        capabilities=(Capability("", "empty"),),
    )
    with pytest.raises(IntegrationError):
        manager.register(malformed)


def test_connect_validates_identity_before_saving_any_secret():
    connector = FakeConnector()
    manager, store, _ = _connected_manager(connector)
    with pytest.raises(IntegrationError) as rejected:
        manager.connect("sample", {"access_token": "synthetic-secret", "reject": True})
    assert rejected.value.code == IntegrationErrorCode.AUTHENTICATION_REQUIRED
    assert store.list_records() == []

    account = manager.connect("sample", {"access_token": "synthetic-secret", "identity": "alice"})
    assert account.identity == "alice"
    assert account.status == ConnectionStatus.CONNECTED
    record = store.read(account.account_id)
    assert record["credentials"]["access_token"] == "synthetic-secret"


def test_multiple_accounts_for_same_provider_are_isolated():
    manager, _, _ = _connected_manager()
    first = manager.connect("sample", {"access_token": "token-a", "identity": "alice"})
    second = manager.connect("sample", {"access_token": "token-b", "identity": "bob"})

    assert first.account_id != second.account_id
    assert [item.identity for item in manager.list_accounts("sample")] == ["alice", "bob"]
    assert manager.store.read(first.account_id)["credentials"]["access_token"] == "token-a"
    assert manager.store.read(second.account_id)["credentials"]["access_token"] == "token-b"


def test_connect_fails_closed_when_required_scope_is_missing():
    connector = FakeConnector(requires_scope=("read:user",))
    manager, store, _ = _connected_manager(connector)
    with pytest.raises(IntegrationError) as exc:
        manager.connect("sample", {"access_token": "token", "identity": "alice", "scopes": []})
    assert exc.value.code == IntegrationErrorCode.MISSING_PERMISSION
    assert store.list_records() == []


def test_unsupported_operations_never_reach_connector():
    manager, _, connector = _connected_manager()
    account = manager.connect("sample", {"access_token": "token"})
    result = manager.execute(account.account_id, "sample.delete_everything")
    assert result.status == ActionStatus.UNSUPPORTED
    assert result.error_code == IntegrationErrorCode.UNSUPPORTED_ACTION.value
    assert connector.calls == []


def test_success_requires_explicit_provider_verification_evidence():
    manager, _, connector = _connected_manager()
    account = manager.connect("sample", {"access_token": "token"})
    result = manager.execute(account.account_id, "sample.read")

    assert result.status == ActionStatus.SUCCEEDED_VERIFIED
    assert result.verification_evidence["http_status"] == 200
    assert result.to_dict()["status"] == "succeeded_and_verified"
    assert len(connector.calls) == 1


def test_idempotency_key_prevents_repeating_the_same_idempotent_action():
    manager, _, connector = _connected_manager()
    account = manager.connect("sample", {"access_token": "token"})
    first = manager.execute(account.account_id, "sample.read", {"query": "x"}, idempotency_key="request-1")
    second = manager.execute(account.account_id, "sample.read", {"query": "x"}, idempotency_key="request-1")

    assert first == second
    assert len(connector.calls) == 1


def test_sensitive_or_mutating_action_waits_for_ui_confirmation():
    manager, _, connector = _connected_manager()
    account = manager.connect("sample", {"access_token": "token"})
    pending = manager.execute(account.account_id, "sample.write", {"body": "hello"})
    assert pending.status == ActionStatus.WAITING_FOR_CONFIRMATION
    assert pending.confirmation_id
    assert connector.calls == []

    rejected = manager.confirm_action(pending.confirmation_id, approved=False)
    assert rejected.status == ActionStatus.REJECTED
    assert connector.calls == []


def test_confirmed_action_runs_once_and_reports_verified_result():
    manager, _, connector = _connected_manager()
    account = manager.connect("sample", {"access_token": "token"})
    pending = manager.execute(account.account_id, "sample.write", {"setting": "value"})
    result = manager.confirm_action(pending.confirmation_id, approved=True)
    assert result.status == ActionStatus.SUCCEEDED_VERIFIED
    assert len(connector.calls) == 1


def test_unknown_confirmation_cannot_execute():
    manager, _, connector = _connected_manager()
    result = manager.confirm_action("invented-confirmation-id", approved=True)
    assert result.status == ActionStatus.REJECTED
    assert connector.calls == []


def test_disconnect_deletes_local_credential_and_blocks_later_actions():
    manager, _, connector = _connected_manager()
    account = manager.connect("sample", {"access_token": "synthetic-access-token"})
    result = manager.disconnect(account.account_id)

    assert result["ok"] is True
    assert result["provider_revocation"] == "not_performed"
    assert manager.list_accounts() == []
    action = manager.execute(account.account_id, "sample.read")
    assert action.status == ActionStatus.FAILED
    assert connector.calls == []


def test_connection_test_reports_authoritative_identity_and_history_is_bounded():
    manager, _, _ = _connected_manager()
    account = manager.connect("sample", {"access_token": "synthetic-access-token", "identity": "alice"})
    result = manager.test_connection(account.account_id)
    assert result["ok"] is True
    assert result["evidence"]["identity"] == "alice"
    assert manager.activity_history()[0]["action"] == "account.connect"
    assert manager.activity_history()[-1]["action"] == "connection.test"


def test_result_redacts_github_access_token_patterns():
    from core.account_integrations import ActionResult
    result = ActionResult(
        ActionStatus.FAILED,
        "github",
        "fake-account-id",
        "github.profile.read",
        message="Authorization failed for gho_" + "x" * 32,
    ).to_dict()
    assert "gho_" not in result["message"]
    assert "[REDACTED]" in result["message"]


class GithubRequester:
    def __init__(self, *, token_outcome=None, profile_status=200, profile_scopes="read:user", repositories=None):
        self.calls = []
        self.token_outcome = token_outcome or {
            "access_token": "gho_" + "T" * 30,
            "scope": "read:user",
            "token_type": "bearer",
        }
        self.profile_status = profile_status
        self.profile_scopes = profile_scopes
        self.repositories = repositories or [
            {"name": "public-one", "full_name": "demo/public-one", "private": False, "html_url": "https://github.com/demo/public-one"},
            {"name": "private-one", "full_name": "demo/private-one", "private": True, "html_url": "https://github.com/demo/private-one"},
        ]

    def __call__(self, url, *, method="GET", form=None, token=None, timeout=8.0):
        self.calls.append((url, method, dict(form or {}), token))
        if url.endswith("/login/device/code"):
            return HttpResponse(200, {}, {
                "device_code": "device-secret-not-for-logs",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://github.com/login/device",
                "expires_in": 900,
                "interval": 5,
            })
        if url.endswith("/login/oauth/access_token"):
            return HttpResponse(200, {}, dict(self.token_outcome))
        if url.endswith("/user"):
            return HttpResponse(self.profile_status, {"X-OAuth-Scopes": self.profile_scopes}, {"login": "demo-user", "id": 123, "name": "Demo User"})
        if "/users/demo-user/repos" in url:
            return HttpResponse(200, {}, list(self.repositories))
        return HttpResponse(404, {}, {"message": "not found"})


def test_github_device_flow_requires_real_provider_validation_before_connecting():
    requester = GithubRequester()
    connector = GitHubConnector(requester=requester)
    store = MemoryCredentialStore()
    manager = IntegrationManager(store=store, adapters=[connector])

    flow = connector.begin_device_authorization("public-client-id")
    assert flow["user_code"] == "ABCD-EFGH"
    assert flow["verification_uri"] == "https://github.com/login/device"
    assert flow["device_code"] == "device-secret-not-for-logs"
    flow["next_poll_at"] = 0
    outcome = connector.poll_device_authorization(flow)
    account = manager.connect("github", outcome["credentials"])

    assert account.identity == "demo-user"
    assert account.status == ConnectionStatus.CONNECTED
    assert len(manager.list_accounts("github")) == 1
    assert "device-secret-not-for-logs" not in str(manager.activity_history())
    assert "gho_" not in str(manager.activity_history())


def test_github_public_repository_action_filters_private_entries():
    requester = GithubRequester()
    connector = GitHubConnector(requester=requester)
    manager = IntegrationManager(store=MemoryCredentialStore(), adapters=[connector])
    flow = connector.begin_device_authorization("public-client-id")
    flow["next_poll_at"] = 0
    credentials = connector.poll_device_authorization(flow)["credentials"]
    account = manager.connect("github", credentials)

    result = manager.execute(account.account_id, "github.repositories.public.list")
    assert result.status == ActionStatus.SUCCEEDED_VERIFIED
    assert [item["name"] for item in result.result] == ["public-one"]
    assert result.verification_evidence["http_status"] == 200


def test_github_missing_required_scope_is_not_reported_as_connected():
    requester = GithubRequester(profile_scopes="")
    connector = GitHubConnector(requester=requester)
    manager = IntegrationManager(store=MemoryCredentialStore(), adapters=[connector])
    with pytest.raises(IntegrationError) as exc:
        manager.connect("github", {
            "access_token": "valid-but-under-scoped",
            "scopes": [],
            "client_id": "public-client-id",
        })
    assert exc.value.code == IntegrationErrorCode.MISSING_PERMISSION
    assert manager.list_accounts() == []


def test_github_device_flow_enforces_minimum_poll_interval_and_slow_down():
    requester = GithubRequester(token_outcome={"error": "slow_down"})
    connector = GitHubConnector(requester=requester)
    flow = connector.begin_device_authorization("public-client-id")
    count = len(requester.calls)

    pending = connector.poll_device_authorization(flow)
    assert pending["pending"] is True
    assert len(requester.calls) == count
    flow["next_poll_at"] = 0
    pending = connector.poll_device_authorization(flow)
    assert pending["pending"] is True
    assert flow["interval"] >= 10
    assert pending["retry_after"] >= 10


def test_github_device_flow_denial_and_expiration_are_typed_errors():
    denied = GithubRequester(token_outcome={"error": "access_denied"})
    connector = GitHubConnector(requester=denied)
    flow = connector.begin_device_authorization("public-client-id")
    flow["next_poll_at"] = 0
    with pytest.raises(IntegrationError) as exc:
        connector.poll_device_authorization(flow)
    assert exc.value.code == IntegrationErrorCode.AUTHORIZATION_DENIED

    expired = connector.begin_device_authorization("public-client-id")
    expired["expires_at"] = time.time() - 1
    with pytest.raises(IntegrationError) as exc2:
        connector.poll_device_authorization(expired)
    assert exc2.value.code == IntegrationErrorCode.AUTHORIZATION_EXPIRED


def test_invalid_github_client_id_is_rejected_before_network_call():
    requester = GithubRequester()
    connector = GitHubConnector(requester=requester)
    with pytest.raises(IntegrationError) as exc:
        connector.begin_device_authorization("bad client id")
    assert exc.value.code == IntegrationErrorCode.INVALID_REQUEST
    assert requester.calls == []


def test_http_helper_rejects_non_https_endpoints_before_network_call():
    with pytest.raises(IntegrationError) as exc:
        request_json("http://example.invalid/api")
    assert exc.value.code == IntegrationErrorCode.INVALID_REQUEST


def test_provider_catalog_discloses_unimplemented_integrations_without_fake_capabilities():
    manager = IntegrationManager(store=MemoryCredentialStore(), adapters=[GitHubConnector(requester=GithubRequester())])
    catalog = {entry.provider_id: entry for entry in manager.catalog()}
    assert catalog["github"].capabilities
    assert catalog["roblox"].status_detail
    assert catalog["amazon"].status_detail
    assert catalog["outlook"].status == ConnectionStatus.UNAVAILABLE
    assert manager.capabilities("roblox") == []
