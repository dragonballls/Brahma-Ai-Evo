"""Secure, capability-driven third-party account integrations.

Secrets are never written to ordinary configuration files. The production store uses
Windows Credential Manager; other platforms must inject a real secure store rather
than silently falling back to plaintext persistence.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Protocol


CREDENTIAL_TARGET_PREFIX = "BrahmaEvo:IntegrationAccount:"
ACTIVITY_LIMIT = 200
PENDING_CONFIRMATION_LIMIT = 64
PENDING_CONFIRMATION_TTL_SECONDS = 90.0
REQUEST_TIMEOUT_SECONDS = 8.0
GITHUB_API = "https://api.github.com"
GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_DEVICE_URL = "https://github.com/login/device"


class IntegrationErrorCode(str, Enum):
    AUTHENTICATION_REQUIRED = "authentication_required"
    MISSING_PERMISSION = "missing_permission"
    UNSUPPORTED_ACTION = "unsupported_action"
    RATE_LIMITED = "rate_limited"
    NETWORK_ERROR = "network_error"
    PROVIDER_ERROR = "provider_error"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    INVALID_AUTH_RESPONSE = "invalid_auth_response"
    AUTHORIZATION_DENIED = "authorization_denied"
    AUTHORIZATION_EXPIRED = "authorization_expired"
    CANCELLED = "cancelled"
    CONFIRMATION_REQUIRED = "confirmation_required"
    ACCOUNT_NOT_FOUND = "account_not_found"
    INVALID_REQUEST = "invalid_request"


class ConnectionStatus(str, Enum):
    CONNECTED = "Connected"
    AUTHENTICATION_REQUIRED = "Authentication Required"
    MISSING_PERMISSION = "Missing Permission"
    LIMITED_SUPPORT = "Limited Support"
    UNAVAILABLE = "Unavailable"
    DISCONNECTED = "Disconnected"


class ActionStatus(str, Enum):
    REJECTED = "rejected_before_execution"
    WAITING_FOR_AUTHORIZATION = "waiting_for_user_authorization"
    WAITING_FOR_CONFIRMATION = "waiting_for_confirmation"
    RUNNING = "running"
    ACCEPTED_UNVERIFIED = "accepted_by_provider_unverified"
    SUCCEEDED_VERIFIED = "succeeded_and_verified"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    UNSUPPORTED = "unsupported"


class RiskLevel(str, Enum):
    READ_ONLY = "read_only"
    REVERSIBLE = "reversible_change"
    EXTERNAL_COMMUNICATION = "external_communication"
    PUBLICATION = "publication"
    FINANCIAL = "financial_commitment"
    DESTRUCTIVE = "destructive_or_security_sensitive"


@dataclass(frozen=True)
class Capability:
    action: str
    description: str
    risk: RiskLevel = RiskLevel.READ_ONLY
    required_scopes: tuple[str, ...] = ()
    idempotent: bool = True
    supported: bool = True


@dataclass(frozen=True)
class IntegrationManifest:
    provider_id: str
    display_name: str
    auth_method: str
    capabilities: tuple[Capability, ...] = ()
    documentation_url: str = ""
    status: ConnectionStatus = ConnectionStatus.LIMITED_SUPPORT
    status_detail: str = ""


@dataclass(frozen=True)
class AccountSummary:
    account_id: str
    provider_id: str
    identity: str
    status: ConnectionStatus
    scopes: tuple[str, ...] = ()
    connected_at: float = 0.0
    last_checked_at: float = 0.0
    last_error_code: str | None = None


@dataclass(frozen=True)
class ActionResult:
    status: ActionStatus
    provider_id: str
    account_id: str | None
    action: str
    result: Any = None
    verification_evidence: Mapping[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    message: str = ""
    follow_up: str | None = None
    confirmation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe, secret-free result contract."""
        return {
            "status": self.status.value,
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "action": self.action,
            "result": _sanitize_payload(self.result),
            "verification_evidence": _sanitize_payload(self.verification_evidence),
            "error_code": self.error_code,
            "message": _redact(self.message),
            "follow_up": _redact(self.follow_up or ""),
            "confirmation_id": self.confirmation_id,
        }


class IntegrationError(RuntimeError):
    def __init__(
        self,
        code: IntegrationErrorCode,
        message: str,
        *,
        retryable: bool = False,
        retry_after: float | None = None,
    ):
        self.code = code
        self.retryable = bool(retryable)
        self.retry_after = retry_after
        super().__init__(_redact(message))


def _looks_secret(key: str) -> bool:
    lowered = str(key or "").casefold().replace("-", "_")
    return any(marker in lowered for marker in ("token", "secret", "password", "credential", "cookie", "authorization", "device_code", "client_secret", "api_key", "private_key"))


def _sanitize_payload(value: Any, *, depth: int = 0) -> Any:
    """Bound nested provider data and remove fields that could contain secrets."""
    if depth > 6:
        return "[TRUNCATED]"
    if isinstance(value, str):
        return _redact(value)[:4000]
    if isinstance(value, Mapping):
        cleaned = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 100:
                cleaned["_truncated"] = True
                break
            name = str(key)[:120]
            if _looks_secret(name):
                continue
            cleaned[name] = _sanitize_payload(item, depth=depth + 1)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_sanitize_payload(item, depth=depth + 1) for item in list(value)[:100]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact(value)


def _redact(text: object) -> str:
    value = str(text or "")
    for marker in ("Bearer ", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_", "ghp_"):
        start = 0
        while True:
            idx = value.find(marker, start)
            if idx < 0:
                break
            secret_start = idx + len(marker) if marker == "Bearer " else idx
            end = secret_start
            while end < len(value) and value[end] not in " \r\n\t,;\"'":
                end += 1
            value = value[:secret_start] + "[REDACTED]" + value[end:]
            start = secret_start + len("[REDACTED]")
    return value[:1000]


class SecureCredentialStore(Protocol):
    @property
    def available(self) -> bool: ...
    def save(self, account_id: str, record: Mapping[str, Any]) -> None: ...
    def read(self, account_id: str) -> dict[str, Any] | None: ...
    def delete(self, account_id: str) -> bool: ...
    def list_records(self) -> list[tuple[str, dict[str, Any]]]: ...


class WindowsCredentialManager:
    """Windows Credential Manager backend. No plaintext file fallback is provided."""

    def __init__(self):
        self._win32cred = None
        self._lock = threading.RLock()
        if os.name == "nt":
            try:
                import win32cred
                self._win32cred = win32cred
            except Exception:
                self._win32cred = None

    @property
    def available(self) -> bool:
        return self._win32cred is not None

    def _target(self, account_id: str) -> str:
        try:
            canonical = str(uuid.UUID(str(account_id)))
        except (ValueError, TypeError, AttributeError) as exc:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Invalid account identifier.") from exc
        return CREDENTIAL_TARGET_PREFIX + canonical

    def save(self, account_id: str, record: Mapping[str, Any]) -> None:
        if not self.available:
            raise IntegrationError(
                IntegrationErrorCode.STORAGE_UNAVAILABLE,
                "Windows Credential Manager is unavailable; refusing to persist account secrets insecurely.",
            )
        payload = json.dumps(dict(record), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > 5000:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Credential record exceeds the Windows secure-store size limit.")
        item = {
            "Type": self._win32cred.CRED_TYPE_GENERIC,
            "TargetName": self._target(account_id),
            "UserName": str(account_id),
            "CredentialBlob": payload,
            "Persist": self._win32cred.CRED_PERSIST_LOCAL_MACHINE,
            "Comment": "Brahma Evo integration account; secret data stored in Windows Credential Manager.",
        }
        try:
            with self._lock:
                self._win32cred.CredWrite(item, 0)
        except Exception as exc:
            raise IntegrationError(IntegrationErrorCode.STORAGE_UNAVAILABLE, "Windows Credential Manager could not save the account securely.") from exc

    def read(self, account_id: str) -> dict[str, Any] | None:
        if not self.available:
            raise IntegrationError(IntegrationErrorCode.STORAGE_UNAVAILABLE, "Windows Credential Manager is unavailable.")
        try:
            with self._lock:
                item = self._win32cred.CredRead(self._target(account_id), self._win32cred.CRED_TYPE_GENERIC, 0)
            blob = item.get("CredentialBlob", b"")
            if isinstance(blob, str):
                blob = blob.encode("utf-8")
            value = json.loads(bytes(blob).decode("utf-8"))
            return value if isinstance(value, dict) else None
        except Exception as exc:
            # Do not bubble up credential-manager exceptions; some include target data.
            text = str(exc).lower()
            if "not found" in text or "1168" in text:
                return None
            raise IntegrationError(IntegrationErrorCode.STORAGE_UNAVAILABLE, "Windows Credential Manager could not read the account.") from exc

    def delete(self, account_id: str) -> bool:
        if not self.available:
            raise IntegrationError(IntegrationErrorCode.STORAGE_UNAVAILABLE, "Windows Credential Manager is unavailable.")
        try:
            with self._lock:
                self._win32cred.CredDelete(self._target(account_id), self._win32cred.CRED_TYPE_GENERIC, 0)
            return True
        except Exception as exc:
            if "not found" in str(exc).lower() or "1168" in str(exc):
                return False
            raise IntegrationError(IntegrationErrorCode.STORAGE_UNAVAILABLE, "Windows Credential Manager could not delete the account.") from exc

    def list_records(self) -> list[tuple[str, dict[str, Any]]]:
        if not self.available:
            raise IntegrationError(IntegrationErrorCode.STORAGE_UNAVAILABLE, "Windows Credential Manager is unavailable.")
        try:
            with self._lock:
                items = self._win32cred.CredEnumerate(CREDENTIAL_TARGET_PREFIX + "*", 0) or []
        except Exception as exc:
            text = str(exc).lower()
            if "not found" in text or "1168" in text:
                return []
            raise IntegrationError(IntegrationErrorCode.STORAGE_UNAVAILABLE, "Windows Credential Manager could not enumerate integration accounts.") from exc
        records: list[tuple[str, dict[str, Any]]] = []
        for item in items:
            target = str(item.get("TargetName", ""))
            if not target.startswith(CREDENTIAL_TARGET_PREFIX):
                continue
            account_id = target[len(CREDENTIAL_TARGET_PREFIX):]
            try:
                canonical = str(uuid.UUID(account_id))
                blob = item.get("CredentialBlob", b"")
                if isinstance(blob, str):
                    blob = blob.encode("utf-8")
                record = json.loads(bytes(blob).decode("utf-8"))
                if isinstance(record, dict):
                    records.append((canonical, record))
            except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
                continue
        return records


class MemoryCredentialStore:
    """Explicit test/development store; never selected as a production fallback."""

    def __init__(self):
        self._records: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    @property
    def available(self) -> bool:
        return True

    def save(self, account_id: str, record: Mapping[str, Any]) -> None:
        with self._lock:
            self._records[str(account_id)] = json.loads(json.dumps(dict(record)))

    def read(self, account_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._records.get(str(account_id))
            return json.loads(json.dumps(value)) if value is not None else None

    def delete(self, account_id: str) -> bool:
        with self._lock:
            return self._records.pop(str(account_id), None) is not None

    def list_records(self) -> list[tuple[str, dict[str, Any]]]:
        with self._lock:
            return [(key, json.loads(json.dumps(value))) for key, value in self._records.items()]


@dataclass
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    data: Any


def request_json(
    url: str,
    *,
    method: str = "GET",
    form: Mapping[str, str] | None = None,
    token: str | None = None,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> HttpResponse:
    """Issue JSON/form requests only to HTTPS, returning safe structured errors."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Integration requests require HTTPS.")
    body = None
    headers = {"Accept": "application/json", "User-Agent": "Brahma-Evo-Account-Integrations"}
    if form is not None:
        body = urllib.parse.urlencode(dict(form)).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=max(0.5, min(float(timeout), 15.0))) as response:
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Provider response exceeded the 1 MiB safety limit.")
            data = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(data, (dict, list)):
                raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Provider returned an unexpected response format.")
            return HttpResponse(int(response.status), dict(response.headers.items()), data)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(65536)
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}
        return HttpResponse(int(exc.code), dict(exc.headers.items()) if exc.headers else {}, payload)
    except IntegrationError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, UnicodeDecodeError) as exc:
        raise IntegrationError(IntegrationErrorCode.NETWORK_ERROR, "Provider request failed or timed out.", retryable=True) from exc


class Connector(Protocol):
    manifest: IntegrationManifest
    def validate_credentials(self, credentials: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]: ...
    def health_check(self, credentials: Mapping[str, Any]) -> tuple[bool, Mapping[str, Any]]: ...
    def execute(self, action: str, arguments: Mapping[str, Any], credentials: Mapping[str, Any]) -> tuple[Any, Mapping[str, Any]]: ...


class GitHubConnector:
    """GitHub OAuth device flow and safe read-only REST capabilities.

    A user-provided OAuth App client ID is required. The app owner must enable
    Device Flow. The optional app client secret is only persisted in the secure
    credential store and is used solely for the provider's token-refresh request.
    """

    manifest = IntegrationManifest(
        provider_id="github",
        display_name="GitHub",
        auth_method="oauth_device_flow",
        documentation_url="https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps",
        status=ConnectionStatus.LIMITED_SUPPORT,
        status_detail="OAuth device authorization with profile and public-repository read operations. Requires a configured OAuth App with Device Flow enabled.",
        capabilities=(
            Capability("github.profile.read", "Read the connected GitHub account profile.", RiskLevel.READ_ONLY, ("read:user",)),
            Capability("github.repositories.public.list", "List public repositories for the connected account.", RiskLevel.READ_ONLY),
            Capability("github.connection.test", "Validate the token and connected GitHub identity.", RiskLevel.READ_ONLY),
        ),
    )

    def __init__(self, requester: Callable[..., HttpResponse] | None = None):
        self._requester = requester or request_json

    @staticmethod
    def _validate_client_id(client_id: str) -> str:
        value = str(client_id or "").strip()
        if not value or len(value) > 160 or any(ch.isspace() for ch in value):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Enter a valid GitHub OAuth App client ID.")
        return value

    def begin_device_authorization(self, client_id: str) -> dict[str, Any]:
        client_id = self._validate_client_id(client_id)
        response = self._requester(
            GITHUB_DEVICE_CODE_URL,
            method="POST",
            form={"client_id": client_id, "scope": "read:user"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        data = response.data
        if response.status != 200 or data.get("error"):
            raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "GitHub did not start device authorization. Check the OAuth client ID and whether Device Flow is enabled.")
        device_code = str(data.get("device_code") or "")
        user_code = str(data.get("user_code") or "")
        verification_uri = str(data.get("verification_uri") or "")
        try:
            expires_in = max(30, min(int(data.get("expires_in", 900)), 1800))
            interval = max(5, min(int(data.get("interval", 5)), 60))
        except (TypeError, ValueError) as exc:
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "GitHub returned an invalid device authorization response.") from exc
        if not device_code or not user_code or verification_uri != GITHUB_DEVICE_URL:
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "GitHub returned incomplete or unexpected device authorization details.")
        return {
            "device_code": device_code,
            "user_code": user_code,
            "verification_uri": verification_uri,
            "client_id": client_id,
            "expires_at": time.time() + expires_in,
            "interval": interval,
            "next_poll_at": time.time() + interval,
            "client_secret": "",
        }

    def poll_device_authorization(self, flow: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(flow, dict) or not flow.get("device_code") or not flow.get("client_id"):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "No active GitHub device authorization is available.")
        now = time.time()
        if now >= float(flow.get("expires_at", 0)):
            raise IntegrationError(IntegrationErrorCode.AUTHORIZATION_EXPIRED, "GitHub device authorization expired; start a new connection.")
        if now < float(flow.get("next_poll_at", 0)):
            return {"pending": True, "retry_after": max(1, int(float(flow["next_poll_at"]) - now))}
        response = self._requester(
            GITHUB_TOKEN_URL,
            method="POST",
            form={
                "client_id": str(flow["client_id"]),
                "device_code": str(flow["device_code"]),
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        data = response.data
        error = str(data.get("error") or "")
        if error in ("authorization_pending", "slow_down"):
            interval = max(5, int(flow.get("interval", 5))) + (5 if error == "slow_down" else 0)
            flow["interval"] = min(interval, 120)
            flow["next_poll_at"] = time.time() + flow["interval"]
            return {"pending": True, "retry_after": flow["interval"]}
        if error == "access_denied":
            raise IntegrationError(IntegrationErrorCode.AUTHORIZATION_DENIED, "GitHub authorization was denied or cancelled.")
        if error in ("expired_token", "token_expired"):
            raise IntegrationError(IntegrationErrorCode.AUTHORIZATION_EXPIRED, "GitHub device authorization expired; start a new connection.")
        token = str(data.get("access_token") or "")
        if response.status != 200 or not token:
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "GitHub did not return a valid access token.")
        credentials = {
            "access_token": token,
            "refresh_token": str(data.get("refresh_token") or ""),
            "expires_at": time.time() + int(data.get("expires_in", 0)) if data.get("expires_in") else 0,
            "refresh_token_expires_at": time.time() + int(data.get("refresh_token_expires_in", 0)) if data.get("refresh_token_expires_in") else 0,
            "scopes": [scope.strip() for scope in str(data.get("scope") or "").split(",") if scope.strip()],
            "client_id": str(flow["client_id"]),
            "client_secret": str(flow.get("client_secret") or ""),
            "token_type": str(data.get("token_type") or "bearer"),
        }
        return {"pending": False, "credentials": credentials}

    def validate_credentials(self, credentials: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
        token = str(credentials.get("access_token") or "")
        if not token:
            raise IntegrationError(IntegrationErrorCode.AUTHENTICATION_REQUIRED, "GitHub authorization is missing.")
        response = self._requester(
            GITHUB_API + "/user",
            method="GET",
            token=token,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status == 401:
            raise IntegrationError(IntegrationErrorCode.AUTHENTICATION_REQUIRED, "GitHub rejected the authorization token; reconnect this account.")
        if response.status == 429 or (response.status == 403 and any(key.lower() == "x-ratelimit-remaining" and str(value) == "0" for key, value in response.headers.items())):
            raise IntegrationError(IntegrationErrorCode.RATE_LIMITED, "GitHub rate limit reached; retry after the provider's reset time.", retryable=True)
        if response.status == 403:
            raise IntegrationError(IntegrationErrorCode.MISSING_PERMISSION, "GitHub denied access to the account profile. Grant the required read:user permission.")
        if response.status != 200:
            raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "GitHub account validation failed.")
        login = str(response.data.get("login") or "").strip()
        if not login:
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "GitHub validated the token but returned no account identity.")
        scope_header = ""
        scope_header_present = False
        for key, value in response.headers.items():
            if key.lower() == "x-oauth-scopes":
                scope_header_present = True
                scope_header = str(value or "")
                break
        scopes = tuple(sorted({scope.strip() for scope in scope_header.split(",") if scope.strip()}))
        raw_credential_scopes = credentials.get("scopes") or []
        if isinstance(raw_credential_scopes, str):
            raw_credential_scopes = raw_credential_scopes.split(",")
        response_scopes = {str(scope).strip() for scope in raw_credential_scopes if str(scope).strip()}
        # When GitHub explicitly reports granted scopes, an empty header is
        # authoritative and must not be overridden by token-response metadata.
        effective_scopes = tuple(sorted(set(scopes if scope_header_present else response_scopes)))
        # The OAuth device flow requests only read:user. Fail closed if that scope
        # was not granted, instead of storing an account whose declared actions fail.
        if "read:user" not in effective_scopes and "user" not in effective_scopes:
            raise IntegrationError(IntegrationErrorCode.MISSING_PERMISSION, "GitHub token is valid but the required read:user permission was not granted.")
        return login, effective_scopes

    def health_check(self, credentials: Mapping[str, Any]) -> tuple[bool, Mapping[str, Any]]:
        try:
            identity, scopes = self.validate_credentials(credentials)
            return True, {"identity": identity, "scopes": list(scopes), "http_status": 200, "source": "GET /user"}
        except IntegrationError as exc:
            if exc.code == IntegrationErrorCode.AUTHENTICATION_REQUIRED:
                return False, {"error_code": exc.code.value, "status": ConnectionStatus.AUTHENTICATION_REQUIRED.value}
            if exc.code == IntegrationErrorCode.MISSING_PERMISSION:
                return False, {"error_code": exc.code.value, "status": ConnectionStatus.MISSING_PERMISSION.value}
            raise

    def refresh_credentials(self, credentials: Mapping[str, Any]) -> dict[str, Any]:
        refresh_token = str(credentials.get("refresh_token") or "")
        client_id = str(credentials.get("client_id") or "")
        if not refresh_token or not client_id:
            raise IntegrationError(IntegrationErrorCode.AUTHENTICATION_REQUIRED, "This GitHub authorization cannot be refreshed automatically; reconnect it in Accounts & Integrations.")
        # GitHub's documented device-flow refresh does not require a client secret.
        response = self._requester(
            GITHUB_TOKEN_URL,
            method="POST",
            form={
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status != 200 or not response.data.get("access_token"):
            raise IntegrationError(IntegrationErrorCode.AUTHENTICATION_REQUIRED, "GitHub token refresh failed; reconnect this account.")
        updated = dict(credentials)
        updated["access_token"] = str(response.data["access_token"])
        updated["refresh_token"] = str(response.data.get("refresh_token") or refresh_token)
        updated["expires_at"] = time.time() + int(response.data.get("expires_in", 0)) if response.data.get("expires_in") else 0
        updated["refresh_token_expires_at"] = time.time() + int(response.data.get("refresh_token_expires_in", 0)) if response.data.get("refresh_token_expires_in") else updated.get("refresh_token_expires_at", 0)
        return updated

    def execute(self, action: str, arguments: Mapping[str, Any], credentials: Mapping[str, Any]) -> tuple[Any, Mapping[str, Any]]:
        token = str(credentials.get("access_token") or "")
        if action in ("github.profile.read", "github.connection.test"):
            response = self._requester(GITHUB_API + "/user", method="GET", token=token, timeout=REQUEST_TIMEOUT_SECONDS)
            if response.status == 401:
                raise IntegrationError(IntegrationErrorCode.AUTHENTICATION_REQUIRED, "GitHub authorization expired or was revoked; reconnect this account.")
            if response.status == 403 and any(key.lower() == "x-ratelimit-remaining" and str(value) == "0" for key, value in response.headers.items()):
                raise IntegrationError(IntegrationErrorCode.RATE_LIMITED, "GitHub rate limit reached; retry later.", retryable=True)
            if response.status != 200:
                raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "GitHub profile request failed.")
            profile = {key: response.data.get(key) for key in ("login", "id", "name", "html_url", "type") if response.data.get(key) is not None}
            return profile, {"verified": True, "source": "GET /user", "http_status": response.status, "identity": profile.get("login")}
        if action == "github.repositories.public.list":
            login, _scopes = self.validate_credentials(credentials)
            url = GITHUB_API + "/users/" + urllib.parse.quote(login, safe="") + "/repos?type=owner&sort=updated&per_page=30"
            response = self._requester(url, method="GET", token=token, timeout=REQUEST_TIMEOUT_SECONDS)
            if response.status == 401:
                raise IntegrationError(IntegrationErrorCode.AUTHENTICATION_REQUIRED, "GitHub authorization expired or was revoked; reconnect this account.")
            if response.status == 403 and any(key.lower() == "x-ratelimit-remaining" and str(value) == "0" for key, value in response.headers.items()):
                raise IntegrationError(IntegrationErrorCode.RATE_LIMITED, "GitHub rate limit reached; retry later.", retryable=True)
            if response.status != 200:
                raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "GitHub public repository lookup failed.")
            # The user's public-repository endpoint should return public entries only,
            # but filter explicitly so no private names can enter a result.
            source = response.data.get("repositories", response.data) if isinstance(response.data, dict) else response.data
            if not isinstance(source, list):
                raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "GitHub returned an unexpected repository list.")
            repositories = []
            for item in source:
                if not isinstance(item, dict) or bool(item.get("private", False)):
                    continue
                repositories.append({
                    "name": str(item.get("name") or ""),
                    "full_name": str(item.get("full_name") or ""),
                    "html_url": str(item.get("html_url") or ""),
                    "description": str(item.get("description") or ""),
                    "updated_at": item.get("updated_at"),
                    "default_branch": item.get("default_branch"),
                })
            return repositories, {"verified": True, "source": "GET /users/{login}/repos?type=owner", "http_status": response.status, "items_returned": len(repositories)}
        raise IntegrationError(IntegrationErrorCode.UNSUPPORTED_ACTION, "The GitHub connector does not declare that operation.")


class RobloxConnector:
    """Official Roblox OAuth2 authorization-code adapter with PKCE and loopback callback validation."""

    AUTHORIZATION_URL = "https://apis.roblox.com/oauth/v1/authorize"
    TOKEN_URL = "https://apis.roblox.com/oauth/v1/token"
    USERINFO_URL = "https://apis.roblox.com/oauth/v1/userinfo"
    REVOCATION_URL = "https://apis.roblox.com/oauth/v1/token/revoke"

    manifest = IntegrationManifest(
        provider_id="roblox",
        display_name="Roblox",
        auth_method="oauth2_authorization_code_pkce",
        documentation_url="https://create.roblox.com/docs/cloud/auth/oauth2-reference",
        status=ConnectionStatus.LIMITED_SUPPORT,
        status_detail="Official OAuth2/OIDC with PKCE; verified user identity and basic profile reads only. Roblox documents this OAuth API as beta; resource/game actions are not claimed.",
        capabilities=(
            Capability("roblox.connection.test", "Validate the authorization and Roblox user identity.", RiskLevel.READ_ONLY, ("openid",)),
            Capability("roblox.profile.read", "Read the connected Roblox account's basic profile.", RiskLevel.READ_ONLY, ("openid", "profile")),
        ),
    )

    def __init__(self, requester: Callable[..., HttpResponse] | None = None):
        self._requester = requester or request_json

    @staticmethod
    def _text(value: Any, field_name: str, *, max_length: int = 512) -> str:
        text = str(value or "").strip()
        if not text or len(text) > max_length or any(ord(ch) < 32 for ch in text):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, f"Enter a valid {field_name}.")
        return text

    @staticmethod
    def _redirect_parts(redirect_uri: str):
        parsed = urllib.parse.urlparse(str(redirect_uri or "").strip())
        # This native-app flow intentionally supports only the exact IPv4 loopback
        # callback. It does not start a server on a LAN interface or accept open redirects.
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port is None
            or not (1024 <= parsed.port <= 65535)
            or parsed.path != "/roblox/callback"
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise IntegrationError(
                IntegrationErrorCode.INVALID_REQUEST,
                "Roblox redirect URI must be an exact loopback URL such as http://127.0.0.1:8765/roblox/callback, registered with the Roblox OAuth app.",
            )
        return parsed

    def begin_authorization(self, client_id: str, client_secret: str, redirect_uri: str) -> dict[str, Any]:
        client_id = self._text(client_id, "Roblox OAuth client ID", max_length=160)
        client_secret = self._text(client_secret, "Roblox OAuth client secret", max_length=1000)
        self._redirect_parts(redirect_uri)
        verifier = secrets.token_urlsafe(48)
        challenge = __import__("base64").urlsafe_b64encode(
            __import__("hashlib").sha256(verifier.encode("ascii")).digest()
        ).decode("ascii").rstrip("=")
        state = secrets.token_urlsafe(32)
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid profile",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "consent",
        }
        authorization_url = self.AUTHORIZATION_URL + "?" + urllib.parse.urlencode(params)
        return {
            "provider_id": "roblox",
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_verifier": verifier,
            "expires_at": time.time() + 600,
            "authorization_url": authorization_url,
        }

    def complete_authorization(self, flow: Mapping[str, Any], callback_url: str) -> dict[str, Any]:
        if not isinstance(flow, Mapping):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "No Roblox authorization flow is active.")
        if time.time() >= float(flow.get("expires_at") or 0):
            raise IntegrationError(IntegrationErrorCode.AUTHORIZATION_EXPIRED, "Roblox authorization expired; start a new connection.")
        expected = self._redirect_parts(str(flow.get("redirect_uri") or ""))
        callback = urllib.parse.urlparse(str(callback_url or "").strip())
        if (
            callback.scheme != expected.scheme
            or callback.hostname != expected.hostname
            or callback.port != expected.port
            or callback.path != expected.path
            or callback.fragment
            or callback.username
            or callback.password
        ):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Roblox returned to an unexpected callback URI.")
        query = urllib.parse.parse_qs(callback.query, keep_blank_values=True, max_num_fields=20)
        state_values = query.get("state") or []
        if len(state_values) != 1 or not secrets.compare_digest(str(state_values[0]), str(flow.get("state") or "")):
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "Roblox OAuth state did not match; authorization was discarded.")
        errors = query.get("error") or []
        if errors:
            message = "Roblox authorization was denied or cancelled." if errors[0] == "access_denied" else "Roblox authorization returned an error."
            raise IntegrationError(IntegrationErrorCode.AUTHORIZATION_DENIED, message)
        codes = query.get("code") or []
        if len(codes) != 1 or not codes[0] or len(codes[0]) > 2048:
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "Roblox callback did not contain exactly one valid authorization code.")
        if not flow.get("client_id") or not flow.get("client_secret") or not flow.get("code_verifier"):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Roblox authorization flow is missing required PKCE credentials.")
        response = self._requester(
            self.TOKEN_URL,
            method="POST",
            form={
                "grant_type": "authorization_code",
                "client_id": str(flow["client_id"]),
                "client_secret": str(flow["client_secret"]),
                "redirect_uri": str(flow["redirect_uri"]),
                "code_verifier": str(flow["code_verifier"]),
                "code": str(codes[0]),
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        data = response.data
        if response.status == 401 or response.status == 400:
            raise IntegrationError(IntegrationErrorCode.AUTHENTICATION_REQUIRED, "Roblox rejected the authorization code; start a new connection.")
        if response.status == 429:
            raise IntegrationError(IntegrationErrorCode.RATE_LIMITED, "Roblox rate limit reached during authorization exchange.", retryable=True)
        if response.status != 200 or not isinstance(data, dict) or not data.get("access_token") or not data.get("refresh_token"):
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "Roblox did not return a complete access and refresh token pair.")
        scopes = tuple(sorted({item for item in str(data.get("scope") or "").split() if item}))
        credentials = {
            "access_token": str(data["access_token"]),
            "refresh_token": str(data["refresh_token"]),
            "client_id": str(flow["client_id"]),
            "client_secret": str(flow["client_secret"]),
            "redirect_uri": str(flow["redirect_uri"]),
            "token_type": str(data.get("token_type") or "Bearer"),
            "expires_at": time.time() + max(1, min(int(data.get("expires_in", 900)), 86400)),
            "scopes": list(scopes),
        }
        identity, _granted_scopes = self.validate_credentials(credentials)
        credentials["identity"] = identity
        return credentials

    def _get_userinfo(self, credentials: Mapping[str, Any]) -> tuple[dict[str, Any], HttpResponse]:
        token = str(credentials.get("access_token") or "")
        if not token:
            raise IntegrationError(IntegrationErrorCode.AUTHENTICATION_REQUIRED, "Roblox authorization is missing.")
        try:
            expires_at = float(credentials.get("expires_at") or 0)
        except (TypeError, ValueError):
            expires_at = 0
        if expires_at and expires_at <= time.time():
            raise IntegrationError(IntegrationErrorCode.AUTHORIZATION_EXPIRED, "Roblox access token has expired; refresh the authorization.")
        response = self._requester(self.USERINFO_URL, method="GET", token=token, timeout=REQUEST_TIMEOUT_SECONDS)
        if response.status == 401:
            raise IntegrationError(IntegrationErrorCode.AUTHENTICATION_REQUIRED, "Roblox rejected the access token; refresh or reconnect the account.")
        if response.status == 403:
            raise IntegrationError(IntegrationErrorCode.MISSING_PERMISSION, "Roblox denied user-information access; reauthorize with the minimum required scopes.")
        if response.status == 429:
            raise IntegrationError(IntegrationErrorCode.RATE_LIMITED, "Roblox rate limit reached; retry later.", retryable=True)
        if response.status != 200 or not isinstance(response.data, dict):
            raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Roblox user-information lookup failed.")
        data = response.data
        identity = str(data.get("sub") or "").strip()
        if not identity:
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "Roblox did not return a stable user identifier.")
        raw_scopes = credentials.get("scopes") or []
        if isinstance(raw_scopes, str):
            raw_scopes = raw_scopes.split()
        scopes = {str(scope).strip() for scope in raw_scopes if str(scope).strip()}
        if "openid" not in scopes:
            raise IntegrationError(IntegrationErrorCode.MISSING_PERMISSION, "Roblox token did not grant the required openid identity scope.")
        return data, response

    def validate_credentials(self, credentials: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
        data, _response = self._get_userinfo(credentials)
        identity = str(data.get("sub") or "").strip()
        raw_scopes = credentials.get("scopes") or []
        if isinstance(raw_scopes, str):
            raw_scopes = raw_scopes.split()
        scopes = tuple(sorted({str(scope).strip() for scope in raw_scopes if str(scope).strip()}))
        return identity, scopes

    def health_check(self, credentials: Mapping[str, Any]) -> tuple[bool, Mapping[str, Any]]:
        try:
            data, response = self._get_userinfo(credentials)
            return True, {
                "verified": True,
                "identity": str(data["sub"]),
                "username": str(data.get("preferred_username") or ""),
                "http_status": response.status,
                "source": "GET /oauth/v1/userinfo",
                "scopes": list(credentials.get("scopes") or []),
            }
        except IntegrationError as exc:
            if exc.code in (IntegrationErrorCode.AUTHENTICATION_REQUIRED, IntegrationErrorCode.AUTHORIZATION_EXPIRED):
                return False, {"error_code": exc.code.value, "status": ConnectionStatus.AUTHENTICATION_REQUIRED.value}
            if exc.code == IntegrationErrorCode.MISSING_PERMISSION:
                return False, {"error_code": exc.code.value, "status": ConnectionStatus.MISSING_PERMISSION.value}
            raise

    def execute(self, action: str, arguments: Mapping[str, Any], credentials: Mapping[str, Any]) -> tuple[Any, Mapping[str, Any]]:
        if action not in ("roblox.connection.test", "roblox.profile.read"):
            raise IntegrationError(IntegrationErrorCode.UNSUPPORTED_ACTION, "The Roblox connector does not declare that operation.")
        if action == "roblox.profile.read":
            scopes = set(credentials.get("scopes") or [])
            if "profile" not in scopes:
                raise IntegrationError(IntegrationErrorCode.MISSING_PERMISSION, "Roblox profile access requires the profile scope; reconnect with explicit consent.")
        data, response = self._get_userinfo(credentials)
        result = {"user_id": str(data["sub"])}
        if "profile" in set(credentials.get("scopes") or []):
            for field_name in ("preferred_username", "name", "nickname", "profile", "picture", "created_at"):
                if data.get(field_name) is not None:
                    result[field_name] = data[field_name]
        return result, {"verified": True, "source": "GET /oauth/v1/userinfo", "http_status": response.status, "identity": str(data["sub"])}

    def refresh_credentials(self, credentials: Mapping[str, Any]) -> dict[str, Any]:
        refresh_token = str(credentials.get("refresh_token") or "")
        client_id = str(credentials.get("client_id") or "")
        client_secret = str(credentials.get("client_secret") or "")
        if not refresh_token or not client_id or not client_secret:
            raise IntegrationError(IntegrationErrorCode.AUTHENTICATION_REQUIRED, "Roblox refresh credentials are missing; reconnect the account.")
        response = self._requester(
            self.TOKEN_URL,
            method="POST",
            form={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id, "client_secret": client_secret},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status != 200 or not isinstance(response.data, dict) or not response.data.get("access_token") or not response.data.get("refresh_token"):
            raise IntegrationError(IntegrationErrorCode.AUTHENTICATION_REQUIRED, "Roblox token refresh failed; reconnect this account.")
        updated = dict(credentials)
        updated["access_token"] = str(response.data["access_token"])
        updated["refresh_token"] = str(response.data["refresh_token"])
        updated["expires_at"] = time.time() + max(1, min(int(response.data.get("expires_in", 900)), 86400))
        if response.data.get("scope"):
            updated["scopes"] = sorted({item for item in str(response.data["scope"]).split() if item})
        return updated

    def revoke_credentials(self, credentials: Mapping[str, Any]) -> None:
        refresh_token = str(credentials.get("refresh_token") or "")
        client_id = str(credentials.get("client_id") or "")
        client_secret = str(credentials.get("client_secret") or "")
        if not refresh_token or not client_id or not client_secret:
            raise IntegrationError(IntegrationErrorCode.AUTHENTICATION_REQUIRED, "Roblox remote revocation credentials are missing.")
        response = self._requester(
            self.REVOCATION_URL,
            method="POST",
            form={"token": refresh_token, "client_id": client_id, "client_secret": client_secret},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status != 200:
            raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Roblox did not confirm remote token revocation.")


# Informational entries are intentionally not executable adapters. They keep a user
# informed without pretending that an existing legacy feature is a connected account.
PROVIDER_CATALOG: tuple[IntegrationManifest, ...] = (
    GitHubConnector.manifest,
    RobloxConnector.manifest,
    IntegrationManifest("amazon", "Amazon", "provider_specific", documentation_url="https://developer.amazon.com/", status=ConnectionStatus.LIMITED_SUPPORT, status_detail="Product research is separate from account authorization. Private orders, checkout, and purchases are not supported by this unified connector."),
    IntegrationManifest("google_workspace", "Google Workspace", "existing_legacy_connector", documentation_url="https://developers.google.com/identity/protocols/oauth2", status=ConnectionStatus.LIMITED_SUPPORT, status_detail="Existing Gmail/Calendar/Drive paths remain separate; they have not yet been migrated to the shared account registry."),
    IntegrationManifest("youtube", "YouTube", "oauth2", documentation_url="https://developers.google.com/youtube/v3/guides/authentication", status=ConnectionStatus.LIMITED_SUPPORT, status_detail="Existing video discovery remains separate; channel authorization and publishing are not claimed by this connector."),
    IntegrationManifest("instagram", "Instagram", "provider_specific", documentation_url="https://developers.facebook.com/docs/instagram-platform/", status=ConnectionStatus.LIMITED_SUPPORT, status_detail="A legacy browser/Instagram path exists but is not migrated; only official API permissions will be supported."),
    IntegrationManifest("outlook", "Outlook / Microsoft 365", "oauth2", documentation_url="https://learn.microsoft.com/entra/identity-platform/v2-oauth2-auth-code-flow", status=ConnectionStatus.UNAVAILABLE, status_detail="No shared-account adapter is implemented in this build."),
)


class IntegrationManager:
    """Registry, secure account vault, capability enforcement, and verified execution."""

    def __init__(self, store: SecureCredentialStore | None = None, *, adapters: tuple[Connector, ...] | list[Connector] = ()):
        self.store: SecureCredentialStore = store if store is not None else WindowsCredentialManager()
        self._adapters: dict[str, Connector] = {}
        self._lock = threading.RLock()
        self._history: deque[dict[str, Any]] = deque(maxlen=ACTIVITY_LIMIT)
        self._idempotent_results: dict[tuple[str, str], ActionResult] = {}
        self._pending: dict[str, tuple[str, str, dict[str, Any], str]] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: Connector) -> None:
        manifest = adapter.manifest
        provider_id = str(manifest.provider_id or "").strip().lower()
        if not provider_id or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for ch in provider_id):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Invalid integration provider identifier.")
        with self._lock:
            if provider_id in self._adapters:
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "An adapter is already registered for this provider.")
            seen: set[str] = set()
            for capability in manifest.capabilities:
                if not capability.action or capability.action in seen:
                    raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Integration manifest contains an empty or duplicate action.")
                seen.add(capability.action)
            self._adapters[provider_id] = adapter

    def connector(self, provider_id: str) -> Connector | None:
        """Return an adapter to trusted application code; do not expose it as a model tool."""
        return self._adapters.get(str(provider_id or "").strip().lower())

    def catalog(self) -> list[IntegrationManifest]:
        catalog = {item.provider_id: item for item in PROVIDER_CATALOG}
        for provider_id, adapter in self._adapters.items():
            catalog[provider_id] = adapter.manifest
        return [catalog[key] for key in sorted(catalog)]

    def capabilities(self, provider_id: str, *, scopes: tuple[str, ...] | list[str] = ()) -> list[dict[str, Any]]:
        adapter = self._adapters.get(str(provider_id or "").lower())
        if not adapter:
            manifest = next((item for item in PROVIDER_CATALOG if item.provider_id == str(provider_id or "").lower()), None)
            if manifest:
                return []
            raise IntegrationError(IntegrationErrorCode.UNSUPPORTED_ACTION, "Integration provider is not registered.")
        allowed_scopes = set(scopes)
        out = []
        for item in adapter.manifest.capabilities:
            missing = sorted(set(item.required_scopes) - allowed_scopes)
            out.append({
                "action": item.action,
                "description": item.description,
                "risk": item.risk.value,
                "required_scopes": list(item.required_scopes),
                "supported": bool(item.supported),
                "available": bool(item.supported and not missing),
                "missing_scopes": missing,
                "requires_confirmation": item.risk != RiskLevel.READ_ONLY,
                "idempotent": item.idempotent,
            })
        return out

    def connect(self, provider_id: str, credentials: Mapping[str, Any]) -> AccountSummary:
        provider_id = str(provider_id or "").strip().lower()
        adapter = self._adapters.get(provider_id)
        if adapter is None:
            raise IntegrationError(IntegrationErrorCode.UNSUPPORTED_ACTION, "This provider has no live connector in the current build.")
        if not self.store.available:
            raise IntegrationError(IntegrationErrorCode.STORAGE_UNAVAILABLE, "A secure credential store is unavailable; no account token has been saved.")
        safe_credentials = dict(credentials)
        identity, scopes = adapter.validate_credentials(safe_credentials)
        if not identity or not str(identity).strip():
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "The provider did not return a verified account identity.")
        # Validate identity here; individual capabilities enforce their own scopes at execution time.
        # This avoids requesting every possible permission merely to connect an account.
        account_id = str(uuid.uuid4())
        record = {
            "account_id": account_id,
            "provider_id": provider_id,
            "identity": str(identity)[:256],
            "status": ConnectionStatus.CONNECTED.value,
            "scopes": list(scopes),
            "connected_at": time.time(),
            "last_checked_at": time.time(),
            "last_error_code": None,
            "credentials": safe_credentials,
        }
        self.store.save(account_id, record)
        self._record_activity(provider_id, account_id, "account.connect", "succeeded_and_verified", {"identity_verified": True})
        return self._summary(account_id, record)

    @staticmethod
    def _summary(account_id: str, record: Mapping[str, Any]) -> AccountSummary:
        try:
            status = ConnectionStatus(str(record.get("status") or ConnectionStatus.AUTHENTICATION_REQUIRED.value))
        except ValueError:
            status = ConnectionStatus.AUTHENTICATION_REQUIRED
        return AccountSummary(
            account_id=account_id,
            provider_id=str(record.get("provider_id") or ""),
            identity=str(record.get("identity") or ""),
            status=status,
            scopes=tuple(sorted(str(item) for item in record.get("scopes", []) if item)),
            connected_at=float(record.get("connected_at") or 0),
            last_checked_at=float(record.get("last_checked_at") or 0),
            last_error_code=str(record.get("last_error_code")) if record.get("last_error_code") else None,
        )

    def list_accounts(self, provider_id: str | None = None) -> list[AccountSummary]:
        provider = str(provider_id or "").strip().lower()
        records = self.store.list_records()
        result = []
        for account_id, record in records:
            if provider and str(record.get("provider_id") or "").lower() != provider:
                continue
            if str(record.get("provider_id") or "") not in self._adapters:
                continue
            result.append(self._summary(account_id, record))
        return sorted(result, key=lambda account: (account.provider_id, account.identity.casefold(), account.connected_at))

    def test_connection(self, account_id: str) -> dict[str, Any]:
        account, adapter, record = self._require_account(account_id)
        ok, evidence = adapter.health_check(record["credentials"])
        changed = dict(record)
        changed["last_checked_at"] = time.time()
        if ok:
            changed["status"] = ConnectionStatus.CONNECTED.value
            changed["last_error_code"] = None
        else:
            changed["status"] = str(evidence.get("status") or ConnectionStatus.AUTHENTICATION_REQUIRED.value)
            changed["last_error_code"] = str(evidence.get("error_code") or IntegrationErrorCode.PROVIDER_ERROR.value)
        self.store.save(account.account_id, changed)
        self._record_activity(account.provider_id, account.account_id, "connection.test", "succeeded_and_verified" if ok else "failed", {"http_status": evidence.get("http_status"), "identity_verified": ok})
        return {"ok": ok, "account": self._summary(account.account_id, changed).__dict__, "evidence": dict(evidence)}

    def refresh_authorization(self, account_id: str) -> AccountSummary:
        account, adapter, record = self._require_account(account_id)
        refresh = getattr(adapter, "refresh_credentials", None)
        if not callable(refresh):
            raise IntegrationError(IntegrationErrorCode.AUTHENTICATION_REQUIRED, "This provider does not expose automatic refresh; reconnect the account.")
        new_credentials = refresh(record["credentials"])
        identity, scopes = adapter.validate_credentials(new_credentials)
        if identity.casefold() != account.identity.casefold():
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "Refreshed credentials resolved to a different account; refusing to replace the saved authorization.")
        changed = dict(record)
        changed["credentials"] = dict(new_credentials)
        changed["scopes"] = list(scopes)
        changed["status"] = ConnectionStatus.CONNECTED.value
        changed["last_checked_at"] = time.time()
        changed["last_error_code"] = None
        self.store.save(account_id, changed)
        self._record_activity(account.provider_id, account_id, "authorization.refresh", "succeeded_and_verified", {"identity_verified": True})
        return self._summary(account_id, changed)

    def disconnect(self, account_id: str) -> dict[str, Any]:
        account, adapter, record = self._require_account(account_id)
        provider_revocation = "not_performed"
        revocation_error = None
        revoke = getattr(adapter, "revoke_credentials", None)
        revoke_supported = bool(getattr(adapter, "supports_revocation", True))
        if callable(revoke) and revoke_supported:
            try:
                revoke(record["credentials"])
                provider_revocation = "succeeded"
            except IntegrationError as exc:
                provider_revocation = "failed"
                revocation_error = exc.code.value
            except Exception:
                provider_revocation = "failed"
                revocation_error = IntegrationErrorCode.PROVIDER_ERROR.value
        removed = self.store.delete(account_id)
        self._record_activity(account.provider_id, account_id, "account.disconnect", "succeeded_and_verified" if removed else "failed", {"local_credential_deleted": removed, "provider_revocation": provider_revocation})
        return {
            "ok": removed,
            "account_id": account_id,
            "status": ConnectionStatus.DISCONNECTED.value if removed else ConnectionStatus.AUTHENTICATION_REQUIRED.value,
            "provider_revocation": provider_revocation,
            "provider_revocation_error": revocation_error,
            "message": "Local credentials were deleted. " + (
                "The provider confirmed remote token revocation." if provider_revocation == "succeeded"
                else "Remote revocation failed; revoke the app grant in the provider's security settings if required." if provider_revocation == "failed"
                else "This connector did not perform remote revocation; revoke the app grant in the provider's security settings if required."
            ),
        }

    def execute(
        self,
        account_id: str,
        action: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> ActionResult:
        arguments = dict(arguments or {})
        if len(arguments) > 64:
            return self._result(ActionStatus.REJECTED, "", account_id, action, code=IntegrationErrorCode.INVALID_REQUEST, message="Action arguments exceed the safety limit.")
        try:
            account, adapter, record = self._require_account(account_id)
            capability = next((item for item in adapter.manifest.capabilities if item.action == action and item.supported), None)
            if capability is None:
                return self._result(ActionStatus.UNSUPPORTED, account.provider_id, account_id, action, code=IntegrationErrorCode.UNSUPPORTED_ACTION, message="This operation is not declared by the connected provider.")
            missing = sorted(set(capability.required_scopes) - set(account.scopes))
            if missing and not (missing == ["read:user"] and "user" in account.scopes):
                self._mark_account_status(account_id, record, ConnectionStatus.MISSING_PERMISSION, IntegrationErrorCode.MISSING_PERMISSION)
                return self._result(ActionStatus.REJECTED, account.provider_id, account_id, action, code=IntegrationErrorCode.MISSING_PERMISSION, message="The connected account has not granted the required permission.")
            cache_key = (account_id, str(idempotency_key or "").strip())
            if capability.idempotent and cache_key[1]:
                with self._lock:
                    cached = self._idempotent_results.get(cache_key)
                if cached is not None:
                    return cached
            if capability.risk != RiskLevel.READ_ONLY:
                safe_args = {str(key): value for key, value in arguments.items() if not _looks_secret(str(key))}
                pending_key = str(idempotency_key or "")
                now = time.time()
                with self._lock:
                    stale = [key for key, item in self._pending.items() if now - item[4] > PENDING_CONFIRMATION_TTL_SECONDS]
                    for key in stale:
                        self._pending.pop(key, None)
                    if capability.idempotent and pending_key:
                        existing = next((
                            (key, item) for key, item in self._pending.items()
                            if item[0] == account_id and item[1] == action and item[3] == pending_key
                        ), None)
                        if existing is not None:
                            confirmation_id = existing[0]
                        else:
                            confirmation_id = ""
                    else:
                        confirmation_id = ""
                    if not confirmation_id:
                        if len(self._pending) >= PENDING_CONFIRMATION_LIMIT:
                            return self._result(ActionStatus.REJECTED, account.provider_id, account_id, action, code=IntegrationErrorCode.INVALID_REQUEST, message="Too many pending confirmations. Resolve or cancel existing actions first.")
                        confirmation_id = secrets.token_urlsafe(24)
                        self._pending[confirmation_id] = (account_id, action, safe_args, pending_key, now)
                return ActionResult(
                    ActionStatus.WAITING_FOR_CONFIRMATION, account.provider_id, account_id, action,
                    message="Explicit user approval is required. No provider request has been sent.",
                    confirmation_id=confirmation_id,
                )
            result, evidence = adapter.execute(action, arguments, record["credentials"])
            if not isinstance(evidence, Mapping):
                raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Connector returned no valid verification evidence.")
            if evidence.get("verified") is True:
                final = ActionResult(ActionStatus.SUCCEEDED_VERIFIED, account.provider_id, account_id, action, result=result, verification_evidence={key: value for key, value in evidence.items() if key != "verified"}, message="Operation completed and the provider response verified the result.")
            else:
                final = ActionResult(ActionStatus.ACCEPTED_UNVERIFIED, account.provider_id, account_id, action, result=result, verification_evidence={key: value for key, value in evidence.items() if key != "verified"}, message="The provider returned a response, but independent verification is unavailable.")
            if capability.idempotent and cache_key[1]:
                with self._lock:
                    if len(self._idempotent_results) >= ACTIVITY_LIMIT:
                        self._idempotent_results.pop(next(iter(self._idempotent_results)))
                    self._idempotent_results[cache_key] = final
            self._record_activity(account.provider_id, account_id, action, final.status.value, final.verification_evidence)
            return final
        except IntegrationError as exc:
            code = exc.code
            status = ActionStatus.UNSUPPORTED if code == IntegrationErrorCode.UNSUPPORTED_ACTION else ActionStatus.FAILED
            if code in (IntegrationErrorCode.AUTHENTICATION_REQUIRED, IntegrationErrorCode.AUTHORIZATION_EXPIRED):
                try:
                    _, _, record = self._require_account(account_id)
                    self._mark_account_status(account_id, record, ConnectionStatus.AUTHENTICATION_REQUIRED, code)
                except IntegrationError:
                    pass
            elif code == IntegrationErrorCode.MISSING_PERMISSION:
                try:
                    _, _, record = self._require_account(account_id)
                    self._mark_account_status(account_id, record, ConnectionStatus.MISSING_PERMISSION, code)
                except IntegrationError:
                    pass
            return self._result(status, str(record.get("provider_id") if "record" in locals() else ""), account_id, action, code=code, message=str(exc), follow_up="Reconnect or grant the minimum required permission." if code in (IntegrationErrorCode.AUTHENTICATION_REQUIRED, IntegrationErrorCode.MISSING_PERMISSION) else None)
        except Exception:
            # Unexpected connector exceptions are deliberately hidden from user-visible results.
            return self._result(ActionStatus.FAILED, "", account_id, action, code=IntegrationErrorCode.PROVIDER_ERROR, message="The integration failed unexpectedly; sensitive provider details were not exposed.")

    def confirm_action(self, confirmation_id: str, *, approved: bool) -> ActionResult:
        """UI-only confirmation boundary; never expose this method directly to a model tool."""
        with self._lock:
            pending = self._pending.pop(str(confirmation_id or ""), None)
        if pending is None:
            return self._result(ActionStatus.REJECTED, "", None, "", code=IntegrationErrorCode.INVALID_REQUEST, message="Confirmation expired or is unknown; no action was executed.")
        account_id, action, arguments, _idempotency_key, created_at = pending
        if time.time() - float(created_at) > PENDING_CONFIRMATION_TTL_SECONDS:
            return self._result(ActionStatus.REJECTED, "", account_id, action, code=IntegrationErrorCode.AUTHORIZATION_EXPIRED, message="The confirmation expired; no provider action was executed.")
        if not approved:
            return self._result(ActionStatus.REJECTED, "", account_id, action, code=IntegrationErrorCode.CANCELLED, message="The user cancelled the action; no provider request was sent.")
        try:
            account, adapter, record = self._require_account(account_id)
            capability = next((item for item in adapter.manifest.capabilities if item.action == action and item.supported), None)
            if capability is None:
                return self._result(ActionStatus.UNSUPPORTED, account.provider_id, account_id, action, code=IntegrationErrorCode.UNSUPPORTED_ACTION, message="This operation is no longer declared by the provider; no action was executed.")
            missing = sorted(set(capability.required_scopes) - set(account.scopes))
            if missing and not (missing == ["read:user"] and "user" in account.scopes):
                return self._result(ActionStatus.REJECTED, account.provider_id, account_id, action, code=IntegrationErrorCode.MISSING_PERMISSION, message="Required provider permission is no longer present; no action was executed.")
            result, evidence = adapter.execute(action, arguments, record["credentials"])
            if not isinstance(evidence, Mapping):
                final = self._result(ActionStatus.OUTCOME_UNKNOWN, account.provider_id, account_id, action, code=IntegrationErrorCode.PROVIDER_ERROR, message="The action may have reached the provider, but no verification evidence was returned.")
                self._record_activity(account.provider_id, account_id, action, final.status.value)
                return final
            status = ActionStatus.SUCCEEDED_VERIFIED if evidence.get("verified") is True else ActionStatus.ACCEPTED_UNVERIFIED
            final = ActionResult(status, account.provider_id, account_id, action, result, {key: value for key, value in evidence.items() if key != "verified"}, message="User-approved action executed." if status == ActionStatus.SUCCEEDED_VERIFIED else "User-approved action was accepted but remains unverified.")
            if capability.idempotent and _idempotency_key:
                with self._lock:
                    if len(self._idempotent_results) >= ACTIVITY_LIMIT:
                        self._idempotent_results.pop(next(iter(self._idempotent_results)))
                    self._idempotent_results[(account_id, _idempotency_key)] = final
            self._record_activity(account.provider_id, account_id, action, status.value, final.verification_evidence)
            return final
        except IntegrationError as exc:
            ambiguous = (
                "capability" in locals()
                and not capability.idempotent
                and exc.code in (IntegrationErrorCode.NETWORK_ERROR, IntegrationErrorCode.RATE_LIMITED, IntegrationErrorCode.PROVIDER_ERROR)
            )
            status = ActionStatus.OUTCOME_UNKNOWN if ambiguous else (ActionStatus.UNSUPPORTED if exc.code == IntegrationErrorCode.UNSUPPORTED_ACTION else ActionStatus.FAILED)
            final = self._result(status, "", account_id, action, code=exc.code, message=str(exc), follow_up="Inspect provider state before retrying; the result may be ambiguous." if ambiguous else None)
            self._record_activity("", account_id, action, final.status.value)
            return final
        except Exception:
            # Once an approved provider request starts, an unexpected exception
            # is ambiguous rather than an automatic failure: avoid unsafe retries.
            final = self._result(ActionStatus.OUTCOME_UNKNOWN, "", account_id, action, code=IntegrationErrorCode.PROVIDER_ERROR, message="The approved action's outcome is unknown; inspect provider state before retrying.")
            self._record_activity("", account_id, action, final.status.value)
            return final

    def activity_history(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._history]

    def _require_account(self, account_id: str) -> tuple[AccountSummary, Connector, dict[str, Any]]:
        try:
            canonical = str(uuid.UUID(str(account_id)))
        except (ValueError, TypeError, AttributeError) as exc:
            raise IntegrationError(IntegrationErrorCode.ACCOUNT_NOT_FOUND, "Connected account was not found.") from exc
        record = self.store.read(canonical)
        if not record:
            raise IntegrationError(IntegrationErrorCode.ACCOUNT_NOT_FOUND, "Connected account was not found or was disconnected.")
        provider_id = str(record.get("provider_id") or "")
        adapter = self._adapters.get(provider_id)
        if adapter is None:
            raise IntegrationError(IntegrationErrorCode.UNSUPPORTED_ACTION, "No connector is registered for this account.")
        return self._summary(canonical, record), adapter, record

    def _mark_account_status(self, account_id: str, record: Mapping[str, Any], status: ConnectionStatus, code: IntegrationErrorCode) -> None:
        changed = dict(record)
        changed["status"] = status.value
        changed["last_error_code"] = code.value
        changed["last_checked_at"] = time.time()
        self.store.save(account_id, changed)

    @staticmethod
    def _result(status: ActionStatus, provider_id: str, account_id: str | None, action: str, *, code: IntegrationErrorCode, message: str, follow_up: str | None = None) -> ActionResult:
        return ActionResult(status, provider_id, account_id, action, error_code=code.value, message=message, follow_up=follow_up)

    def _record_activity(self, provider_id: str, account_id: str | None, action: str, status: str, evidence: Mapping[str, Any] | None = None) -> None:
        item = {
            "timestamp": time.time(),
            "provider_id": str(provider_id),
            "account_id": str(account_id) if account_id else None,
            "action": str(action)[:120],
            "status": str(status)[:80],
            "evidence": {key: value for key, value in dict(evidence or {}).items() if key in ("http_status", "source", "identity_verified", "local_credential_deleted", "items_returned", "provider_revocation")},
        }
        with self._lock:
            self._history.append(item)


def create_default_manager(store: SecureCredentialStore | None = None) -> IntegrationManager:
    """Create an app manager with the currently executable adapters only."""
    return IntegrationManager(store=store, adapters=[GitHubConnector(), RobloxConnector()])


_default_manager: IntegrationManager | None = None
_default_manager_lock = threading.Lock()


def get_default_manager() -> IntegrationManager:
    global _default_manager
    with _default_manager_lock:
        if _default_manager is None:
            _default_manager = create_default_manager()
        return _default_manager
