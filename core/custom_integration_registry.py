"""Persist non-secret custom-provider settings; keep OAuth client secrets in secure storage."""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Mapping

from core.account_integrations import IntegrationError, IntegrationErrorCode, IntegrationManager
from core.runtime_paths import CONFIG_DIR
from core.universal_integrations import configure_provider_connector

CONFIG_PATH = CONFIG_DIR / "custom_account_providers.json"
MAX_REGISTRY_BYTES = 2 * 1024 * 1024
MAX_SPEC_BYTES = 1024 * 1024
MAX_PROVIDER_COUNT = 50
_SECRET_PROVIDER_SENTINEL = "__custom_provider_secret__"
_SECRET_NAMESPACE = uuid.UUID("e4f1b9f1-77a0-4b34-a6a9-fc83c7f399cd")
_PUBLIC_FIELDS = {
    "provider_id", "display_name", "auth_type", "trusted_hosts", "api_key_header",
    "identity_url", "identity_field", "documentation_url", "client_id", "redirect_uri",
    "requested_scopes", "issuer", "oauth_endpoints", "max_operations",
    "protocol", "graphql_endpoint_url", "graphql_operations",
}
_ENDPOINT_FIELDS = ("issuer", "authorization_endpoint", "token_endpoint", "userinfo_endpoint", "jwks_uri", "revocation_endpoint", "scopes_supported")


def _secret_record_id(provider_id: str) -> str:
    return str(uuid.uuid5(_SECRET_NAMESPACE, str(provider_id).strip().lower()))


def _read_document() -> dict[str, Any]:
    path = Path(CONFIG_PATH)
    if not path.exists():
        return {"schema_version": 1, "providers": []}
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_REGISTRY_BYTES:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Custom provider registry exceeds its size limit.")
        value = json.loads(raw.decode("utf-8"))
    except IntegrationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Custom provider registry is unreadable or malformed; it was not overwritten.") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("providers"), list) or len(value["providers"]) > MAX_PROVIDER_COUNT:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Custom provider registry has an unsupported structure; it was not overwritten.")
    seen: set[str] = set()
    for entry in value["providers"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("config"), dict) or not isinstance(entry.get("openapi_spec"), str):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "A custom provider record is malformed; the registry was not overwritten.")
        provider_id = str(entry["config"].get("provider_id") or "").strip().lower()
        if not provider_id or provider_id in seen or len(provider_id) > 64:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Custom provider registry contains a duplicate or invalid provider ID.")
        if len(entry["openapi_spec"].encode("utf-8")) > MAX_SPEC_BYTES:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "A saved OpenAPI document exceeds its size limit.")
        seen.add(provider_id)
    return value


def _write_document(document: Mapping[str, Any]) -> None:
    path = Path(CONFIG_PATH)
    try:
        raw = json.dumps(dict(document), ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Custom provider configuration is not JSON serializable.") from exc
    if len(raw) > MAX_REGISTRY_BYTES:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Custom provider registry exceeds its size limit.")
    temp: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix="custom-providers-", suffix=".tmp", dir=str(path.parent), delete=False) as handle:
            temp = Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        os.replace(temp, path)
    except OSError as exc:
        raise IntegrationError(IntegrationErrorCode.STORAGE_UNAVAILABLE, "Custom provider configuration could not be saved atomically.") from exc
    finally:
        if temp is not None and temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def _public_config(config: Mapping[str, Any], connector: Any) -> dict[str, Any]:
    public = {key: config[key] for key in _PUBLIC_FIELDS if key in config}
    if isinstance(public.get("oauth_endpoints"), Mapping):
        endpoints = public["oauth_endpoints"]
        public["oauth_endpoints"] = {key: endpoints[key] for key in _ENDPOINT_FIELDS if key in endpoints}
    # Cached OIDC discovery is copied from validated adapter metadata, not caller input.
    if str(public.get("auth_type") or "").lower() == "oidc":
        meta = getattr(connector, "metadata", None)
        if meta is None:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OIDC provider cannot be saved without validated issuer metadata.")
        public["oidc_metadata"] = {
            "issuer": meta.issuer, "authorization_endpoint": meta.authorization_endpoint,
            "token_endpoint": meta.token_endpoint, "userinfo_endpoint": meta.userinfo_endpoint,
            "jwks_uri": meta.jwks_uri, "revocation_endpoint": meta.revocation_endpoint,
            "scopes_supported": list(meta.scopes_supported),
            "id_token_signing_alg_values_supported": list(meta.id_token_signing_alg_values_supported),
        }
    return public


def save_custom_provider(
    manager: IntegrationManager,
    config: Mapping[str, Any],
    openapi_spec: str | Mapping[str, Any],
    connector: Any,
    *,
    client_secret: str = "",
) -> dict[str, Any]:
    """Persist and register a reviewed adapter. Secret/config writes roll back on failure."""
    if not manager.store.available:
        raise IntegrationError(IntegrationErrorCode.STORAGE_UNAVAILABLE, "Secure credential storage is unavailable; this provider was not saved.")
    provider = str(config.get("provider_id") or "").strip().lower()
    if not provider or provider != str(getattr(connector, "provider_id", "")).lower():
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Provider ID does not match the reviewed connector.")
    if manager.connector(provider) is not None:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "A provider with this identifier is already registered.")
    if not isinstance(client_secret, str) or len(client_secret) > 4096:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OAuth client secret exceeds the supported length.")
    try:
        spec_text = openapi_spec if isinstance(openapi_spec, str) else json.dumps(openapi_spec, ensure_ascii=False)
        if not isinstance(spec_text, str) or len(spec_text.encode("utf-8")) > MAX_SPEC_BYTES:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Provider schema exceeds the configured size limit.")
        if str(config.get("protocol") or "openapi").strip().lower() != "graphql":
            parsed_spec = json.loads(spec_text)
            if not isinstance(parsed_spec, dict):
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI spec must be a JSON object.")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, IntegrationError):
            raise
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Provider schema must be valid OpenAPI JSON or GraphQL SDL/introspection JSON.") from exc

    public = _public_config(config, connector)
    # Never copy secret material into this file, even if callers pass excess config fields.
    old_exists = Path(CONFIG_PATH).exists()
    old = _read_document()
    entries = old["providers"]
    if any(str(row["config"].get("provider_id") or "").lower() == provider for row in entries):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "A provider configuration with this identifier already exists.")
    if len(entries) >= MAX_PROVIDER_COUNT:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "The custom provider registry is full.")
    secret_id = _secret_record_id(provider)
    previous = manager.store.read(secret_id) if client_secret else None
    has_secret = bool(client_secret)
    if has_secret:
        manager.store.save(secret_id, {
            "provider_id": _SECRET_PROVIDER_SENTINEL, "record_type": "provider_config_secret",
            "provider_key": provider, "client_secret": client_secret,
        })
    updated = dict(old)
    updated["providers"] = list(entries) + [{"config": public, "openapi_spec": spec_text, "client_secret_saved": has_secret}]
    try:
        _write_document(updated)
        manager.register(connector)
    except Exception as exc:
        try:
            if old_exists:
                _write_document(old)
            else:
                Path(CONFIG_PATH).unlink(missing_ok=True)
        except Exception:
            pass
        if has_secret:
            try:
                manager.store.save(secret_id, previous) if isinstance(previous, dict) else manager.store.delete(secret_id)
            except Exception:
                pass
        if isinstance(exc, IntegrationError):
            raise
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Provider registration failed; saved configuration was rolled back where possible.") from exc
    return {"provider_id": provider, "display_name": str(public.get("display_name") or provider), "auth_type": str(public.get("auth_type") or ""), "client_secret_saved": has_secret}


def register_saved_custom_providers(manager: IntegrationManager) -> dict[str, list[str]]:
    """Restore saved adapters without making a malformed config fatal to application startup."""
    result: dict[str, list[str]] = {"loaded": [], "errors": []}
    try:
        document = _read_document()
    except IntegrationError as exc:
        result["errors"].append(str(exc))
        return result
    for entry in document["providers"]:
        config = dict(entry["config"])
        provider = str(config.get("provider_id") or "").strip().lower()
        if manager.connector(provider) is not None:
            result["loaded"].append(provider)
            continue
        if entry.get("client_secret_saved"):
            if not manager.store.available:
                result["errors"].append("Provider " + provider + " needs a protected client secret, but secure storage is unavailable.")
                continue
            try:
                secret = manager.store.read(_secret_record_id(provider))
            except IntegrationError:
                secret = None
            if (not isinstance(secret, dict) or secret.get("record_type") != "provider_config_secret"
                or str(secret.get("provider_key") or "").lower() != provider
                or not isinstance(secret.get("client_secret"), str) or not secret.get("client_secret")):
                result["errors"].append("Provider " + provider + " was not restored because its protected client secret is missing; reconfigure it.")
                continue
            config["client_secret"] = secret["client_secret"]
        try:
            adapter, _preview = configure_provider_connector(config, entry["openapi_spec"])
            manager.register(adapter)
            result["loaded"].append(provider)
        except IntegrationError as exc:
            result["errors"].append("Provider " + provider + " was not restored: " + exc.code.value + ". Review its saved configuration.")
        except Exception:
            result["errors"].append("Provider " + (provider or "(unknown)") + " was not restored because its configuration could not be validated.")
    return result
