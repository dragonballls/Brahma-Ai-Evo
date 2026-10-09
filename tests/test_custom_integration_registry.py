from __future__ import annotations

import json

import pytest

from unittest.mock import MagicMock

import core.custom_integration_registry as registry
from core.account_integrations import IntegrationError, IntegrationManager, MemoryCredentialStore
from core.universal_integrations import OAuth2PKCEConnector, configure_provider_connector


def _config():
    return {
        "provider_id": "example_oidc",
        "display_name": "Example OIDC service",
        "auth_type": "oidc",
        "trusted_hosts": ["api.example.com", "auth.example.com"],
        "client_id": "example-client",
        "client_secret": "test-client-secret-do-not-write-to-disk",
        "redirect_uri": "http://127.0.0.1:8766/oauth/callback",
        "issuer": "https://auth.example.com",
        "requested_scopes": ["openid", "profile"],
        "identity_field": "sub",
        "documentation_url": "https://docs.example.com",
    }


def _spec():
    return {
        "openapi": "3.0.3",
        "info": {"title": "Example service", "version": "1.0.0"},
        "servers": [{"url": "https://api.example.com"}],
        "components": {"securitySchemes": {
            "oidc": {"type": "openIdConnect", "openIdConnectUrl": "https://auth.example.com/.well-known/openid-configuration"}
        }},
        "security": [{"oidc": ["openid", "profile"]}],
        "paths": {"/v1/items": {"get": {
            "operationId": "listItems",
            "summary": "List items",
            "responses": {"200": {"description": "OK"}},
        }}},
    }


def _metadata():
    return {
        "issuer": "https://auth.example.com",
        "authorization_endpoint": "https://auth.example.com/oauth/authorize",
        "token_endpoint": "https://auth.example.com/oauth/token",
        "userinfo_endpoint": "https://auth.example.com/oauth/userinfo",
        "jwks_uri": "https://auth.example.com/.well-known/jwks.json",
        "revocation_endpoint": "https://auth.example.com/oauth/revoke",
        "scopes_supported": ["openid", "profile"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }


def _make_connector(config):
    return configure_provider_connector(config, _spec())[0]


def test_custom_provider_saves_oauth_secret_only_to_protected_store_and_restores(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "CONFIG_PATH", tmp_path / "custom_providers.json")
    store = MemoryCredentialStore()
    manager = IntegrationManager(store=store, adapters=[])
    config = _config()
    config["oidc_metadata"] = _metadata()
    adapter = _make_connector(config)
    saved = registry.save_custom_provider(manager, config, _spec(), adapter, client_secret=config["client_secret"])

    text = registry.CONFIG_PATH.read_text(encoding="utf-8")
    assert config["client_secret"] not in text
    assert '"client_secret":' not in text
    secret = store.read(registry._secret_record_id("example_oidc"))
    assert secret and secret["client_secret"] == config["client_secret"]
    assert saved["provider_id"] == "example_oidc"
    assert manager.list_accounts() == []

    restarted = IntegrationManager(store=store, adapters=[])
    result = registry.register_saved_custom_providers(restarted)
    assert result == {"loaded": ["example_oidc"], "errors": []}
    restored = restarted.connector("example_oidc")
    assert isinstance(restored, OAuth2PKCEConnector)
    assert restored.client_secret == config["client_secret"]
    assert restarted.list_accounts() == []


def test_custom_provider_does_not_restore_when_protected_client_secret_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "CONFIG_PATH", tmp_path / "custom_providers.json")
    store = MemoryCredentialStore()
    manager = IntegrationManager(store=store, adapters=[])
    config = _config()
    config["oidc_metadata"] = _metadata()
    registry.save_custom_provider(manager, config, _spec(), _make_connector(config), client_secret=config["client_secret"])
    store.delete(registry._secret_record_id("example_oidc"))

    restarted = IntegrationManager(store=store, adapters=[])
    result = registry.register_saved_custom_providers(restarted)
    assert not result["loaded"]
    assert any("protected client secret is missing" in message for message in result["errors"])
    assert restarted.connector("example_oidc") is None


def test_corrupt_custom_provider_registry_is_not_overwritten(monkeypatch, tmp_path):
    path = tmp_path / "custom_providers.json"
    path.write_text("{this is not valid JSON", encoding="utf-8")
    monkeypatch.setattr(registry, "CONFIG_PATH", path)
    original = path.read_bytes()
    result = registry.register_saved_custom_providers(IntegrationManager(store=MemoryCredentialStore(), adapters=[]))
    assert result["loaded"] == []
    assert result["errors"]
    assert path.read_bytes() == original


def test_custom_provider_secret_record_never_appears_as_a_connected_account(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "CONFIG_PATH", tmp_path / "custom_providers.json")
    store = MemoryCredentialStore()
    manager = IntegrationManager(store=store, adapters=[])
    config = _config()
    config["oidc_metadata"] = _metadata()
    adapter = _make_connector(config)
    registry.save_custom_provider(manager, config, _spec(), adapter, client_secret=config["client_secret"])
    secret_record = store.read(registry._secret_record_id("example_oidc"))
    assert secret_record["provider_id"] == registry._SECRET_PROVIDER_SENTINEL
    assert all(account.provider_id != registry._SECRET_PROVIDER_SENTINEL for account in manager.list_accounts())


def test_duplicate_custom_provider_registration_is_rejected_without_replacing_saved_definition(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "CONFIG_PATH", tmp_path / "custom_providers.json")
    store = MemoryCredentialStore()
    manager = IntegrationManager(store=store, adapters=[])
    config = _config()
    config["oidc_metadata"] = _metadata()
    registry.save_custom_provider(manager, config, _spec(), _make_connector(config), client_secret=config["client_secret"])
    before = registry.CONFIG_PATH.read_bytes()
    second_manager = IntegrationManager(store=store, adapters=[_make_connector(config)])
    with pytest.raises(IntegrationError, match="already registered"):
        registry.save_custom_provider(second_manager, config, _spec(), second_manager.connector("example_oidc"), client_secret=config["client_secret"])
    assert registry.CONFIG_PATH.read_bytes() == before

def test_generic_manifest_declares_configuration_fields_and_only_real_capabilities():
    config = _config()
    config["oidc_metadata"] = _metadata()
    connector = _make_connector(config)
    assert "client_secret (protected storage only)" in connector.manifest.configuration_fields
    assert any(cap.action == "example_oidc.listItems" and cap.supported for cap in connector.manifest.capabilities)
    assert all(cap.risk.value == "read_only" for cap in connector.manifest.capabilities)


def test_custom_ui_worker_routes_operation_to_the_selected_integration_manager():
    from core.custom_account_integrations_ui import _CustomProviderWorker

    manager = MagicMock()
    result = MagicMock()
    result.to_dict.return_value = {"status": "succeeded_and_verified", "action": "example_oidc.listItems"}
    manager.execute.return_value = result
    emitted = []
    worker = _CustomProviderWorker("execute", manager, account_id="acct-1",
        action="example_oidc.listItems", arguments_text='{"limit": 2}')
    worker.completed.connect(emitted.append)
    worker.run()
    manager.execute.assert_called_once_with("acct-1", "example_oidc.listItems", {"limit": 2})
    assert emitted == [{"ok": True, "kind": "action", "data": {"status": "succeeded_and_verified", "action": "example_oidc.listItems"}}]



def test_custom_ui_worker_queues_write_confirmation_and_redacts_sensitive_arguments():
    from types import SimpleNamespace
    from core.custom_account_integrations_ui import _CustomProviderWorker

    manager = MagicMock()
    pending_result = MagicMock()
    pending_result.to_dict.return_value = {
        "status": "waiting_for_confirmation",
        "provider_id": "example_oidc",
        "action": "example_oidc.updateItem",
        "confirmation_id": "confirm-123",
    }
    manager.execute.return_value = pending_result
    account = SimpleNamespace(account_id="acct-1", provider_id="example_oidc")
    capability = SimpleNamespace(
        action="example_oidc.updateItem",
        description="Update one item",
        risk=SimpleNamespace(value="reversible_change"),
    )
    manager.list_accounts.return_value = [account]
    manager.connector.return_value = SimpleNamespace(
        manifest=SimpleNamespace(capabilities=[capability])
    )
    emitted = []
    worker = _CustomProviderWorker(
        "execute", manager,
        account_id="acct-1", action="example_oidc.updateItem",
        arguments_text='{"item_id":"item-7","body":{"name":"safe","api_key":"never-display-this"}}',
    )
    worker.completed.connect(emitted.append)
    worker.run()

    manager.execute.assert_called_once_with("acct-1", "example_oidc.updateItem", {
        "item_id": "item-7", "body": {"name": "safe", "api_key": "never-display-this"},
    })
    assert len(emitted) == 1
    prompt = emitted[0]["confirmation_prompt"]
    assert prompt["provider_id"] == "example_oidc"
    assert prompt["operation"] == "example_oidc.updateItem"
    assert prompt["risk"] == "reversible_change"
    assert prompt["arguments"]["body"]["api_key"] == "[REDACTED]"
    assert "never-display-this" not in json.dumps(prompt)
    manager.confirm_action.assert_not_called()

    confirmed_result = MagicMock()
    confirmed_result.to_dict.return_value = {"status": "accepted_by_provider_unverified"}
    manager.confirm_action.return_value = confirmed_result
    confirm_events = []
    confirmation_worker = _CustomProviderWorker(
        "confirm", manager, confirmation_id="confirm-123", approved=True
    )
    confirmation_worker.completed.connect(confirm_events.append)
    confirmation_worker.run()
    manager.confirm_action.assert_called_once_with("confirm-123", approved=True)
    assert confirm_events == [{
        "ok": True, "kind": "action",
        "data": {"status": "accepted_by_provider_unverified"},
    }]



def test_custom_registry_persists_and_restores_graphql_schema_and_operations(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "CONFIG_PATH", tmp_path / "custom-providers.json")
    config = {
        "provider_id": "registry_graphql", "display_name": "Registry GraphQL",
        "protocol": "graphql", "auth_type": "api_key",
        "trusted_hosts": ["api.example.test"], "api_key_header": "X-API-Key",
        "identity_url": "https://api.example.test/me", "identity_field": "id",
        "graphql_endpoint_url": "https://api.example.test/graphql",
        "graphql_operations": [{
            "operation_id": "getItem", "summary": "Read item",
            "document": "query GetItem($id: ID!) { item(id: $id) { id } }",
        }],
    }
    schema = "type Item { id: ID! } type Query { item(id: ID!): Item }"
    manager = IntegrationManager(store=MemoryCredentialStore())
    connector, _ = configure_provider_connector(config, schema)
    saved = registry.save_custom_provider(manager, config, schema, connector)
    assert saved["provider_id"] == "registry_graphql"
    record = registry._read_document()["providers"][0]
    assert record["openapi_spec"] == schema
    assert record["config"]["protocol"] == "graphql"
    assert record["config"]["graphql_operations"][0]["operation_id"] == "getItem"

    restored_manager = IntegrationManager(store=MemoryCredentialStore())
    restored = registry.register_saved_custom_providers(restored_manager)
    assert restored["loaded"] == ["registry_graphql"]
    assert restored_manager.connector("registry_graphql") is not None
