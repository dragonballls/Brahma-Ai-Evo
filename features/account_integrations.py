"""Natural-language tool surface for Brahma's secure connected-account registry.

Connection setup and account deletion stay in the explicit Settings UI so a model
cannot create a device-code flow or remove credentials without direct user action.
This tool permits only reads that have been declared by a connected adapter.
"""
from __future__ import annotations

from typing import Any

from core.account_integrations import (
    ConnectionStatus,
    IntegrationError,
    IntegrationErrorCode,
    get_default_manager,
)

FEATURE_METADATA = {
    "name": "account_integrations",
    "aliases": ["accounts", "connected_accounts", "account_connections", "integrations"],
    "description": (
        "Inspect supported integrations, list connected accounts, explain declared capabilities, "
        "test authorization, read the connected GitHub profile, and list public GitHub repositories. "
        "Account connection and disconnection must be completed in the Accounts & Integrations UI."
    ),
    "triggers": [
        "show my connected accounts",
        "list connected accounts",
        "what accounts are connected",
        "show supported integrations",
        "list integrations",
        "what can you do with my github account",
        "what can you do with my roblox account",
        "check my github connection",
        "test my github connection",
        "read my github profile",
        "read my roblox profile",
        "test my roblox connection",
        "show my public github repositories",
        "list my github repositories",
        "why can't you perform this account action",
        "connect an account",
        "disconnect an account",
    ],
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "providers | accounts | capabilities | test_connection | github_profile | roblox_profile | github_public_repositories | connect | disconnect",
            },
            "provider_id": {
                "type": "STRING",
                "description": "Provider identifier such as github, roblox, amazon, youtube, instagram, google_workspace, or outlook",
            },
            "account_id": {
                "type": "STRING",
                "description": "Exact account identifier returned by the connected-account listing",
            },
        },
    },
}


def _account_dict(account) -> dict[str, Any]:
    return {
        "account_id": account.account_id,
        "provider_id": account.provider_id,
        "identity": account.identity,
        "status": account.status.value,
        "scopes": list(account.scopes),
        "connected_at": account.connected_at,
        "last_checked_at": account.last_checked_at,
        "last_error_code": account.last_error_code,
    }


def _select_account(manager, provider_id: str, account_id: str):
    accounts = manager.list_accounts(provider_id or None)
    if account_id:
        chosen = next((item for item in accounts if item.account_id == account_id), None)
        if chosen is None:
            return None, {
                "status": "rejected_before_execution",
                "error_code": "account_not_found",
                "message": "The specified connected account was not found for the requested provider.",
            }
        return chosen, None
    if not accounts:
        return None, {
            "status": "waiting_for_user_authorization",
            "message": f"No connected {provider_id or 'provider'} account is available. Open Settings → Accounts & Integrations and complete the official provider authorization flow.",
        }
    if len(accounts) > 1:
        return None, {
            "status": "rejected_before_execution",
            "error_code": "ambiguous_account",
            "message": "More than one matching account is connected. Specify the exact account_id or identity; no account action was executed.",
            "accounts": [_account_dict(item) for item in accounts],
        }
    return accounts[0], None


def execute_with_manager(manager, *, action: str = "accounts", provider_id: str = "", account_id: str = "") -> dict[str, Any]:
    """Deterministic entry point used by the feature and its tests."""
    action = str(action or "accounts").strip().casefold().replace("-", "_").replace(" ", "_")
    provider_id = str(provider_id or "").strip().casefold()
    account_id = str(account_id or "").strip()

    if action in {"connect", "connect_account", "disconnect", "disconnect_account"}:
        intent = "connect" if action.startswith("connect") else "disconnect"
        return {
            "status": "waiting_for_user_authorization" if intent == "connect" else "waiting_for_confirmation",
            "message": (
                "Open Settings → Accounts & Integrations and complete the provider's official authorization flow. "
                "Brahma does not accept account passwords or authentication codes in chat."
                if intent == "connect" else
                "To disconnect an account, choose the exact account in Settings → Accounts & Integrations and confirm removal there. "
                "No local credential was deleted and no provider request was sent."
            ),
            "ui_route": "settings/accounts",
        }

    if action in {"providers", "integrations", "supported_providers", "catalog"}:
        providers = []
        for manifest in manager.catalog():
            capabilities = manager.capabilities(manifest.provider_id) if manager.connector(manifest.provider_id) else []
            providers.append({
                "provider_id": manifest.provider_id,
                "display_name": manifest.display_name,
                "auth_method": manifest.auth_method,
                "status": manifest.status.value,
                "status_detail": manifest.status_detail,
                "documentation_url": manifest.documentation_url,
                "capabilities": capabilities,
            })
        return {"status": "succeeded_and_verified", "result": providers}

    if action in {"accounts", "list_accounts", "connected_accounts", "status"}:
        return {
            "status": "succeeded_and_verified",
            "result": [_account_dict(item) for item in manager.list_accounts(provider_id or None)],
        }

    if action in {"capabilities", "list_capabilities", "supported_actions"}:
        if not provider_id and account_id:
            account = next((item for item in manager.list_accounts() if item.account_id == account_id), None)
            if account is None:
                return {"status": "rejected_before_execution", "error_code": "account_not_found", "message": "The account was not found."}
            provider_id = account.provider_id
        if not provider_id:
            return {"status": "rejected_before_execution", "error_code": "invalid_request", "message": "Specify the provider whose capabilities you want to inspect."}
        account, error = _select_account(manager, provider_id, account_id) if account_id else (None, None)
        if error:
            return error
        scopes = account.scopes if account else ()
        try:
            capabilities = manager.capabilities(provider_id, scopes=scopes)
        except IntegrationError as exc:
            return {"status": "unsupported", "error_code": exc.code.value, "message": str(exc)}
        return {
            "status": "succeeded_and_verified",
            "provider_id": provider_id,
            "account_id": account.account_id if account else None,
            "result": capabilities,
        }

    action_map = {
        "test": "test_connection",
        "test_connection": "test_connection",
        "connection_test": "test_connection",
        "profile": "github_profile",
        "read_profile": "github_profile",
        "github_profile": "github_profile",
        "roblox_profile": "roblox_profile",
        "read_roblox_profile": "roblox_profile",
        "public_repositories": "github_public_repositories",
        "list_public_repositories": "github_public_repositories",
        "github_public_repositories": "github_public_repositories",
        "github_repositories": "github_public_repositories",
    }
    normalized = action_map.get(action)
    if normalized is None:
        return {
            "status": "unsupported",
            "error_code": IntegrationErrorCode.UNSUPPORTED_ACTION.value,
            "message": "That account operation is not declared by this tool. Inspect the provider capabilities before requesting an action.",
        }

    if normalized == "roblox_profile":
        provider_id = provider_id or "roblox"
    else:
        provider_id = provider_id or "github"
    if normalized == "github_profile" and provider_id != "github":
        return {"status": "unsupported", "error_code": "unsupported_action", "message": "GitHub profile reads require a connected GitHub account."}
    if normalized == "github_public_repositories" and provider_id != "github":
        return {"status": "unsupported", "error_code": "unsupported_action", "message": "Public-repository listing is currently implemented only for GitHub."}
    if normalized == "roblox_profile" and provider_id != "roblox":
        return {"status": "unsupported", "error_code": "unsupported_action", "message": "Roblox profile reads require a connected Roblox account."}

    account, error = _select_account(manager, provider_id, account_id)
    if error:
        return error
    if account.status != ConnectionStatus.CONNECTED:
        return {
            "status": "rejected_before_execution",
            "error_code": "authentication_required" if account.status == ConnectionStatus.AUTHENTICATION_REQUIRED else "missing_permission",
            "message": f"Account status is {account.status.value}; no account operation was executed.",
        }

    try:
        if normalized == "test_connection":
            checked = manager.test_connection(account.account_id)
            return {
                "status": "succeeded_and_verified" if checked.get("ok") else "failed",
                "account": _account_dict(next((a for a in manager.list_accounts() if a.account_id == account.account_id), account)),
                "verification_evidence": checked.get("evidence") or {},
                "error_code": None if checked.get("ok") else str((checked.get("evidence") or {}).get("error_code") or "connection_failed"),
            }
        operation = (
            "github.profile.read" if normalized == "github_profile"
            else "roblox.profile.read" if normalized == "roblox_profile"
            else "github.repositories.public.list"
        )
        result = manager.execute(account.account_id, operation)
        return result.to_dict()
    except IntegrationError as exc:
        return {"status": "failed", "error_code": exc.code.value, "message": str(exc)}
    except Exception:
        return {"status": "failed", "error_code": "unexpected_error", "message": "The account request failed without exposing provider credentials."}


def execute(**kwargs):
    try:
        return execute_with_manager(
            get_default_manager(),
            action=kwargs.get("action", "accounts"),
            provider_id=kwargs.get("provider_id", ""),
            account_id=kwargs.get("account_id", ""),
        )
    except IntegrationError as exc:
        return {"status": "failed", "error_code": exc.code.value, "message": str(exc)}
    except Exception:
        return {"status": "failed", "error_code": "unexpected_error", "message": "Account integrations are currently unavailable."}
