"""Generic OAuth2/OIDC onboarding and safe OpenAPI read-operation discovery.

This module deliberately generates read-only candidates from API descriptions.
Mutation operations remain blocked until a provider-specific adapter implements
them and Brahma's confirmation/verification contract can be applied.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from core.account_integrations import (
    ActionStatus,
    Capability,
    ConnectionStatus,
    HttpResponse,
    IntegrationError,
    IntegrationErrorCode,
    IntegrationManifest,
    RiskLevel,
    _sanitize_payload,
)

_MAX_SPEC_BYTES = 2 * 1024 * 1024
_MAX_SPEC_NODES = 50000
_MAX_OPERATIONS = 500
_MAX_RESPONSE_BYTES = 1024 * 1024
_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_OPERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_PATH_PARAM_RE = re.compile(r"\{([A-Za-z0-9_.-]+)\}")


@dataclass(frozen=True)
class OAuthProviderMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    userinfo_endpoint: str
    jwks_uri: str = ""
    revocation_endpoint: str = ""
    scopes_supported: tuple[str, ...] = ()


@dataclass(frozen=True)
class OpenAPIOperation:
    action: str
    summary: str
    path: str
    required_scopes: tuple[str, ...] = ()
    path_params: tuple[str, ...] = ()
    query_params: tuple[str, ...] = ()
    method: str = "GET"
    auth_type: str = "oauth2"
    api_key_header: str = ""

    def __post_init__(self):
        if not _OPERATION_RE.fullmatch(str(self.action or "")):
            raise ValueError("Operation action must be a stable identifier.")
        if str(self.method).upper() != "GET":
            raise ValueError("The generic OpenAPI connector only executes GET operations.")
        decoded_path = urllib.parse.unquote(str(self.path or ""))
        if not str(self.path).startswith("/") or "://" in self.path or "#" in self.path or "?" in self.path or "\\" in self.path or ".." in decoded_path.split("/"):
            raise ValueError("Operation path must be a safe relative API path.")
        placeholders = set(_PATH_PARAM_RE.findall(self.path))
        if placeholders != set(self.path_params):
            raise ValueError("Declared path parameters must exactly match the path template.")
        if len(set(self.query_params)) != len(self.query_params) or len(set(self.path_params)) != len(self.path_params):
            raise ValueError("Operation parameter names must be unique.")
        if self.auth_type not in ("oauth2", "oidc", "api_key", "bearer"):
            raise ValueError("Operation authentication type is unsupported.")
        if self.auth_type in ("api_key", "bearer"):
            if not re.fullmatch(r"[!#$%&'*+.^_|~0-9A-Za-z-]+", self.api_key_header):
                raise ValueError("API credential header name is invalid.")
        elif self.api_key_header:
            raise ValueError("OAuth operations may not declare a static credential header.")


def _normalized_host(value: str) -> str:
    return str(value or "").strip().rstrip(".").casefold()


def _check_https_url(
    value: str,
    *,
    allowed_hosts: Sequence[str] = (),
    allow_loopback_http: bool = False,
) -> urllib.parse.ParseResult:
    raw = str(value or "").strip()
    if not raw or len(raw) > 2048:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "A provider URL is missing or exceeds the supported length.")
    try:
        parsed = urllib.parse.urlparse(raw)
        host = _normalized_host(parsed.hostname or "")
        port = parsed.port
    except ValueError as exc:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "A provider URL is malformed.") from exc
    loopback_http = False
    if parsed.scheme == "http" and allow_loopback_http and host:
        try:
            loopback_http = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback_http = host == "localhost"
    if parsed.scheme != "https" and not loopback_http:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Provider endpoints must use HTTPS; only an exact loopback redirect may use HTTP.")
    if not host or parsed.username or parsed.password or parsed.fragment:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Provider URLs must have a host and must not embed credentials or fragments.")
    if port is not None and not (1 <= port <= 65535):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Provider URL port is invalid.")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if not loopback_http and (host == "localhost" or host.endswith(".localhost") or host.endswith(".local") or (ip is not None and not ip.is_global)):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Provider endpoints may not target local, private, or non-global IP addresses.")
    allow = {_normalized_host(item) for item in allowed_hosts if str(item or "").strip()}
    if allow and host not in allow:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Provider endpoint host is not in the explicit trust list.")
    return parsed


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _default_requester(
    url: str,
    *,
    method: str = "GET",
    form: Mapping[str, str] | None = None,
    token: str | None = None,
    timeout: float = 8.0,
) -> HttpResponse:
    """Bounded HTTPS JSON request that refuses redirects to prevent credential forwarding."""
    _check_https_url(url)
    body = urllib.parse.urlencode(dict(form)).encode("utf-8") if form is not None else None
    headers = {"Accept": "application/json", "User-Agent": "Brahma-Evo-Generic-Integration"}
    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = "Bearer " + str(token)
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(req, timeout=max(0.5, min(float(timeout), 15.0))) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            status = int(response.status)
            response_headers = dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        raw = exc.read(65536)
        status = int(exc.code)
        response_headers = dict(exc.headers.items()) if exc.headers else {}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise IntegrationError(IntegrationErrorCode.NETWORK_ERROR, "Provider request failed or timed out.", retryable=True) from exc
    if 300 <= status < 400:
        raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Provider redirected an API request; redirects are not followed by the secure integration client.")
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Provider response exceeded the 1 MiB safety limit.")
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Provider returned malformed JSON.") from exc
    if not isinstance(data, (dict, list)):
        raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Provider returned an unexpected response format.")
    return HttpResponse(status, response_headers, data)


def discover_oidc_metadata(
    issuer: str,
    *,
    trusted_hosts: Sequence[str] = (),
    requester: Callable[..., HttpResponse] | None = None,
) -> OAuthProviderMetadata:
    """Fetch and validate OIDC discovery metadata from a developer-approved issuer."""
    issuer_value = str(issuer or "").strip()
    parsed_issuer = _check_https_url(issuer_value, allowed_hosts=trusted_hosts)
    if parsed_issuer.query or parsed_issuer.fragment:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OIDC issuer identifiers may not contain a query or fragment.")
    issuer_host = _normalized_host(parsed_issuer.hostname or "")
    hosts = tuple(dict.fromkeys((issuer_host, *(_normalized_host(item) for item in trusted_hosts if item))))
    issuer_path = parsed_issuer.path.rstrip("/")
    discovery_url = urllib.parse.urlunparse((
        "https", parsed_issuer.netloc, issuer_path + "/.well-known/openid-configuration", "", "", ""
    ))
    fetch = requester or _default_requester
    response = fetch(discovery_url, method="GET", timeout=8.0)
    if response.status != 200 or not isinstance(response.data, dict):
        raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "OIDC discovery did not return a valid metadata object.")
    data = response.data
    if str(data.get("issuer") or "") != issuer_value:
        raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "OIDC discovery issuer does not exactly match the configured issuer.")
    auth = str(data.get("authorization_endpoint") or "")
    token = str(data.get("token_endpoint") or "")
    userinfo = str(data.get("userinfo_endpoint") or "")
    jwks = str(data.get("jwks_uri") or "")
    if not auth or not token or not userinfo or not jwks:
        raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "OIDC metadata must provide authorization, token, userinfo, and JWKS endpoints.")
    for endpoint in (auth, token, userinfo, jwks):
        _check_https_url(endpoint, allowed_hosts=hosts)
    revocation = str(data.get("revocation_endpoint") or "")
    if revocation:
        _check_https_url(revocation, allowed_hosts=hosts)
    challenge_methods = data.get("code_challenge_methods_supported")
    if challenge_methods is not None and "S256" not in challenge_methods:
        raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "OIDC provider metadata does not advertise PKCE S256 support.")
    response_types = data.get("response_types_supported")
    if response_types is not None and "code" not in response_types:
        raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "OIDC provider does not advertise the authorization-code response type.")
    return OAuthProviderMetadata(
        issuer=issuer_value,
        authorization_endpoint=auth,
        token_endpoint=token,
        userinfo_endpoint=userinfo,
        jwks_uri=jwks,
        revocation_endpoint=revocation,
        scopes_supported=tuple(str(item) for item in (data.get("scopes_supported") or []) if isinstance(item, str)),
    )


def _load_openapi_spec(spec: str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(spec, str):
        if len(spec.encode("utf-8")) > _MAX_SPEC_BYTES:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI description exceeds the 2 MiB onboarding limit.")
        try:
            loaded = json.loads(spec)
        except json.JSONDecodeError as exc:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Supply an OpenAPI JSON document; YAML conversion is not enabled in this safe onboarding path.") from exc
    elif isinstance(spec, Mapping):
        loaded = dict(spec)
    else:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI description must be a JSON object or JSON string.")
    if not isinstance(loaded, dict) or not str(loaded.get("openapi") or "").startswith(("3.0.", "3.1.")):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Only OpenAPI 3.0 and 3.1 documents are supported.")
    visited = 0

    def check_refs(value: Any, depth: int = 0):
        nonlocal visited
        visited += 1
        if visited > _MAX_SPEC_NODES or depth > 40:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI description exceeds safe structural complexity limits.")
        if isinstance(value, Mapping):
            ref = value.get("$ref")
            if ref is not None and (not isinstance(ref, str) or not ref.startswith("#/")):
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "External OpenAPI references are disabled; bundle the specification before onboarding.")
            for item in value.values():
                check_refs(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                check_refs(item, depth + 1)

    check_refs(loaded)
    return loaded


def analyze_openapi_spec(
    spec: str | Mapping[str, Any],
    *,
    provider_id: str = "custom",
    trusted_server_hosts: Sequence[str] = (),
    max_operations: int = 250,
) -> dict[str, Any]:
    """Return candidate GET operations and separately list blocked mutating routes.

    Candidates are not registered or executable by this analysis function alone.
    """
    provider = str(provider_id or "").strip().lower()
    if not _PROVIDER_RE.fullmatch(provider):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Provider identifier must use lowercase letters, digits, underscores, or hyphens.")
    document = _load_openapi_spec(spec)
    servers = document.get("servers")
    if not isinstance(servers, list) or len(servers) != 1 or not isinstance(servers[0], Mapping):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Provide exactly one explicit API server; implicit or multi-server routing requires manual adapter review.")
    server_url = str(servers[0].get("url") or "")
    if "{" in server_url or "}" in server_url:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Templated API server URLs require manual provider review.")
    _check_https_url(server_url, allowed_hosts=trusted_server_hosts)
    paths = document.get("paths")
    if not isinstance(paths, Mapping) or len(paths) > _MAX_OPERATIONS:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI document has no valid paths or exceeds the path limit.")
    schemes = document.get("components", {}).get("securitySchemes", {}) if isinstance(document.get("components"), Mapping) else {}
    root_security = document.get("security", [])
    read_candidates = []
    mutation_candidates = []
    seen_ids: set[str] = set()
    warnings: list[str] = []
    for path, path_item in paths.items():
        if not isinstance(path, str) or not path.startswith("/") or "://" in path or ".." in path.split("/") or not isinstance(path_item, Mapping):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI contains an unsafe or malformed path.")
        path_parameters = path_item.get("parameters", [])
        for method in ("get", "post", "put", "patch", "delete"):
            operation = path_item.get(method)
            if not isinstance(operation, Mapping):
                continue
            operation_id = str(operation.get("operationId") or "")
            summary = str(operation.get("summary") or operation.get("description") or operation_id or (method.upper() + " " + path))[:400]
            candidate = {"operation_id": operation_id, "method": method.upper(), "path": path, "summary": summary}
            if method == "get":
                if not operation_id or not _OPERATION_RE.fullmatch(operation_id) or operation_id in seen_ids:
                    warnings.append("Skipped a GET route with a missing, invalid, or duplicate operationId.")
                    continue
                if operation.get("deprecated") is True:
                    warnings.append("Skipped deprecated GET operation " + operation_id + ".")
                    continue
                declared_security = operation.get("security", root_security)
                if not isinstance(declared_security, list) or not declared_security:
                    warnings.append("Skipped GET operation " + operation_id + " because it lacks explicit authentication requirements.")
                    continue
                if len(declared_security) != 1 or not isinstance(declared_security[0], Mapping) or len(declared_security[0]) != 1:
                    warnings.append("Skipped GET operation " + operation_id + " because its security alternatives require manual review.")
                    continue
                scheme_name, scopes = next(iter(declared_security[0].items()))
                scheme = schemes.get(scheme_name) if isinstance(schemes, Mapping) else None
                if not isinstance(scheme, Mapping):
                    warnings.append("Skipped GET operation " + operation_id + " because its security scheme is missing.")
                    continue
                scheme_type = str(scheme.get("type") or "")
                auth_type = ""
                api_key_header = ""
                if scheme_type in ("oauth2", "openIdConnect"):
                    auth_type = "oidc" if scheme_type == "openIdConnect" else "oauth2"
                elif scheme_type == "apiKey" and scheme.get("in") == "header":
                    api_key_header = str(scheme.get("name") or "")
                    if not re.fullmatch(r"[!#$%&'*+.^_|~0-9A-Za-z-]+", api_key_header):
                        warnings.append("Skipped GET operation " + operation_id + " because its API-key header name is invalid.")
                        continue
                    auth_type = "api_key"
                    if scopes:
                        warnings.append("Skipped GET operation " + operation_id + " because API-key security must not declare OAuth scopes.")
                        continue
                elif scheme_type == "http" and str(scheme.get("scheme") or "").casefold() == "bearer":
                    auth_type = "bearer"
                    api_key_header = "Authorization"
                    if scopes:
                        warnings.append("Skipped GET operation " + operation_id + " because HTTP bearer security must not declare OAuth scopes.")
                        continue
                else:
                    warnings.append("Skipped GET operation " + operation_id + " because its auth method is not supported by the generic executor (query/cookie API keys are intentionally disabled).")
                    continue
                if not isinstance(scopes, list) or any(not isinstance(scope, str) for scope in scopes):
                    warnings.append("Skipped GET operation " + operation_id + " due to invalid security scopes.")
                    continue
                all_parameters = list(path_parameters) if isinstance(path_parameters, list) else []
                operation_parameters = operation.get("parameters", [])
                if isinstance(operation_parameters, list):
                    all_parameters.extend(operation_parameters)
                path_params = tuple(sorted(set(_PATH_PARAM_RE.findall(path))))
                query_params = []
                declared_path_params = set()
                valid_params = True
                for parameter in all_parameters:
                    if not isinstance(parameter, Mapping):
                        valid_params = False
                        break
                    loc = parameter.get("in")
                    name = str(parameter.get("name") or "")
                    schema = parameter.get("schema")
                    if loc not in ("path", "query") or not name or not isinstance(schema, Mapping):
                        valid_params = False
                        break
                    if schema.get("type") not in ("string", "integer", "number", "boolean"):
                        valid_params = False
                        break
                    if loc == "path":
                        if name not in path_params or parameter.get("required") is not True:
                            valid_params = False
                            break
                        declared_path_params.add(name)
                    if loc == "query":
                        query_params.append(name)
                if (not valid_params or declared_path_params != set(path_params)
                    or len(set(query_params)) != len(query_params)
                    or set(query_params) & set(path_params)):
                    warnings.append("Skipped GET operation " + operation_id + " because its parameters could not be validated safely.")
                    continue
                seen_ids.add(operation_id)
                read_candidates.append({
                    "action": provider + "." + operation_id,
                    "operation_id": operation_id,
                    "summary": summary,
                    "method": "GET",
                    "path": path,
                    "required_scopes": sorted(set(scopes)),
                    "path_params": list(path_params),
                    "query_params": sorted(set(query_params)),
                    "auth_scheme": str(scheme_name),
                    "auth_type": auth_type,
                    "api_key_header": api_key_header,
                    "supported": True,
                    "requires_manual_approval": True,
                })
            else:
                mutation_candidates.append({
                    **candidate,
                    "risk": "destructive_or_security_sensitive" if method == "DELETE" else "reversible_change_or_external_effect",
                    "supported": False,
                    "requires_manual_approval": True,
                })
    if len(read_candidates) > min(max_operations, _MAX_OPERATIONS):
        read_candidates = read_candidates[: min(max_operations, _MAX_OPERATIONS)]
        warnings.append("Read-only candidate list was truncated to the configured operation limit.")
    return {
        "provider_id": provider,
        "openapi": document["openapi"],
        "server_url": server_url,
        "read_only_candidates": read_candidates,
        "mutation_candidates": mutation_candidates[:_MAX_OPERATIONS],
        "warnings": warnings,
        "registered": False,
        "note": "This is an onboarding preview. Candidates are not exposed for execution until an approved adapter registers the exact operations and passes contract tests.",
    }


def operations_from_openapi(preview: Mapping[str, Any]) -> tuple[OpenAPIOperation, ...]:
    """Build GET operation descriptors from an analyzed preview for a reviewed adapter."""
    if preview.get("registered") is not False or not isinstance(preview.get("read_only_candidates"), list):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Use a validated OpenAPI preview before creating operation descriptors.")
    operations = []
    for item in preview["read_only_candidates"]:
        if not isinstance(item, Mapping) or item.get("supported") is not True or item.get("requires_manual_approval") is not True:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI candidate did not satisfy the safe preview contract.")
        operations.append(OpenAPIOperation(
            action=str(item["action"]),
            summary=str(item.get("summary") or item["operation_id"]),
            path=str(item["path"]),
            required_scopes=tuple(str(x) for x in item.get("required_scopes", [])),
            path_params=tuple(str(x) for x in item.get("path_params", [])),
            query_params=tuple(str(x) for x in item.get("query_params", [])),
            method=str(item.get("method") or "GET"),
            auth_type=str(item.get("auth_type") or "oauth2"),
            api_key_header=str(item.get("api_key_header") or ""),
        ))
    return tuple(operations)


class OAuth2PKCEConnector:
    """Reusable authorization-code/PKCE connector with OIDC ID-token validation.

    Provider API operations are intentionally limited to exact GET routes derived
    from a reviewed OpenAPI preview. Provider-specific mutation behavior needs a
    separate adapter implementing Brahma's risk, confirmation and verification rules.
    """

    def __init__(
        self,
        *,
        provider_id: str,
        display_name: str,
        client_id: str,
        redirect_uri: str,
        metadata: OAuthProviderMetadata,
        api_base_url: str,
        operations: Sequence[OpenAPIOperation] = (),
        requested_scopes: Sequence[str] = ("openid", "profile"),
        identity_field: str = "sub",
        client_secret: str = "",
        trusted_hosts: Sequence[str] = (),
        requester: Callable[..., HttpResponse] | None = None,
        use_oidc: bool = True,
    ):
        self.provider_id = str(provider_id or "").strip().lower()
        if not _PROVIDER_RE.fullmatch(self.provider_id):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Invalid provider identifier.")
        self.client_id = str(client_id or "").strip()
        if not self.client_id or len(self.client_id) > 512 or any(ch.isspace() for ch in self.client_id):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "A valid OAuth client ID is required.")
        self.client_secret = str(client_secret or "")
        self.redirect_uri = str(redirect_uri or "").strip()
        _check_https_url(self.redirect_uri, allow_loopback_http=True)
        if "?" in self.redirect_uri or "#" in self.redirect_uri:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OAuth redirect URI may not contain query or fragment data.")
        parsed_redirect = urllib.parse.urlparse(self.redirect_uri)
        if parsed_redirect.scheme == "http":
            try:
                if not ipaddress.ip_address(parsed_redirect.hostname or "").is_loopback:
                    raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "HTTP OAuth redirects are permitted only on IP loopback.")
            except ValueError as exc:
                if parsed_redirect.hostname != "localhost":
                    raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "HTTP OAuth redirects are permitted only on IP loopback.") from exc
        self.metadata = metadata
        self.requested_scopes = tuple(dict.fromkeys(str(scope).strip() for scope in requested_scopes if str(scope).strip()))
        self.identity_field = str(identity_field or "sub").strip()
        if not self.identity_field or len(self.identity_field) > 128 or any(not part for part in self.identity_field.split(".")):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Identity field must be a dotted claim or response path.")
        self.use_oidc = bool(use_oidc)
        if self.use_oidc and ("openid" not in self.requested_scopes or not self.metadata.issuer or not self.metadata.jwks_uri):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OIDC mode requires the openid scope, issuer, and JWKS metadata.")
        if self.use_oidc and self.identity_field != "sub":
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OIDC account identity must use the stable subject (sub) claim.")
        hosts = set(_normalized_host(item) for item in trusted_hosts if item)
        if metadata.issuer:
            issuer = _check_https_url(metadata.issuer, allowed_hosts=tuple(hosts))
            hosts.add(_normalized_host(issuer.hostname or ""))
        if not hosts:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Declare the provider's trusted HTTPS hosts explicitly.")
        for endpoint in (metadata.authorization_endpoint, metadata.token_endpoint, metadata.userinfo_endpoint):
            _check_https_url(endpoint, allowed_hosts=tuple(hosts))
        if metadata.jwks_uri:
            _check_https_url(metadata.jwks_uri, allowed_hosts=tuple(hosts))
        if metadata.revocation_endpoint:
            _check_https_url(metadata.revocation_endpoint, allowed_hosts=tuple(hosts))
        self.trusted_hosts = tuple(sorted(hosts))
        self.api_base_url = str(api_base_url or "").rstrip("/")
        base = _check_https_url(self.api_base_url, allowed_hosts=self.trusted_hosts)
        if base.query or base.fragment:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "API base URL may not contain a query or fragment.")
        self._requester = requester or _default_requester
        self.operations = {item.action: item for item in operations}
        if len(self.operations) != len(tuple(operations)):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Duplicate API operations are not allowed.")
        for operation in self.operations.values():
            if not operation.action.startswith(self.provider_id + "."):
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Operation actions must use the configured provider prefix.")
            if operation.auth_type not in ("oauth2", "oidc"):
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OAuth connectors may only execute operations configured for OAuth2/OIDC authentication.")
        caps = [
            Capability(
                item.action, item.summary, RiskLevel.READ_ONLY, item.required_scopes,
                idempotent=True, supported=True
            )
            for item in self.operations.values()
        ]
        caps.append(Capability(
            self.provider_id + ".connection.test",
            "Verify authorization and the stable account identity.",
            RiskLevel.READ_ONLY,
            (),
            idempotent=True,
            supported=True,
        ))
        self.manifest = IntegrationManifest(
            provider_id=self.provider_id,
            display_name=str(display_name or self.provider_id)[:120],
            auth_method="oauth2_authorization_code_pkce_oidc" if self.use_oidc else "oauth2_authorization_code_pkce",
            documentation_url=self.metadata.issuer or self.metadata.authorization_endpoint,
            status=ConnectionStatus.LIMITED_SUPPORT,
            status_detail="Generic OAuth2 authorization-code/PKCE adapter; only explicitly configured read-only API operations are executable.",
            authentication_documentation_url=self.metadata.issuer or self.metadata.authorization_endpoint,
            identity_validation_method="Validated OIDC RS256 ID token plus matching UserInfo sub" if self.use_oidc else "Configured HTTPS UserInfo endpoint and explicit stable identity field",
            supports_token_expiration=True,
            supports_refresh=True,
            supports_revocation=bool(self.metadata.revocation_endpoint),
            pagination_strategy="Provider-specific; no automatic multi-page traversal is inferred from OpenAPI.",
            rate_limit_behavior="Surface 401/403/429 as typed errors; no automatic replay.",
            setup_requirements=("Official OAuth client ID", "Exact registered redirect URI", "Explicit minimum scopes", "Trusted endpoint host allowlist", "Reviewed OpenAPI JSON specification"),
            limitations=("Only reviewed GET operations are executable.", "Refresh works only when the provider issues a refresh token.", "No generic browser automation, write operation, or arbitrary endpoint discovery."),
            capabilities=tuple(caps),
        )

    @property
    def supports_revocation(self) -> bool:
        return bool(self.metadata.revocation_endpoint)

    def begin_authorization(self) -> dict[str, Any]:
        now = time.time()
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        nonce = secrets.token_urlsafe(32) if self.use_oidc else ""
        scopes = list(self.requested_scopes)
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if nonce:
            params["nonce"] = nonce
        separator = "&" if urllib.parse.urlparse(self.metadata.authorization_endpoint).query else "?"
        return {
            "provider_id": self.provider_id,
            "state": state,
            "nonce": nonce,
            "code_verifier": verifier,
            "expires_at": now + 600,
            "redirect_uri": self.redirect_uri,
            "authorization_url": self.metadata.authorization_endpoint + separator + urllib.parse.urlencode(params),
        }

    @staticmethod
    def _parse_callback(flow: Mapping[str, Any], callback_url: str) -> tuple[str, str]:
        try:
            expected = urllib.parse.urlparse(str(flow.get("redirect_uri") or ""))
            callback = urllib.parse.urlparse(str(callback_url or ""))
            expected_port = expected.port or (443 if expected.scheme == "https" else 80)
            callback_port = callback.port or (443 if callback.scheme == "https" else 80)
        except ValueError as exc:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OAuth callback URL is malformed.") from exc
        if (
            callback.scheme != expected.scheme
            or _normalized_host(callback.hostname or "") != _normalized_host(expected.hostname or "")
            or callback_port != expected_port
            or callback.path != expected.path
            or callback.username or callback.password or callback.fragment
        ):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OAuth callback did not match the exact registered redirect URI.")
        try:
            query = urllib.parse.parse_qs(callback.query, keep_blank_values=True, max_num_fields=20, strict_parsing=False)
        except ValueError as exc:
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "OAuth callback query is malformed.") from exc
        states = query.get("state") or []
        if len(states) != 1 or not hmac.compare_digest(str(states[0]), str(flow.get("state") or "")):
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "OAuth state did not match; the callback was rejected.")
        codes = query.get("code") or []
        errors = query.get("error") or []
        if len(errors) > 1 or len(codes) > 1:
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "OAuth callback contains duplicated authorization parameters.")
        if errors:
            raise IntegrationError(IntegrationErrorCode.AUTHORIZATION_DENIED, "OAuth authorization was denied or cancelled.")
        if len(codes) != 1 or not codes[0] or len(codes[0]) > 2048:
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "OAuth callback must contain exactly one authorization code.")
        return codes[0], states[0]

    def complete_authorization(self, flow: Mapping[str, Any], callback_url: str) -> dict[str, Any]:
        if not isinstance(flow, Mapping) or str(flow.get("provider_id") or "") != self.provider_id:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "No matching OAuth authorization flow is active.")
        if time.time() >= float(flow.get("expires_at") or 0):
            raise IntegrationError(IntegrationErrorCode.AUTHORIZATION_EXPIRED, "OAuth authorization flow expired; start a new connection.")
        code, _state = self._parse_callback(flow, callback_url)
        response = self._requester(
            self.metadata.token_endpoint,
            method="POST",
            form={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "client_id": self.client_id,
                "code_verifier": str(flow.get("code_verifier") or ""),
                **({"client_secret": self.client_secret} if self.client_secret else {}),
            },
            timeout=8.0,
        )
        data = response.data
        if response.status in (400, 401):
            raise IntegrationError(IntegrationErrorCode.AUTHENTICATION_REQUIRED, "Provider rejected the authorization code; start a new connection.")
        if response.status == 429:
            raise IntegrationError(IntegrationErrorCode.RATE_LIMITED, "Provider rate limit reached during authorization.", retryable=True)
        if response.status != 200 or not isinstance(data, Mapping) or not data.get("access_token"):
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "Provider did not return an access token.")
        token_type = str(data.get("token_type") or "Bearer")
        if token_type.casefold() != "bearer":
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "Provider returned an unsupported token type.")
        if self.use_oidc:
            token = str(data.get("id_token") or "")
            if not token:
                raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "OIDC token response did not contain an ID token.")
            id_claims = self._validate_id_token(token, str(flow.get("nonce") or ""))
        else:
            id_claims = {}
        access_token = str(data["access_token"])
        userinfo = self._userinfo(access_token)
        identity = self._claim(userinfo, self.identity_field)
        if not identity:
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "Provider user-information response did not contain a stable identity.")
        if self.use_oidc and str(userinfo.get("sub") or "") != str(id_claims.get("sub") or ""):
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "OIDC UserInfo subject did not match the validated ID token subject.")
        try:
            expires_in = max(1, min(int(data.get("expires_in", 3600)), 31 * 24 * 3600))
        except (TypeError, ValueError) as exc:
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "Provider returned an invalid token lifetime.") from exc
        raw_scopes = data.get("scope")
        scopes = sorted(set(str(raw_scopes).split()) if raw_scopes else set(self.requested_scopes))
        return {
            "access_token": access_token,
            "refresh_token": str(data.get("refresh_token") or ""),
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "expires_at": time.time() + expires_in,
            "scopes": scopes,
            "token_type": token_type,
            "identity": str(identity),
        }

    def _validate_id_token(self, token: str, expected_nonce: str) -> dict[str, Any]:
        parts = str(token).split(".")
        if len(parts) != 3:
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "OIDC ID token is not a signed JWT.")
        try:
            header = json.loads(base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4)))
            claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            signature = base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4))
        except Exception as exc:
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "OIDC ID token encoding is malformed.") from exc
        if not isinstance(header, Mapping) or not isinstance(claims, Mapping) or header.get("alg") != "RS256" or not header.get("kid"):
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "OIDC ID token uses an unsupported or missing signing algorithm.")
        if str(claims.get("iss") or "") != self.metadata.issuer:
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "OIDC ID token issuer did not match the configured issuer.")
        audiences = claims.get("aud")
        if isinstance(audiences, str):
            audiences = [audiences]
        if (not isinstance(audiences, list) or self.client_id not in audiences
            or (len(audiences) > 1 and claims.get("azp") != self.client_id)
            or (claims.get("azp") is not None and claims.get("azp") != self.client_id)):
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "OIDC ID token audience did not match this client.")
        try:
            now = time.time()
            if float(claims.get("exp", 0)) <= now or float(claims.get("iat", now + 9999)) > now + 120:
                raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "OIDC ID token has expired or has an invalid issue time.")
            if claims.get("nbf") is not None and float(claims["nbf"]) > now + 120:
                raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "OIDC ID token is not yet valid.")
        except (TypeError, ValueError) as exc:
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "OIDC ID token contains invalid time claims.") from exc
        if not expected_nonce or not hmac.compare_digest(str(claims.get("nonce") or ""), expected_nonce):
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "OIDC ID token nonce did not match the active flow.")
        if not str(claims.get("sub") or "").strip():
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "OIDC ID token has no subject claim.")
        response = self._requester(self.metadata.jwks_uri, method="GET", timeout=8.0)
        keys = response.data.get("keys") if response.status == 200 and isinstance(response.data, Mapping) else None
        if not isinstance(keys, list) or len(keys) > 100:
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "OIDC JWKS response is malformed.")
        key = next((item for item in keys if isinstance(item, Mapping) and item.get("kid") == header["kid"]
                    and item.get("kty") == "RSA" and item.get("use") in (None, "sig")
                    and item.get("alg") in (None, "RS256")), None)
        if not key:
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "OIDC signing key was not found in the issuer's JWKS.")
        try:
            modulus = int.from_bytes(base64.urlsafe_b64decode(str(key["n"]) + "=" * (-len(str(key["n"])) % 4)), "big")
            exponent = int.from_bytes(base64.urlsafe_b64decode(str(key["e"]) + "=" * (-len(str(key["e"])) % 4)), "big")
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding, rsa
            public_key = rsa.RSAPublicNumbers(exponent, modulus).public_key()
            public_key.verify(signature, (parts[0] + "." + parts[1]).encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        except Exception as exc:
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "OIDC ID token signature verification failed.") from exc
        return dict(claims)

    @staticmethod
    def _claim(data: Mapping[str, Any], path: str) -> str:
        current: Any = data
        for part in path.split("."):
            if not isinstance(current, Mapping):
                return ""
            current = current.get(part)
        if current is None or isinstance(current, (dict, list, bool)):
            return ""
        return str(current).strip()[:256]

    def _userinfo(self, token: str) -> dict[str, Any]:
        response = self._requester(self.metadata.userinfo_endpoint, method="GET", token=token, timeout=8.0)
        if response.status == 401:
            raise IntegrationError(IntegrationErrorCode.AUTHENTICATION_REQUIRED, "Provider rejected the access token; refresh or reconnect the account.")
        if response.status == 403:
            raise IntegrationError(IntegrationErrorCode.MISSING_PERMISSION, "Provider denied access to user information.")
        if response.status == 429:
            raise IntegrationError(IntegrationErrorCode.RATE_LIMITED, "Provider rate limit reached; retry later.", retryable=True)
        if response.status != 200 or not isinstance(response.data, Mapping):
            raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Provider user-information request failed.")
        return dict(response.data)

    def validate_credentials(self, credentials: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
        token = str(credentials.get("access_token") or "")
        if not token:
            raise IntegrationError(IntegrationErrorCode.AUTHENTICATION_REQUIRED, "Provider authorization is missing.")
        try:
            expires_at = float(credentials.get("expires_at") or 0)
        except (TypeError, ValueError):
            expires_at = 0
        if expires_at and expires_at <= time.time():
            raise IntegrationError(IntegrationErrorCode.AUTHORIZATION_EXPIRED, "Provider access token has expired; refresh or reconnect the account.")
        userinfo = self._userinfo(token)
        identity = self._claim(userinfo, self.identity_field)
        if not identity:
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "Provider user-information response did not contain a stable identity.")
        scopes_value = credentials.get("scopes") or []
        if isinstance(scopes_value, str):
            scopes_value = scopes_value.split()
        scopes = tuple(sorted({str(scope).strip() for scope in scopes_value if str(scope).strip()}))
        return identity, scopes

    def health_check(self, credentials: Mapping[str, Any]) -> tuple[bool, Mapping[str, Any]]:
        try:
            identity, scopes = self.validate_credentials(credentials)
            return True, {"verified": True, "identity": identity, "scopes": list(scopes), "source": "configured userinfo endpoint", "http_status": 200}
        except IntegrationError as exc:
            if exc.code in (IntegrationErrorCode.AUTHENTICATION_REQUIRED, IntegrationErrorCode.AUTHORIZATION_EXPIRED, IntegrationErrorCode.MISSING_PERMISSION):
                status = ConnectionStatus.MISSING_PERMISSION if exc.code == IntegrationErrorCode.MISSING_PERMISSION else ConnectionStatus.AUTHENTICATION_REQUIRED
                return False, {"error_code": exc.code.value, "status": status.value}
            raise

    def refresh_credentials(self, credentials: Mapping[str, Any]) -> dict[str, Any]:
        refresh_token = str(credentials.get("refresh_token") or "")
        if not refresh_token:
            raise IntegrationError(IntegrationErrorCode.AUTHENTICATION_REQUIRED, "This provider did not issue a refresh token; reconnect the account.")
        form = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": self.client_id}
        if self.client_secret:
            form["client_secret"] = self.client_secret
        response = self._requester(self.metadata.token_endpoint, method="POST", form=form, timeout=8.0)
        data = response.data
        if response.status == 429:
            raise IntegrationError(IntegrationErrorCode.RATE_LIMITED, "Provider rate limit reached during token refresh.", retryable=True)
        if response.status in (400, 401) or not isinstance(data, Mapping) or not data.get("access_token"):
            raise IntegrationError(IntegrationErrorCode.AUTHENTICATION_REQUIRED, "Provider token refresh failed; reconnect the account.")
        updated = dict(credentials)
        updated["access_token"] = str(data["access_token"])
        updated["refresh_token"] = str(data.get("refresh_token") or refresh_token)
        try:
            updated["expires_at"] = time.time() + max(1, min(int(data.get("expires_in", 3600)), 31 * 24 * 3600))
        except (TypeError, ValueError) as exc:
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "Provider returned an invalid token lifetime.") from exc
        if data.get("scope"):
            updated["scopes"] = sorted(set(str(data["scope"]).split()))
        return updated

    def revoke_credentials(self, credentials: Mapping[str, Any]) -> None:
        if not self.supports_revocation:
            raise IntegrationError(IntegrationErrorCode.UNSUPPORTED_ACTION, "Provider metadata does not declare a revocation endpoint.")
        token = str(credentials.get("refresh_token") or credentials.get("access_token") or "")
        if not token:
            raise IntegrationError(IntegrationErrorCode.AUTHENTICATION_REQUIRED, "No provider token is available for remote revocation.")
        form = {"token": token, "client_id": self.client_id}
        if self.client_secret:
            form["client_secret"] = self.client_secret
        response = self._requester(self.metadata.revocation_endpoint, method="POST", form=form, timeout=8.0)
        if response.status not in (200, 204):
            raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Provider did not confirm remote token revocation.")

    def execute(self, action: str, arguments: Mapping[str, Any], credentials: Mapping[str, Any]) -> tuple[Any, Mapping[str, Any]]:
        if action == self.provider_id + ".connection.test":
            identity, scopes = self.validate_credentials(credentials)
            return {"identity": identity, "scopes": list(scopes)}, {"verified": True, "source": "configured userinfo endpoint", "http_status": 200, "identity": identity}
        operation = self.operations.get(action)
        if operation is None:
            raise IntegrationError(IntegrationErrorCode.UNSUPPORTED_ACTION, "The provider does not declare this operation.")
        try:
            expires_at = float(credentials.get("expires_at") or 0)
        except (TypeError, ValueError):
            expires_at = 0
        if expires_at and expires_at <= time.time():
            raise IntegrationError(IntegrationErrorCode.AUTHORIZATION_EXPIRED, "Provider access token has expired; refresh or reconnect the account.")
        raw_scopes = credentials.get("scopes") or []
        if isinstance(raw_scopes, str):
            raw_scopes = raw_scopes.split()
        missing = sorted(set(operation.required_scopes) - {str(scope).strip() for scope in raw_scopes if str(scope).strip()})
        if missing:
            raise IntegrationError(IntegrationErrorCode.MISSING_PERMISSION, "The connected account has not granted the scopes required by this operation.")
        args = dict(arguments or {})
        allowed_args = set(operation.path_params) | set(operation.query_params)
        if set(args) - allowed_args:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Operation arguments contain parameters not declared by the reviewed OpenAPI contract.")
        path = operation.path
        for name in operation.path_params:
            value = args.get(name)
            if isinstance(value, (dict, list, bool)) or value is None or len(str(value)) > 256 or not str(value):
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "A required path parameter is missing or invalid.")
            path = path.replace("{" + name + "}", urllib.parse.quote(str(value), safe=""))
        if _PATH_PARAM_RE.search(path):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Operation path contains an unresolved path parameter.")
        url = self.api_base_url + "/" + path.lstrip("/")
        query_items = []
        for name in operation.query_params:
            if name not in args:
                continue
            value = args[name]
            values = value if isinstance(value, (list, tuple)) else [value]
            if len(values) > 20:
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Query parameter contains too many values.")
            for item in values:
                if item is None or isinstance(item, (dict, tuple, list)) or len(str(item)) > 512 or any(ch in str(item) for ch in "\r\n"):
                    raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "A query parameter is invalid.")
                query_items.append((name, str(item)))
        if query_items:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query_items)
        parsed = _check_https_url(url, allowed_hosts=self.trusted_hosts)
        base = urllib.parse.urlparse(self.api_base_url)
        if _normalized_host(parsed.hostname or "") != _normalized_host(base.hostname or ""):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Resolved operation URL left the configured API host.")
        response = self._requester(url, method="GET", token=str(credentials.get("access_token") or ""), timeout=8.0)
        if response.status == 401:
            raise IntegrationError(IntegrationErrorCode.AUTHENTICATION_REQUIRED, "Provider rejected the access token; refresh or reconnect the account.")
        if response.status == 403:
            raise IntegrationError(IntegrationErrorCode.MISSING_PERMISSION, "Provider denied this operation.")
        if response.status == 429:
            raise IntegrationError(IntegrationErrorCode.RATE_LIMITED, "Provider rate limit reached; retry later.", retryable=True)
        if response.status < 200 or response.status >= 300:
            raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Provider read operation failed.")
        data = response.data
        return _sanitize_payload(data), {
            "verified": True,
            "source": "reviewed OpenAPI GET operation",
            "operation": operation.action,
            "http_status": response.status,
        }


_HEADER_TOKEN = re.compile(r"^[!#$%&'*+.^_|~0-9A-Za-z-]+$")


def _default_header_requester(
    url: str,
    *,
    api_key: str,
    header_name: str,
    trusted_hosts: Sequence[str],
    bearer_token: bool = False,
    timeout: float = 8.0,
) -> HttpResponse:
    """Send one bounded GET with an explicit header credential and no redirects."""
    _check_https_url(url, allowed_hosts=trusted_hosts)
    key = str(api_key or "")
    header = str(header_name or "")
    if not key or len(key) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in key):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "API key is empty or contains forbidden header characters.")
    if not _HEADER_TOKEN.fullmatch(header):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "API-key header name is invalid.")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Brahma-Evo-Account-Integrations",
            header: ("Bearer " + key) if bearer_token else key,
        },
        method="GET",
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=max(0.5, min(float(timeout), 15.0))) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            status = int(response.status)
            headers = dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        raw = exc.read(65536)
        status = int(exc.code)
        headers = dict(exc.headers.items()) if exc.headers else {}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise IntegrationError(IntegrationErrorCode.NETWORK_ERROR, "Provider request failed or timed out.", retryable=True) from exc
    if 300 <= status < 400:
        raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Provider redirected an API request; redirects are disabled.")
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Provider response exceeded the 1 MiB safety limit.")
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Provider returned malformed JSON.") from exc
    if not isinstance(data, (dict, list)):
        raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Provider returned an unexpected response format.")
    return HttpResponse(status, headers, data)


class APIKeyConnector:
    """Reusable header-based API-key/bearer adapter for explicitly reviewed GETs.

    The API key is passed as account credentials and remains in the configured secure
    credential store. Keys in query parameters and browser cookies are not supported.
    """

    def __init__(
        self,
        *,
        provider_id: str,
        display_name: str,
        identity_url: str,
        api_base_url: str,
        api_key_header: str,
        trusted_hosts: Sequence[str],
        operations: Sequence[OpenAPIOperation] = (),
        identity_field: str = "id",
        documentation_url: str = "",
        requester: Callable[..., HttpResponse] | None = None,
    ):
        self.provider_id = str(provider_id or "").strip().lower()
        if not _PROVIDER_RE.fullmatch(self.provider_id):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Invalid provider identifier.")
        self.api_key_header = str(api_key_header or "").strip()
        if not _HEADER_TOKEN.fullmatch(self.api_key_header):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "API-key header name is invalid.")
        hosts = tuple(sorted({_normalized_host(item) for item in trusted_hosts if str(item or "").strip()}))
        if not hosts:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Declare explicit trusted HTTPS hosts for this provider.")
        self.trusted_hosts = hosts
        self.identity_url = str(identity_url or "").strip()
        self.api_base_url = str(api_base_url or "").rstrip("/")
        _check_https_url(self.identity_url, allowed_hosts=self.trusted_hosts)
        base = _check_https_url(self.api_base_url, allowed_hosts=self.trusted_hosts)
        if base.query or base.fragment:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "API base URL may not contain query or fragment data.")
        self.identity_field = str(identity_field or "id").strip()
        if not self.identity_field or len(self.identity_field) > 128 or any(not part for part in self.identity_field.split(".")):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Identity field must be a dotted response path.")
        self._requester = requester
        operation_list = tuple(operations)
        self.operations = {item.action: item for item in operation_list}
        if len(self.operations) != len(operation_list):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Duplicate API operations are not allowed.")
        for operation in self.operations.values():
            if not operation.action.startswith(self.provider_id + "."):
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Operation actions must use the configured provider prefix.")
            if operation.auth_type not in ("api_key", "bearer") or operation.api_key_header.casefold() != self.api_key_header.casefold():
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Every operation must use the configured header-based API-key or bearer authentication.")
        auth_types = {operation.auth_type for operation in self.operations.values()}
        if len(auth_types) != 1:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "A generic header connector must use one consistent authentication type.")
        self.credential_auth_type = next(iter(auth_types))
        capabilities = [
            Capability(item.action, item.summary, RiskLevel.READ_ONLY, item.required_scopes, idempotent=True, supported=True)
            for item in self.operations.values()
        ]
        capabilities.append(Capability(
            self.provider_id + ".connection.test",
            "Verify the API key and stable account identity.",
            RiskLevel.READ_ONLY,
            (),
            idempotent=True,
            supported=True,
        ))
        self.manifest = IntegrationManifest(
            provider_id=self.provider_id,
            display_name=str(display_name or self.provider_id)[:120],
            auth_method="bearer_token" if self.credential_auth_type == "bearer" else "api_key_header",
            documentation_url=str(documentation_url or "")[:2048],
            status=ConnectionStatus.LIMITED_SUPPORT,
            status_detail="Header-based API-key/bearer authentication with identity validation and reviewed read-only API operations only.",
            authentication_documentation_url=str(documentation_url or "")[:2048],
            identity_validation_method="Configured HTTPS identity endpoint; response must contain the declared stable identity field",
            supports_token_expiration=False,
            supports_refresh=False,
            supports_revocation=False,
            pagination_strategy="Provider-specific; no automatic multi-page traversal is inferred from OpenAPI.",
            rate_limit_behavior="Surface 401/403/429 as typed errors; no automatic replay.",
            setup_requirements=("Official provider API credential", "Documented identity endpoint", "Trusted HTTPS host allowlist", "Reviewed OpenAPI JSON specification", "Credential sent only in an explicit header"),
            limitations=("Header API-key and bearer authentication only; query-string and cookie credentials are disabled.", "Only reviewed GET operations are executable.", "Provider-specific key rotation/revocation must be handled by the provider's security settings."),
            capabilities=tuple(capabilities),
        )

    def _request(self, url: str, api_key: str) -> HttpResponse:
        _check_https_url(url, allowed_hosts=self.trusted_hosts)
        if self._requester is not None:
            return self._requester(url, method="GET", api_key=api_key, api_key_header=self.api_key_header, timeout=8.0)
        return _default_header_requester(
            url, api_key=api_key, header_name=self.api_key_header,
            trusted_hosts=self.trusted_hosts, bearer_token=(self.credential_auth_type == "bearer"), timeout=8.0,
        )

    @staticmethod
    def _status_error(response: HttpResponse, *, operation: str) -> None:
        if response.status == 401:
            raise IntegrationError(IntegrationErrorCode.AUTHENTICATION_REQUIRED, "Provider rejected the API credential; reconnect this account.")
        if response.status == 403:
            raise IntegrationError(IntegrationErrorCode.MISSING_PERMISSION, "Provider denied this operation with the configured API credential.")
        if response.status == 429:
            raise IntegrationError(IntegrationErrorCode.RATE_LIMITED, "Provider rate limit reached; retry later.", retryable=True)
        if not 200 <= response.status < 300:
            raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Provider " + operation + " request failed.")

    def validate_credentials(self, credentials: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
        api_key = str(credentials.get("api_key") or "")
        if not api_key:
            raise IntegrationError(IntegrationErrorCode.AUTHENTICATION_REQUIRED, "Provider API key is missing.")
        response = self._request(self.identity_url, api_key)
        self._status_error(response, operation="identity")
        if not isinstance(response.data, Mapping):
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "Provider identity response was malformed.")
        identity = OAuth2PKCEConnector._claim(response.data, self.identity_field)
        if not identity:
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "Provider identity response did not contain a stable account identity.")
        return identity, ()

    def health_check(self, credentials: Mapping[str, Any]) -> tuple[bool, Mapping[str, Any]]:
        try:
            identity, scopes = self.validate_credentials(credentials)
            return True, {"verified": True, "identity": identity, "scopes": list(scopes), "source": "configured identity endpoint", "http_status": 200}
        except IntegrationError as exc:
            if exc.code in (IntegrationErrorCode.AUTHENTICATION_REQUIRED, IntegrationErrorCode.MISSING_PERMISSION):
                status = ConnectionStatus.MISSING_PERMISSION if exc.code == IntegrationErrorCode.MISSING_PERMISSION else ConnectionStatus.AUTHENTICATION_REQUIRED
                return False, {"error_code": exc.code.value, "status": status.value}
            raise

    def execute(self, action: str, arguments: Mapping[str, Any], credentials: Mapping[str, Any]) -> tuple[Any, Mapping[str, Any]]:
        api_key = str(credentials.get("api_key") or "")
        if not api_key:
            raise IntegrationError(IntegrationErrorCode.AUTHENTICATION_REQUIRED, "Provider API key is missing.")
        if action == self.provider_id + ".connection.test":
            identity, scopes = self.validate_credentials(credentials)
            return {"identity": identity, "scopes": list(scopes)}, {"verified": True, "source": "configured identity endpoint", "http_status": 200, "identity": identity}
        operation = self.operations.get(action)
        if operation is None:
            raise IntegrationError(IntegrationErrorCode.UNSUPPORTED_ACTION, "The provider does not declare this operation.")
        args = dict(arguments or {})
        allowed_args = set(operation.path_params) | set(operation.query_params)
        if set(args) - allowed_args:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Operation arguments contain parameters not declared by the reviewed API contract.")
        path = operation.path
        for name in operation.path_params:
            value = args.get(name)
            if value is None or isinstance(value, (dict, list, bool)) or len(str(value)) > 256 or not str(value):
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "A required path parameter is missing or invalid.")
            path = path.replace("{" + name + "}", urllib.parse.quote(str(value), safe=""))
        if _PATH_PARAM_RE.search(path):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Operation path contains an unresolved path parameter.")
        url = self.api_base_url + "/" + path.lstrip("/")
        query_items = []
        for name in operation.query_params:
            if name not in args:
                continue
            values = args[name] if isinstance(args[name], (list, tuple)) else [args[name]]
            if len(values) > 20:
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Query parameter contains too many values.")
            for value in values:
                if value is None or isinstance(value, (dict, list, tuple)) or len(str(value)) > 512 or any(char in str(value) for char in "\r\n"):
                    raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "A query parameter is invalid.")
                query_items.append((name, str(value)))
        if query_items:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query_items)
        _check_https_url(url, allowed_hosts=self.trusted_hosts)
        response = self._request(url, api_key)
        self._status_error(response, operation="read")
        return _sanitize_payload(response.data), {
            "verified": True,
            "source": "reviewed OpenAPI GET operation",
            "operation": operation.action,
            "http_status": response.status,
        }


def configure_provider_connector(
    config: Mapping[str, Any],
    openapi_spec: str | Mapping[str, Any],
    *,
    requester: Callable[..., HttpResponse] | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Build a reviewed generic adapter from explicit provider configuration.

    Required config is intentionally small but provider-specific: provider_id,
    display_name, auth_type, trusted_hosts, and either OAuth metadata/client details
    or API-key header/identity endpoint. No endpoints are guessed from names.
    The returned OpenAPI preview is still not registered; callers must review it
    and explicitly register the returned connector with IntegrationManager.
    """
    if not isinstance(config, Mapping):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Provider configuration must be an object.")
    provider_id = str(config.get("provider_id") or "").strip().lower()
    if not _PROVIDER_RE.fullmatch(provider_id):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Provider identifier must use lowercase letters, digits, underscores, or hyphens.")
    display_name = str(config.get("display_name") or provider_id).strip()[:120]
    auth_type = str(config.get("auth_type") or "").strip().lower()
    if auth_type not in ("oidc", "oauth2", "api_key", "bearer"):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Choose an explicitly supported auth_type: oidc, oauth2, api_key, or bearer.")
    raw_hosts = config.get("trusted_hosts")
    if not isinstance(raw_hosts, (list, tuple)) or not raw_hosts or any(not isinstance(host, str) for host in raw_hosts):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "trusted_hosts must explicitly list the provider's approved HTTPS hostnames.")
    trusted_hosts = tuple(sorted({_normalized_host(host) for host in raw_hosts if _normalized_host(host)}))
    if not trusted_hosts:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "No valid trusted HTTPS hostnames were configured.")
    preview = analyze_openapi_spec(
        openapi_spec,
        provider_id=provider_id,
        trusted_server_hosts=trusted_hosts,
        max_operations=int(config.get("max_operations", 250)),
    )
    candidates = preview["read_only_candidates"]
    operations = operations_from_openapi(preview)
    if not operations:
        raise IntegrationError(IntegrationErrorCode.UNSUPPORTED_ACTION, "The reviewed API specification contains no safe, authenticated GET operations for this provider.")
    if auth_type in ("api_key", "bearer"):
        expected_type = auth_type
        if any(item.auth_type != expected_type for item in operations):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "The API specification contains operations that do not use the configured header credential type. Use one authentication type per generic connector.")
        candidate_headers = {item.api_key_header.casefold(): item.api_key_header for item in operations}
        configured_header = str(config.get("api_key_header") or "").strip()
        if configured_header:
            if configured_header.casefold() not in candidate_headers:
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Configured credential header does not match the reviewed API specification.")
            api_key_header = candidate_headers[configured_header.casefold()]
        elif len(candidate_headers) == 1:
            api_key_header = next(iter(candidate_headers.values()))
        else:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Configure one explicit API credential header used by the reviewed operations.")
        identity_url = str(config.get("identity_url") or "").strip()
        if not identity_url:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Configure the documented identity/me endpoint; Brahma will not guess an identity endpoint.")
        connector = APIKeyConnector(
            provider_id=provider_id,
            display_name=display_name,
            identity_url=identity_url,
            api_base_url=preview["server_url"],
            api_key_header=api_key_header,
            trusted_hosts=trusted_hosts,
            operations=operations,
            identity_field=str(config.get("identity_field") or "id"),
            documentation_url=str(config.get("documentation_url") or "")[:2048],
            requester=requester,
        )
    else:
        expected_type = auth_type
        expected_operation_type = "oidc" if auth_type == "oidc" else "oauth2"
        if any(item.auth_type != expected_operation_type for item in operations):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "The API specification contains operations with a different auth type. Use one authentication type per generic connector.")
        client_id = str(config.get("client_id") or "").strip()
        redirect_uri = str(config.get("redirect_uri") or "").strip()
        if not client_id or not redirect_uri:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OAuth configuration requires the official client_id and exact registered redirect_uri.")
        raw_scopes = config.get("requested_scopes")
        if not isinstance(raw_scopes, (list, tuple)) or not raw_scopes or any(not isinstance(scope, str) or not scope.strip() for scope in raw_scopes):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "requested_scopes must explicitly list the minimum provider permissions.")
        requested_scopes = tuple(dict.fromkeys(str(scope).strip() for scope in raw_scopes))
        if auth_type == "oidc":
            if "openid" not in requested_scopes:
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OIDC configuration must request the openid scope.")
            issuer = str(config.get("issuer") or "").strip()
            if not issuer:
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OIDC configuration requires the exact official issuer.")
            metadata = discover_oidc_metadata(issuer, trusted_hosts=trusted_hosts, requester=requester)
            unsupported_scopes = set(requested_scopes) - set(metadata.scopes_supported)
            if metadata.scopes_supported and unsupported_scopes:
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Requested scopes include values not listed in the provider's published OIDC metadata.")
            identity_field = str(config.get("identity_field") or "sub")
            if identity_field != "sub":
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OIDC account identity must use the validated stable sub claim.")
        else:
            endpoints = config.get("oauth_endpoints")
            if not isinstance(endpoints, Mapping):
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OAuth2-only setup requires explicit documented endpoint metadata.")
            issuer = str(endpoints.get("issuer") or "")
            metadata = OAuthProviderMetadata(
                issuer=issuer,
                authorization_endpoint=str(endpoints.get("authorization_endpoint") or ""),
                token_endpoint=str(endpoints.get("token_endpoint") or ""),
                userinfo_endpoint=str(endpoints.get("userinfo_endpoint") or ""),
                jwks_uri=str(endpoints.get("jwks_uri") or ""),
                revocation_endpoint=str(endpoints.get("revocation_endpoint") or ""),
                scopes_supported=tuple(str(item) for item in endpoints.get("scopes_supported", ()) if isinstance(item, str)),
            )
            if not metadata.authorization_endpoint or not metadata.token_endpoint or not metadata.userinfo_endpoint:
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OAuth2 metadata must explicitly define authorization, token, and userinfo endpoints.")
            for endpoint in (metadata.authorization_endpoint, metadata.token_endpoint, metadata.userinfo_endpoint):
                _check_https_url(endpoint, allowed_hosts=trusted_hosts)
            if metadata.jwks_uri:
                _check_https_url(metadata.jwks_uri, allowed_hosts=trusted_hosts)
            if metadata.revocation_endpoint:
                _check_https_url(metadata.revocation_endpoint, allowed_hosts=trusted_hosts)
            identity_field = str(config.get("identity_field") or "id")
        connector = OAuth2PKCEConnector(
            provider_id=provider_id,
            display_name=display_name,
            client_id=client_id,
            client_secret=str(config.get("client_secret") or ""),
            redirect_uri=redirect_uri,
            metadata=metadata,
            api_base_url=preview["server_url"],
            operations=operations,
            requested_scopes=requested_scopes,
            identity_field=identity_field,
            trusted_hosts=trusted_hosts,
            requester=requester,
            use_oidc=(expected_type == "oidc"),
        )
    return connector, preview
