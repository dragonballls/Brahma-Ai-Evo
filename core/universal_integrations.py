"""Reusable OAuth2/OIDC and capability-driven OpenAPI integrations.

Imported API descriptions are untrusted data. Preview never executes a remote
operation; users explicitly approve the reviewed capability set before registration.
Network requests are bounded, HTTPS-only and redirect-free.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import math
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
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
# Discovery is bounded by imported-document size/structure instead of an arbitrary small action count.
_MAX_OPERATIONS = _MAX_SPEC_NODES
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
    id_token_signing_alg_values_supported: tuple[str, ...] = ("RS256",)


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
    required_query_params: tuple[str, ...] = ()
    path_param_schemas: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    query_param_schemas: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    body_schema: Mapping[str, Any] | None = None
    body_required: bool = False
    body_content_type: str = "application/json"
    output_schema: Mapping[str, Any] = field(default_factory=dict)
    risk: RiskLevel = RiskLevel.READ_ONLY
    idempotent: bool = True
    pagination: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        method = str(self.method).upper()
        if not _OPERATION_RE.fullmatch(str(self.action or "")):
            raise ValueError("Operation action must be a stable identifier.")
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            raise ValueError("Operation uses an unsupported HTTP method.")
        if method == "GET" and self.risk != RiskLevel.READ_ONLY:
            raise ValueError("GET operations must be classified read-only.")
        decoded_path = urllib.parse.unquote(str(self.path or ""))
        if not str(self.path).startswith("/") or "://" in self.path or "#" in self.path or "?" in self.path or "\\" in self.path or ".." in decoded_path.split("/"):
            raise ValueError("Operation path must be a safe relative API path.")
        placeholders = set(_PATH_PARAM_RE.findall(self.path))
        if placeholders != set(self.path_params):
            raise ValueError("Declared path parameters must exactly match the path template.")
        if len(set(self.query_params)) != len(self.query_params) or len(set(self.path_params)) != len(self.path_params):
            raise ValueError("Operation parameter names must be unique.")
        if not set(self.required_query_params).issubset(set(self.query_params)):
            raise ValueError("Required query parameters must be declared query parameters.")
        if set(self.path_param_schemas) - set(self.path_params) or set(self.query_param_schemas) - set(self.query_params):
            raise ValueError("Parameter schemas may only describe declared parameters.")
        if self.auth_type not in ("oauth2", "oidc", "api_key", "bearer"):
            raise ValueError("Operation authentication type is unsupported.")
        if self.auth_type in ("api_key", "bearer"):
            if not re.fullmatch(r"[!#$%&'*+.^_|~0-9A-Za-z-]+", self.api_key_header):
                raise ValueError("API credential header name is invalid.")
        elif self.api_key_header:
            raise ValueError("OAuth operations may not declare a static credential header.")
        if self.body_schema is not None and not isinstance(self.body_schema, Mapping):
            raise ValueError("Request body schema must be an object.")
        if self.body_required and self.body_schema is None:
            raise ValueError("Required request bodies must have a validated schema.")
        if self.body_content_type != "application/json" and not self.body_content_type.endswith("+json"):
            raise ValueError("Only explicitly described JSON request bodies are supported.")


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
    json_body: Any = None,
    content_type: str = "application/json",
    token: str | None = None,
    timeout: float = 8.0,
) -> HttpResponse:
    """Bounded HTTPS JSON/form request that refuses redirects and oversized payloads."""
    _check_https_url(url)
    method = str(method or "GET").upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "HTTP method is not supported by the generic connector.")
    if form is not None and json_body is not None:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "A request cannot use both form data and a JSON body.")
    body = None
    headers = {"Accept": "application/json", "User-Agent": "Brahma-Evo-Generic-Integration"}
    if form is not None:
        body = urllib.parse.urlencode(dict(form)).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        if content_type != "application/json" and not str(content_type).endswith("+json"):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Only JSON request content types are supported.")
        try:
            body = json.dumps(json_body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Request body is not valid JSON data.") from exc
        if len(body) > 512 * 1024:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Request body exceeds the 512 KiB safety limit.")
        headers["Content-Type"] = str(content_type)
    if token:
        if len(str(token)) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in str(token)):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Provider token contains invalid header characters.")
        headers["Authorization"] = "Bearer " + str(token)
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect())
    response_headers = {}
    try:
        with opener.open(request, timeout=max(0.5, min(float(timeout), 15.0))) as response:
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
        raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Provider redirected an API request; redirects are not followed.")
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
    advertised_algorithms = data.get("id_token_signing_alg_values_supported")
    if not isinstance(advertised_algorithms, list) or not advertised_algorithms:
        raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "OIDC discovery must declare supported ID-token signing algorithms.")
    supported_algorithms = tuple(dict.fromkeys(
        algorithm for algorithm in advertised_algorithms
        if isinstance(algorithm, str) and algorithm in ("RS256", "ES256")
    ))
    if not supported_algorithms:
        raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "OIDC provider advertises no supported safe signing algorithm; only RS256 and ES256 are implemented.")
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
        id_token_signing_alg_values_supported=supported_algorithms,
    )


def _load_openapi_spec(spec: str | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(spec, str):
        if len(spec.encode("utf-8")) > _MAX_SPEC_BYTES:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI description exceeds the 2 MiB onboarding limit.")
        def _unique_json_object(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate JSON mapping key " + repr(key))
                value[key] = item
            return value

        try:
            loaded = json.loads(spec, object_pairs_hook=_unique_json_object)
        except json.JSONDecodeError:
            try:
                import yaml
            except ImportError as exc:
                raise IntegrationError(IntegrationErrorCode.UNSUPPORTED_ACTION, "OpenAPI YAML import requires the packaged PyYAML dependency.") from exc

            class _UniqueKeySafeLoader(yaml.SafeLoader):
                """Safe YAML loader rejecting ambiguous or non-string mapping keys."""
                def construct_mapping(self, node, deep=False):
                    if not isinstance(node, yaml.nodes.MappingNode):
                        return super().construct_mapping(node, deep=deep)
                    self.flatten_mapping(node)
                    mapping = {}
                    for key_node, value_node in node.value:
                        key = self.construct_object(key_node, deep=deep)
                        if not isinstance(key, str):
                            raise yaml.constructor.ConstructorError(
                                "while constructing an OpenAPI mapping", node.start_mark,
                                "mapping keys must be strings", key_node.start_mark,
                            )
                        if key in mapping:
                            raise yaml.constructor.ConstructorError(
                                "while constructing an OpenAPI mapping", node.start_mark,
                                "duplicate mapping key " + repr(key), key_node.start_mark,
                            )
                        mapping[key] = self.construct_object(value_node, deep=deep)
                    return mapping

            try:
                loaded = yaml.load(spec, Loader=_UniqueKeySafeLoader)
            except yaml.YAMLError as exc:
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI YAML is invalid or uses unsafe/ambiguous YAML features.") from exc
        except ValueError as exc:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI JSON contains a duplicate mapping key.") from exc
    elif isinstance(spec, Mapping):
        loaded = dict(spec)
    else:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI description must be a JSON/YAML object or text.")
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


def fetch_schema_document_url(
    url: str,
    *,
    trusted_hosts: Sequence[str],
    protocol: str = "openapi",
    timeout: float = 8.0,
) -> str:
    """Fetch an approved HTTPS schema with exact-host allowlisting, no redirects and byte limits."""
    parsed = _check_https_url(url, allowed_hosts=trusted_hosts)
    if parsed.query:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Schema URLs may not contain query parameters; use a stable documented URL.")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.oai.openapi+json, application/json, application/yaml, text/yaml, text/plain;q=0.8",
            "User-Agent": "Brahma-Evo-Schema-Importer",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=max(0.5, min(float(timeout), 15.0))) as response:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > _MAX_SPEC_BYTES:
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Remote schema exceeds the 2 MiB import limit.")
            raw = response.read(_MAX_SPEC_BYTES + 1)
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        if 300 <= int(exc.code) < 400:
            raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Schema redirects are not followed; approve and enter the final HTTPS URL.") from exc
        raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "The approved schema URL returned HTTP " + str(int(exc.code)) + ".") from exc
    except IntegrationError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise IntegrationError(IntegrationErrorCode.NETWORK_ERROR, "The approved schema URL could not be fetched or timed out.") from exc
    if not 200 <= status < 300:
        raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "The approved schema URL did not return a successful response.")
    if len(raw) > _MAX_SPEC_BYTES:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Remote schema exceeds the 2 MiB import limit.")
    try:
        document = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Remote schema must use UTF-8 encoding.") from exc
    if not document.strip():
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Remote schema is empty.")
    selected_protocol = str(protocol or "openapi").strip().lower()
    if selected_protocol == "graphql":
        _load_graphql_schema(document)
    elif selected_protocol in ("openapi", "rest", "rest_openapi"):
        _load_openapi_spec(document)
    else:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Remote schema protocol must be OpenAPI/REST or GraphQL.")
    return document


def _resolve_openapi_reference(document: Mapping[str, Any], value: Any, seen: frozenset[str] = frozenset()) -> Any:
    """Resolve internal JSON pointers only; never fetch remote schema references."""
    if not isinstance(value, Mapping) or "$ref" not in value:
        return value
    reference = value.get("$ref")
    if not isinstance(reference, str) or not reference.startswith("#/") or reference in seen:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI contains an external or cyclic reference.")
    target: Any = document
    try:
        for part in reference[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
    except (KeyError, TypeError) as exc:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI contains a broken internal reference.") from exc
    if not isinstance(target, Mapping):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI reference must resolve to an object.")
    merged = dict(target)
    merged.update({key: item for key, item in value.items() if key != "$ref"})
    return _resolve_openapi_reference(document, merged, seen | {reference})


def _normalize_operation_schema(
    schema: Any, document: Mapping[str, Any], *, depth: int = 0,
    seen_refs: frozenset[str] = frozenset(), strict_objects: bool = True,
) -> dict[str, Any]:
    """Normalize a bounded JSON-schema subset; fail closed for unknown constraints."""
    if depth > 16 or not isinstance(schema, Mapping):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI operation schema exceeds the supported validation subset.")
    reference = schema.get("$ref")
    if reference is not None:
        if not isinstance(reference, str) or not reference.startswith("#/") or reference in seen_refs:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI schema references must be internal and acyclic.")
        resolved = _resolve_openapi_reference(document, schema)
        return _normalize_operation_schema(resolved, document, depth=depth + 1, seen_refs=seen_refs | {reference}, strict_objects=strict_objects)
    if any(key in schema for key in ("allOf", "oneOf", "anyOf", "not", "patternProperties", "unevaluatedProperties")):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI schema composition or dynamic properties are outside the generic safe subset.")
    kind = schema.get("type")
    nullable = schema.get("nullable", False)
    if not isinstance(nullable, bool):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI nullable constraint must be a boolean.")
    if isinstance(kind, list):
        if len(kind) == 2 and "null" in kind and all(item in ("null", "object", "array", "string", "integer", "number", "boolean") for item in kind):
            kind = next(item for item in kind if item != "null")
            nullable = True
        else:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI type unions are outside the supported JSON-schema subset.")
    if kind not in ("object", "array", "string", "integer", "number", "boolean"):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI operation schema must declare a supported JSON type.")
    result: dict[str, Any] = {"type": kind}
    if nullable:
        result["nullable"] = True
    if "enum" in schema:
        values = schema["enum"]
        if not isinstance(values, list) or not values or len(values) > _MAX_SPEC_NODES:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI enum is malformed or exceeds the imported-document safety limit.")
        result["enum"] = values
    if kind == "object":
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping) or len(properties) > _MAX_SPEC_NODES:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI object properties are malformed or exceed the imported-document safety limit.")
        if strict_objects and schema.get("additionalProperties", False) not in (False, None):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Dynamic additional properties are not supported for write arguments.")
        normalized = {}
        for name, item in properties.items():
            if not isinstance(name, str) or not name or len(name) > 128:
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI property name is invalid.")
            normalized[name] = _normalize_operation_schema(item, document, depth=depth + 1, seen_refs=seen_refs, strict_objects=strict_objects)
        required = schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(name, str) for name in required) or not set(required).issubset(normalized):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI required object properties are invalid.")
        result.update({"properties": normalized, "required": list(dict.fromkeys(required)), "additionalProperties": False if strict_objects else bool(schema.get("additionalProperties", True))})
    elif kind == "array":
        if "items" not in schema:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI array schema must declare item types.")
        result["items"] = _normalize_operation_schema(schema["items"], document, depth=depth + 1, seen_refs=seen_refs, strict_objects=strict_objects)
        try:
            result["maxItems"] = max(0, min(int(schema.get("maxItems", 1000)), 10000))
            result["minItems"] = max(0, min(int(schema.get("minItems", 0)), 10000))
        except (TypeError, ValueError) as exc:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI array size constraints are invalid.") from exc
    elif kind == "string":
        try:
            result["maxLength"] = max(0, min(int(schema.get("maxLength", 16384)), 65536))
            result["minLength"] = max(0, min(int(schema.get("minLength", 0)), 65536))
        except (TypeError, ValueError) as exc:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI string length constraints are invalid.") from exc
    elif kind in ("integer", "number"):
        for key in ("minimum", "maximum"):
            if schema.get(key) is not None:
                try:
                    result[key] = float(schema[key])
                except (TypeError, ValueError) as exc:
                    raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI numeric constraint is invalid.") from exc
                if not math.isfinite(result[key]):
                    raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI numeric constraints must be finite.")
    return result


def _validate_operation_value(value: Any, schema: Mapping[str, Any], *, depth: int = 0) -> None:
    """Validate an operation argument before any remote request is sent."""
    if depth > 20:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Operation arguments exceed the nesting limit.")
    if value is None and schema.get("nullable") is True:
        return
    kind = schema.get("type")
    valid = (
        isinstance(value, Mapping) if kind == "object" else
        isinstance(value, list) if kind == "array" else
        isinstance(value, str) if kind == "string" else
        isinstance(value, int) and not isinstance(value, bool) if kind == "integer" else
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) if kind == "number" else
        isinstance(value, bool) if kind == "boolean" else False
    )
    if not valid:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "An operation argument does not match the declared JSON schema.")
    if "enum" in schema and value not in schema["enum"]:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "An operation argument is not an allowed declared value.")
    if kind == "object":
        properties = schema.get("properties", {})
        extra_fields = set(value) - set(properties)
        if (extra_fields and schema.get("additionalProperties", False) is not True) or set(schema.get("required", [])) - set(value):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Object arguments contain undeclared fields or omit required fields.")
        for key, item in value.items():
            if key in properties:
                _validate_operation_value(item, properties[key], depth=depth + 1)
    elif kind == "array":
        if len(value) > int(schema.get("maxItems", 1000)) or len(value) < int(schema.get("minItems", 0)):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Array argument is outside its declared size limits.")
        for item in value:
            _validate_operation_value(item, schema["items"], depth=depth + 1)
    elif kind == "string":
        if len(value) > int(schema.get("maxLength", 16384)) or len(value) < int(schema.get("minLength", 0)):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "String argument is outside its declared length limits.")
    elif kind in ("integer", "number"):
        if ("minimum" in schema and value < schema["minimum"]) or ("maximum" in schema and value > schema["maximum"]):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Numeric argument is outside its declared range.")



_POINTER_MISSING = object()


def _parse_json_pointer(pointer: Any, *, label: str) -> tuple[str, ...]:
    value = str(pointer or "")
    if not value.startswith("#/") or len(value) > 1024:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, label + " must be a bounded RFC 6901 JSON pointer beginning with '#/'.")
    encoded = value[2:].split("/")
    if not encoded or len(encoded) > 32:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, label + " has too many pointer segments.")
    segments = []
    for segment in encoded:
        decoded, i = "", 0
        while i < len(segment):
            if segment[i] != "~":
                decoded += segment[i]
                i += 1
            else:
                if i + 1 >= len(segment) or segment[i + 1] not in "01":
                    raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, label + " contains an invalid JSON-pointer escape.")
                decoded += "/" if segment[i + 1] == "1" else "~"
                i += 2
        segments.append(decoded)
    return tuple(segments)


def _read_json_pointer(value: Any, pointer: str) -> Any:
    current = value
    for segment in _parse_json_pointer(pointer, label="Pagination pointer"):
        if isinstance(current, Mapping):
            if segment not in current:
                return _POINTER_MISSING
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit():
            index = int(segment)
            if index >= len(current):
                return _POINTER_MISSING
            current = current[index]
        else:
            return _POINTER_MISSING
    return current


def _normalize_cursor_pagination(
    raw: Any, *, method: str, query_params: Sequence[str],
    required_query_params: Sequence[str],
    query_param_schemas: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "x-brahma-pagination must be an object.")
    if str(method).upper() != "GET" or str(raw.get("style") or "") != "cursor":
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Only explicit cursor pagination on read-only GET operations is supported.")
    cursor_parameter = str(raw.get("cursor_parameter") or raw.get("parameter") or "").strip()
    if not cursor_parameter or cursor_parameter not in query_params or cursor_parameter in required_query_params:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "cursor_parameter must name an optional query parameter declared by the GET operation.")
    if query_param_schemas.get(cursor_parameter, {}).get("type") not in ("string", "integer", "number"):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Cursor parameter must be a declared string or numeric query parameter.")
    page_size_parameter = str(raw.get("page_size_parameter") or "").strip()
    if page_size_parameter:
        if page_size_parameter not in query_params or page_size_parameter in required_query_params or page_size_parameter == cursor_parameter:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "page_size_parameter must name a different optional query parameter declared by the GET operation.")
        page_schema = query_param_schemas.get(page_size_parameter, {})
        if page_schema.get("type") not in ("string", "integer", "number"):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Page-size parameter must be a declared string or numeric query parameter.")
    try:
        page_size = int(raw.get("page_size", 100))
        max_pages = int(raw.get("max_pages", 20))
        max_items = int(raw.get("max_items", 2000))
    except (TypeError, ValueError) as exc:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Pagination page_size, max_pages and max_items must be integers.") from exc
    if any(isinstance(raw.get(name), bool) for name in ("page_size", "max_pages", "max_items") if name in raw):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Pagination numeric limits must not be booleans.")
    if not 1 <= page_size <= 1000:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Pagination page_size must be between 1 and 1000.")
    if not 1 <= max_pages <= 50:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Pagination max_pages must be between 1 and 50.")
    if not 1 <= max_items <= 5000:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Pagination max_items must be between 1 and 5000.")
    if page_size_parameter:
        page_schema = query_param_schemas.get(page_size_parameter, {})
        if ("minimum" in page_schema and page_size < page_schema["minimum"]) or ("maximum" in page_schema and page_size > page_schema["maximum"]):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Configured page_size falls outside the declared parameter schema.")
    items_pointer = str(raw.get("items_pointer") or "")
    next_cursor_pointer = str(raw.get("next_cursor_pointer") or "")
    has_more_pointer = str(raw.get("has_more_pointer") or "")
    _parse_json_pointer(items_pointer, label="items_pointer")
    _parse_json_pointer(next_cursor_pointer, label="next_cursor_pointer")
    if has_more_pointer:
        _parse_json_pointer(has_more_pointer, label="has_more_pointer")
    return {
        "style": "cursor", "cursor_parameter": cursor_parameter,
        "page_size_parameter": page_size_parameter, "page_size": page_size,
        "max_pages": max_pages, "max_items": max_items,
        "items_pointer": items_pointer, "next_cursor_pointer": next_cursor_pointer,
        "has_more_pointer": has_more_pointer,
    }


def _operation_input_schema(operation: OpenAPIOperation) -> dict[str, Any]:
    pagination = dict(operation.pagination or {})
    hidden = {str(pagination.get("cursor_parameter") or ""), str(pagination.get("page_size_parameter") or "")} - {""}
    properties = {
        **{name: dict(schema) for name, schema in operation.path_param_schemas.items()},
        **{name: dict(schema) for name, schema in operation.query_param_schemas.items() if name not in hidden},
        **({"body": dict(operation.body_schema)} if operation.body_schema is not None else {}),
    }
    required = list(operation.path_params) + [name for name in operation.required_query_params if name not in hidden]
    if operation.body_required:
        required.append("body")
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def _validate_openapi_page(response: HttpResponse, operation: OpenAPIOperation) -> Any:
    if response.status == 401:
        raise IntegrationError(IntegrationErrorCode.AUTHENTICATION_REQUIRED, "Provider rejected the credential; refresh or reconnect the account.")
    if response.status == 403:
        raise IntegrationError(IntegrationErrorCode.MISSING_PERMISSION, "Provider denied this operation.")
    if response.status == 429:
        raise IntegrationError(IntegrationErrorCode.RATE_LIMITED, "Provider rate limit reached; retry later.", retryable=True)
    if response.status < 200 or response.status >= 300:
        raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Provider pagination request failed.")
    if operation.output_schema and response.status != 204:
        try:
            _validate_operation_value(response.data, operation.output_schema)
        except IntegrationError as exc:
            raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Provider page did not match its declared response schema.") from exc
    return response.data


def _execute_cursor_pagination(
    base_url: str, operation: OpenAPIOperation, request_page: Callable[[str], HttpResponse],
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    pagination = dict(operation.pagination or {})
    if not pagination:
        response = request_page(base_url)
        return _sanitize_payload(_validate_openapi_page(response, operation)), {
            "verified": operation.method == "GET", "http_status": response.status, "pages_fetched": 1,
        }
    cursor_parameter = str(pagination["cursor_parameter"])
    page_size_parameter = str(pagination.get("page_size_parameter") or "")
    parts = urllib.parse.urlsplit(base_url)
    internal_params = {cursor_parameter, page_size_parameter} - {""}
    static_query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k not in internal_params]
    items, seen_cursors = [], set()
    cursor = None
    pages_fetched, has_more, truncated, last_status = 0, True, False, 0
    for page_number in range(int(pagination["max_pages"])):
        query = list(static_query)
        if page_size_parameter:
            query.append((page_size_parameter, str(pagination["page_size"])))
        if cursor is not None:
            query.append((cursor_parameter, cursor))
        page_url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), ""))
        parsed_page = _check_https_url(page_url)
        if (_normalized_host(parsed_page.hostname or "") != _normalized_host(parts.hostname or "")
                or parsed_page.port != parts.port or parsed_page.scheme != parts.scheme):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Pagination URL changed the configured API origin.")
        response = request_page(page_url)
        pages_fetched += 1
        last_status = response.status
        payload = _validate_openapi_page(response, operation)
        page_items = _read_json_pointer(payload, str(pagination["items_pointer"]))
        if not isinstance(page_items, list):
            raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Pagination items_pointer did not resolve to a list.")
        next_cursor_value = _read_json_pointer(payload, str(pagination["next_cursor_pointer"]))
        if pagination.get("has_more_pointer"):
            more_value = _read_json_pointer(payload, str(pagination["has_more_pointer"]))
            if not isinstance(more_value, bool):
                raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Pagination has_more_pointer did not resolve to a boolean.")
            has_more = more_value
        else:
            has_more = next_cursor_value is not _POINTER_MISSING and next_cursor_value not in (None, "")
        remaining = int(pagination["max_items"]) - len(items)
        items.extend(page_items[:max(0, remaining)])
        if len(page_items) > remaining:
            truncated, has_more = True, True
            break
        if not has_more:
            break
        if next_cursor_value is _POINTER_MISSING or next_cursor_value is None or next_cursor_value == "":
            raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Provider reported more pages without a next cursor.")
        if isinstance(next_cursor_value, bool) or isinstance(next_cursor_value, (Mapping, list, tuple)):
            raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Provider next cursor was not a scalar.")
        next_cursor = str(next_cursor_value)
        if len(next_cursor) > 512 or any(ch in next_cursor for ch in "\r\n"):
            raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Provider next cursor exceeded safe encoding limits.")
        if next_cursor in seen_cursors:
            raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Provider repeated a cursor; pagination stopped to prevent a loop.")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
        if len(items) >= int(pagination["max_items"]) or page_number + 1 >= int(pagination["max_pages"]):
            truncated, has_more = True, True
            break
    result = {
        "items": _sanitize_payload(items), "pages_fetched": pages_fetched,
        "has_more": bool(has_more), "truncated": bool(truncated),
    }
    return result, {
        "verified": True, "source": "reviewed OpenAPI cursor pagination",
        "operation": operation.action, "method": "GET", "http_status": last_status,
        "pages_fetched": pages_fetched, "items_returned": len(items),
        "has_more": bool(has_more), "truncated": bool(truncated), "pagination_style": "cursor",
    }


def _operation_risk(method: str, operation: Mapping[str, Any], path: str) -> RiskLevel:
    """Use conservative method/path semantics; summaries cannot lower DELETE risk."""
    method = str(method).upper()
    raw_tags = operation.get("tags", [])
    tags = raw_tags if isinstance(raw_tags, (list, tuple)) else []
    text = (path + " " + str(operation.get("summary") or "") + " " +
            str(operation.get("description") or "") + " " +
            " ".join(tag for tag in tags if isinstance(tag, str))).casefold()
    if method == "GET":
        return RiskLevel.READ_ONLY
    if method == "DELETE" or any(word in text for word in ("permission", "credential", "access key", "api key", "token", "administrator", "admin", "security", "role", "revoke", "delete")):
        return RiskLevel.DESTRUCTIVE
    if any(word in text for word in ("payment", "checkout", "billing", "transfer", "invoice", "charge", "refund", "purchase", "order")):
        return RiskLevel.FINANCIAL
    if any(word in text for word in ("publish", "social post")):
        return RiskLevel.PUBLICATION
    if any(word in text for word in ("message", "email", "sms", "notify", "invite", "comment")):
        return RiskLevel.EXTERNAL_COMMUNICATION
    return RiskLevel.REVERSIBLE


def analyze_openapi_spec(
    spec: str | Mapping[str, Any],
    *,
    provider_id: str = "custom",
    trusted_server_hosts: Sequence[str] = (),
    max_operations: int = _MAX_OPERATIONS,
) -> dict[str, Any]:
    """Preview explicitly authenticated OpenAPI operations without executing them."""
    provider = str(provider_id or "").strip().lower()
    if not _PROVIDER_RE.fullmatch(provider):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Provider identifier must use lowercase letters, digits, underscores, or hyphens.")
    document = _load_openapi_spec(spec)
    servers = document.get("servers")
    if not isinstance(servers, list) or len(servers) != 1 or not isinstance(servers[0], Mapping):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Provide exactly one explicit API server.")
    server_url = str(servers[0].get("url") or "")
    if "{" in server_url or "}" in server_url:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Templated server URLs require a provider-specific adapter.")
    _check_https_url(server_url, allowed_hosts=trusted_server_hosts)
    paths = document.get("paths")
    if not isinstance(paths, Mapping) or len(paths) > _MAX_OPERATIONS:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI document has no valid paths or exceeds the path limit.")
    components = document.get("components", {})
    schemes = components.get("securitySchemes", {}) if isinstance(components, Mapping) else {}
    root_security = document.get("security", [])
    read_candidates: list[dict[str, Any]] = []
    mutation_candidates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    warnings: list[str] = []
    count = 0
    for path, path_item in paths.items():
        if not isinstance(path, str) or not path.startswith("/") or "://" in path or ".." in urllib.parse.unquote(path).split("/") or not isinstance(path_item, Mapping):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI contains an unsafe or malformed path.")
        shared_parameters = path_item.get("parameters", [])
        if not isinstance(shared_parameters, list):
            warnings.append("Skipped malformed shared parameters on " + path + ".")
            shared_parameters = []
        for method in ("get", "post", "put", "patch", "delete"):
            operation = path_item.get(method)
            if not isinstance(operation, Mapping):
                continue
            count += 1
            if count > _MAX_OPERATIONS:
                warnings.append("Operation discovery stopped at the structural operation limit.")
                break
            operation_id = str(operation.get("operationId") or "")
            summary = str(operation.get("summary") or operation.get("description") or operation_id or (method.upper() + " " + path))[:400]
            if not operation_id or not _OPERATION_RE.fullmatch(operation_id) or operation_id in seen_ids:
                warnings.append("Skipped " + method.upper() + " " + path + " because a duplicate operationId is present or the operationId is missing/invalid.")
                continue
            if operation.get("deprecated") is True:
                warnings.append("Skipped deprecated operation " + operation_id + ".")
                continue
            declared_security = operation.get("security", root_security)
            if not isinstance(declared_security, list) or not declared_security:
                warnings.append("Skipped operation " + operation_id + " because explicit authentication is not declared.")
                continue
            if len(declared_security) != 1 or not isinstance(declared_security[0], Mapping) or len(declared_security[0]) != 1:
                warnings.append("Skipped operation " + operation_id + " because its authentication alternatives require manual adapter support.")
                continue
            scheme_name, scopes = next(iter(declared_security[0].items()))
            scheme = schemes.get(scheme_name) if isinstance(schemes, Mapping) else None
            if not isinstance(scheme, Mapping):
                warnings.append("Skipped operation " + operation_id + " because its security scheme is missing.")
                continue
            scheme_type = str(scheme.get("type") or "")
            auth_type, api_key_header = "", ""
            if scheme_type in ("oauth2", "openIdConnect"):
                auth_type = "oidc" if scheme_type == "openIdConnect" else "oauth2"
            elif scheme_type == "apiKey" and scheme.get("in") == "header":
                api_key_header = str(scheme.get("name") or "")
                if not re.fullmatch(r"[!#$%&'*+.^_|~0-9A-Za-z-]+", api_key_header) or scopes:
                    warnings.append("Skipped operation " + operation_id + " because its API-key header scheme is invalid.")
                    continue
                auth_type = "api_key"
            elif scheme_type == "apiKey":
                warnings.append("Skipped operation " + operation_id + " because query/cookie API keys are not supported; configure the key in a documented header or use an explicit adapter.")
                continue
            elif scheme_type == "http" and str(scheme.get("scheme") or "").casefold() == "bearer":
                api_key_header = "Authorization"
                if scopes:
                    warnings.append("Skipped operation " + operation_id + " because HTTP bearer auth cannot declare OAuth scopes.")
                    continue
                auth_type = "bearer"
            else:
                warnings.append("Skipped operation " + operation_id + " because this generic adapter does not implement its authentication scheme.")
                continue
            if not isinstance(scopes, list) or any(not isinstance(scope, str) or not scope.strip() for scope in scopes):
                warnings.append("Skipped operation " + operation_id + " because its scopes are malformed.")
                continue

            parameters = list(shared_parameters)
            own_parameters = operation.get("parameters", [])
            if not isinstance(own_parameters, list):
                warnings.append("Skipped operation " + operation_id + " because its parameters are malformed.")
                continue
            parameters.extend(own_parameters)
            path_params = tuple(sorted(set(_PATH_PARAM_RE.findall(path))))
            query_params, required_query_params = [], []
            path_schemas, query_schemas = {}, {}
            declared_path_params: set[str] = set()
            valid_parameters = True
            for raw_parameter in parameters:
                parameter = _resolve_openapi_reference(document, raw_parameter) if isinstance(raw_parameter, Mapping) else raw_parameter
                if not isinstance(parameter, Mapping):
                    valid_parameters = False
                    break
                location = parameter.get("in")
                name = str(parameter.get("name") or "")
                if location not in ("path", "query") or not name or len(name) > 128 or not isinstance(parameter.get("schema"), Mapping):
                    valid_parameters = False
                    break
                try:
                    normalized = _normalize_operation_schema(parameter["schema"], document, strict_objects=True)
                except IntegrationError:
                    valid_parameters = False
                    break
                if location == "path":
                    if name not in path_params or parameter.get("required") is not True or name in path_schemas:
                        valid_parameters = False
                        break
                    declared_path_params.add(name)
                    path_schemas[name] = normalized
                else:
                    if name in query_schemas:
                        valid_parameters = False
                        break
                    query_params.append(name)
                    query_schemas[name] = normalized
                    if parameter.get("required") is True:
                        required_query_params.append(name)
            if not valid_parameters or declared_path_params != set(path_params) or set(path_params) & set(query_params):
                warnings.append("Skipped operation " + operation_id + " because path/query parameter schemas cannot be safely validated.")
                continue

            body_schema, body_required, body_content_type = None, False, "application/json"
            body_error = ""
            request_body = operation.get("requestBody")
            if request_body is not None:
                try:
                    request_body = _resolve_openapi_reference(document, request_body)
                    content = request_body.get("content") if isinstance(request_body, Mapping) else None
                    if not isinstance(content, Mapping):
                        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "request body content is missing")
                    json_media = next(((name, item) for name, item in content.items()
                                       if isinstance(name, str) and
                                       (name.casefold() == "application/json" or
                                        (name.casefold().startswith("application/") and name.casefold().endswith("+json")))), None)
                    if json_media is None or not isinstance(json_media[1], Mapping) or not isinstance(json_media[1].get("schema"), Mapping):
                        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "only JSON request bodies with a declared schema are supported")
                    body_content_type, media = json_media
                    body_schema = _normalize_operation_schema(media["schema"], document, strict_objects=True)
                    body_required = request_body.get("required") is True
                except IntegrationError as exc:
                    body_error = str(exc)
            risk = _operation_risk(method.upper(), operation, path)
            if body_error:
                candidate = {
                    "action": provider + "." + operation_id, "operation_id": operation_id,
                    "summary": summary, "method": method.upper(), "path": path,
                    "risk": risk.value, "supported": False,
                    "requires_manual_approval": True, "unsupported_reason": body_error,
                }
                if method == "get":
                    warnings.append("Skipped GET operation " + operation_id + " because " + body_error + ".")
                else:
                    mutation_candidates.append(candidate)
                seen_ids.add(operation_id)
                continue

            output_schema: Mapping[str, Any] = {}
            responses = operation.get("responses", {})
            if isinstance(responses, Mapping):
                for status_code, response_spec in responses.items():
                    if not str(status_code).startswith("2") or not isinstance(response_spec, Mapping):
                        continue
                    try:
                        response_spec = _resolve_openapi_reference(document, response_spec)
                        response_content = response_spec.get("content", {})
                        if isinstance(response_content, Mapping):
                            media = next((item for name, item in response_content.items()
                                          if isinstance(name, str) and
                                          (name.casefold() == "application/json" or
                                           (name.casefold().startswith("application/") and name.casefold().endswith("+json")))
                                          and isinstance(item, Mapping) and isinstance(item.get("schema"), Mapping)), None)
                            if media:
                                output_schema = _normalize_operation_schema(media["schema"], document, strict_objects=False)
                                break
                    except IntegrationError:
                        warnings.append("Response schema for " + operation_id + " is outside the supported subset; bounded JSON parsing remains enabled.")
            pagination: dict[str, Any] = {}
            if "x-brahma-pagination" in operation:
                try:
                    pagination = _normalize_cursor_pagination(
                        operation.get("x-brahma-pagination"), method=method.upper(),
                        query_params=tuple(query_params), required_query_params=tuple(required_query_params),
                        query_param_schemas=query_schemas,
                    )
                except IntegrationError as exc:
                    warnings.append("Skipped operation " + operation_id + " because its x-brahma-pagination contract is invalid: " + str(exc))
                    seen_ids.add(operation_id)
                    continue
            candidate = {
                "action": provider + "." + operation_id, "operation_id": operation_id,
                "summary": summary, "method": method.upper(), "path": path,
                "required_scopes": sorted(set(scopes)), "path_params": list(path_params),
                "query_params": sorted(set(query_params)), "required_query_params": sorted(set(required_query_params)),
                "path_param_schemas": path_schemas, "query_param_schemas": query_schemas,
                "body_schema": body_schema, "body_required": body_required, "body_content_type": body_content_type,
                "output_schema": dict(output_schema), "pagination": pagination, "auth_scheme": str(scheme_name),
                "auth_type": auth_type, "api_key_header": api_key_header, "risk": risk.value,
                "idempotent": method in ("get", "put", "delete"), "supported": True,
                "requires_manual_approval": True,
            }
            (read_candidates if method == "get" else mutation_candidates).append(candidate)
            seen_ids.add(operation_id)

    try:
        limit = max(1, min(int(max_operations), _MAX_OPERATIONS))
    except (TypeError, ValueError) as exc:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "max_operations must be a positive integer.") from exc
    if len(read_candidates) > limit:
        read_candidates = read_candidates[:limit]
        warnings.append("Read operation candidates were truncated to the configured operation limit.")
    if len(mutation_candidates) > limit:
        mutation_candidates = mutation_candidates[:limit]
        warnings.append("State-changing candidates were truncated to the configured operation limit.")
    review_candidates = read_candidates + [item for item in mutation_candidates if item.get("supported") is True]
    return {
        "provider_id": provider, "openapi": document["openapi"], "server_url": server_url,
        "read_only_candidates": read_candidates, "mutation_candidates": mutation_candidates,
        "review_candidates": review_candidates, "warnings": warnings, "registered": False,
        "note": "Inert preview only. Review and explicitly approve the listed capabilities before registering them; state-changing actions require trusted-UI confirmation at execution time.",
    }


def operations_from_openapi(preview: Mapping[str, Any]) -> tuple[OpenAPIOperation, ...]:
    """Build operation descriptors from supported candidates in an inert OpenAPI preview."""
    if preview.get("registered") is not False:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Use a validated, unregistered OpenAPI preview.")
    candidates = preview.get("review_candidates")
    if not isinstance(candidates, list):
        candidates = list(preview.get("read_only_candidates") or []) + [
            item for item in (preview.get("mutation_candidates") or [])
            if isinstance(item, Mapping) and item.get("supported") is True
        ]
    operations = []
    for item in candidates:
        if not isinstance(item, Mapping) or item.get("supported") is not True or item.get("requires_manual_approval") is not True:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI candidate did not satisfy the review contract.")
        try:
            risk = RiskLevel(str(item.get("risk") or ("read_only" if str(item.get("method") or "GET").upper() == "GET" else "reversible_change")))
        except ValueError as exc:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OpenAPI operation risk classification is unknown.") from exc
        operations.append(OpenAPIOperation(
            action=str(item["action"]), summary=str(item.get("summary") or item["operation_id"]),
            path=str(item["path"]), required_scopes=tuple(str(x) for x in item.get("required_scopes", [])),
            path_params=tuple(str(x) for x in item.get("path_params", [])),
            query_params=tuple(str(x) for x in item.get("query_params", [])),
            method=str(item.get("method") or "GET").upper(),
            auth_type=str(item.get("auth_type") or "oauth2"), api_key_header=str(item.get("api_key_header") or ""),
            required_query_params=tuple(str(x) for x in item.get("required_query_params", [])),
            path_param_schemas=dict(item.get("path_param_schemas") or {}),
            query_param_schemas=dict(item.get("query_param_schemas") or {}),
            body_schema=dict(item["body_schema"]) if isinstance(item.get("body_schema"), Mapping) else None,
            body_required=bool(item.get("body_required")), body_content_type=str(item.get("body_content_type") or "application/json"),
            output_schema=dict(item.get("output_schema") or {}), risk=risk,
            idempotent=bool(item.get("idempotent", str(item.get("method") or "GET").upper() in ("GET", "PUT", "DELETE"))),
            pagination=dict(item.get("pagination") or {}),
        ))
    return tuple(operations)


class OAuth2PKCEConnector:
    """Reusable authorization-code/PKCE connector for reviewed read/write API operations.

    State-changing operations are declared by the reviewed capability manifest and
    remain subject to manager-enforced, trusted-UI confirmation before execution.
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
        if self.use_oidc and not set(self.metadata.id_token_signing_alg_values_supported) & {"RS256", "ES256"}:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OIDC mode requires a supported RS256 or ES256 ID-token signing algorithm.")
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
                item.action, item.summary, item.risk, item.required_scopes,
                idempotent=item.idempotent, supported=True,
                input_schema=_operation_input_schema(item),
                pagination=dict(item.pagination),
                output_schema=dict(item.output_schema),
                operation_kind=("read" if item.method == "GET" else "delete" if item.method == "DELETE" else "update" if item.method in ("PUT", "PATCH") else "create_or_submit"),
                changes_state=item.method != "GET",
                reversible=item.risk == RiskLevel.REVERSIBLE,
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
            status_detail="Generic OAuth2 authorization-code/PKCE adapter; configured operations are reviewed and state-changing operations require trusted-UI confirmation.",
            authentication_documentation_url=self.metadata.issuer or self.metadata.authorization_endpoint,
            identity_validation_method="Validated OIDC RS256 ID token plus matching UserInfo sub" if self.use_oidc else "Configured HTTPS UserInfo endpoint and explicit stable identity field",
            supports_token_expiration=True,
            supports_refresh=True,
            supports_revocation=bool(self.metadata.revocation_endpoint),
            pagination_strategy="Provider-specific; no automatic multi-page traversal is inferred from OpenAPI.",
            rate_limit_behavior="Surface 401/403/429 as typed errors; no automatic replay.",
            setup_requirements=("Official OAuth client ID", "Exact registered redirect URI", "Explicit minimum scopes", "Trusted endpoint host allowlist", "Reviewed OpenAPI JSON specification"),
            limitations=("Only reviewed OpenAPI operations with supported authentication and JSON schemas are executable.", "Refresh works only when the provider issues a refresh token.", "Nonstandard signing/protocols require an explicit adapter extension; arbitrary endpoint discovery is disabled."),
            configuration_fields=("provider_id", "display_name", "auth_type", "trusted_hosts", "client_id", "client_secret (protected storage only)", "redirect_uri", "requested_scopes", "issuer or oauth_endpoints", "OpenAPI JSON"),
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
        algorithm = str(header.get("alg") or "") if isinstance(header, Mapping) else ""
        if (not isinstance(header, Mapping) or not isinstance(claims, Mapping)
            or algorithm not in self.metadata.id_token_signing_alg_values_supported
            or algorithm not in ("RS256", "ES256") or not header.get("kid")):
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "OIDC ID token uses an unsupported or undisclosed signing algorithm.")
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
        expected_kty = "RSA" if algorithm == "RS256" else "EC"
        key = next((item for item in keys if isinstance(item, Mapping) and item.get("kid") == header["kid"]
                    and item.get("kty") == expected_kty and item.get("use") in (None, "sig")
                    and item.get("alg") in (None, algorithm)
                    and (algorithm != "ES256" or item.get("crv") == "P-256")), None)
        if not key:
            raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "OIDC signing key was not found in the issuer's JWKS.")
        try:
            from cryptography.hazmat.primitives import hashes
            if algorithm == "RS256":
                from cryptography.hazmat.primitives.asymmetric import padding, rsa
                modulus = int.from_bytes(base64.urlsafe_b64decode(str(key["n"]) + "=" * (-len(str(key["n"])) % 4)), "big")
                exponent = int.from_bytes(base64.urlsafe_b64decode(str(key["e"]) + "=" * (-len(str(key["e"])) % 4)), "big")
                public_key = rsa.RSAPublicNumbers(exponent, modulus).public_key()
                public_key.verify(signature, (parts[0] + "." + parts[1]).encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
            else:
                if len(signature) != 64:
                    raise ValueError("ES256 JOSE signature must contain exactly 64 bytes.")
                from cryptography.hazmat.primitives.asymmetric import ec
                from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
                x = int.from_bytes(base64.urlsafe_b64decode(str(key["x"]) + "=" * (-len(str(key["x"])) % 4)), "big")
                y = int.from_bytes(base64.urlsafe_b64decode(str(key["y"]) + "=" * (-len(str(key["y"])) % 4)), "big")
                public_key = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
                r_value = int.from_bytes(signature[:32], "big")
                s_value = int.from_bytes(signature[32:], "big")
                der_signature = encode_dss_signature(r_value, s_value)
                public_key.verify(der_signature, (parts[0] + "." + parts[1]).encode("ascii"), ec.ECDSA(hashes.SHA256()))
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
        pagination = dict(operation.pagination or {})
        pagination_parameters = {
            str(pagination.get("cursor_parameter") or ""),
            str(pagination.get("page_size_parameter") or ""),
        } - {""}
        allowed_args = set(operation.path_params) | (set(operation.query_params) - pagination_parameters)
        if operation.body_schema is not None:
            allowed_args.add("body")
        if set(args) - allowed_args:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Operation arguments contain parameters not declared by the reviewed OpenAPI contract.")
        if operation.body_required and "body" not in args:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "This operation requires a JSON request body.")
        body = args.get("body") if operation.body_schema is not None and "body" in args else None
        if operation.body_schema is not None and "body" in args:
            _validate_operation_value(body, operation.body_schema)
        path = operation.path
        for name in operation.path_params:
            value = args.get(name)
            if value is None:
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "A required path parameter is missing.")
            schema = operation.path_param_schemas.get(name)
            if schema is not None:
                _validate_operation_value(value, schema)
            elif isinstance(value, (dict, list, bool)) or len(str(value)) > 256 or not str(value):
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "A required path parameter is invalid.")
            if isinstance(value, (dict, list, bool)) or len(str(value)) > 256 or not str(value):
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "A path parameter cannot be encoded safely.")
            path = path.replace("{" + name + "}", urllib.parse.quote(str(value), safe=""))
        if _PATH_PARAM_RE.search(path):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Operation path contains an unresolved path parameter.")
        url = self.api_base_url + "/" + path.lstrip("/")
        query_items = []
        for name in operation.query_params:
            if name not in args:
                if name in operation.required_query_params:
                    raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "A required query parameter is missing.")
                continue
            value = args[name]
            schema = operation.query_param_schemas.get(name)
            if schema is not None:
                _validate_operation_value(value, schema)
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
        request_options: dict[str, Any] = {"method": operation.method, "token": str(credentials.get("access_token") or ""), "timeout": 8.0}
        if body is not None:
            request_options["json_body"] = body
            request_options["content_type"] = operation.body_content_type
        if operation.pagination:
            result, evidence = _execute_cursor_pagination(
                url, operation, lambda page_url: self._requester(page_url, **request_options)
            )
            return _sanitize_payload(result), evidence
        response = self._requester(url, **request_options)
        if response.status == 401:
            raise IntegrationError(IntegrationErrorCode.AUTHENTICATION_REQUIRED, "Provider rejected the access token; refresh or reconnect the account.")
        if response.status == 403:
            raise IntegrationError(IntegrationErrorCode.MISSING_PERMISSION, "Provider denied this operation.")
        if response.status == 429:
            raise IntegrationError(IntegrationErrorCode.RATE_LIMITED, "Provider rate limit reached; retry later.", retryable=True)
        if response.status < 200 or response.status >= 300:
            raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Provider operation failed.")
        if operation.output_schema and response.status != 204:
            try:
                _validate_operation_value(response.data, operation.output_schema)
            except IntegrationError as exc:
                raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Provider response did not match the declared response schema.") from exc
        return _sanitize_payload(response.data), {
            "verified": operation.method == "GET",
            "source": "reviewed OpenAPI operation",
            "operation": operation.action,
            "method": operation.method,
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
    method: str = "GET",
    json_body: Any = None,
    content_type: str = "application/json",
    timeout: float = 8.0,
) -> HttpResponse:
    """Send one bounded authenticated JSON request with redirects disabled."""
    _check_https_url(url, allowed_hosts=trusted_hosts)
    key = str(api_key or "")
    header = str(header_name or "")
    method = str(method or "GET").upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "HTTP method is not supported by the generic connector.")
    if not key or len(key) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in key):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "API key is empty or contains forbidden header characters.")
    if not _HEADER_TOKEN.fullmatch(header):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "API-key header name is invalid.")
    request_headers = {
        "Accept": "application/json", "User-Agent": "Brahma-Evo-Account-Integrations",
        header: ("Bearer " + key) if bearer_token else key,
    }
    body = None
    if json_body is not None:
        if content_type != "application/json" and not str(content_type).endswith("+json"):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Only JSON request content types are supported.")
        try:
            body = json.dumps(json_body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Request body is not valid JSON data.") from exc
        if len(body) > 512 * 1024:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Request body exceeds the 512 KiB safety limit.")
        request_headers["Content-Type"] = str(content_type)
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect())
    response_headers = {}
    try:
        with opener.open(request, timeout=max(0.5, min(float(timeout), 15.0))) as response:
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
        raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Provider redirected an API request; redirects are disabled.")
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Provider response exceeded the 1 MiB safety limit.")
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Provider returned malformed JSON.") from exc
    if not isinstance(data, (dict, list)):
        raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Provider returned an unexpected response format.")
    return HttpResponse(status, response_headers, data)



class APIKeyConnector:
    """Reusable header-based API-key/bearer adapter for reviewed API operations.

    Credentials are sent only using the declared header contract. State-changing
    operations still pass through manager-enforced confirmation and scope checks.
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
            Capability(
                item.action, item.summary, item.risk, item.required_scopes,
                idempotent=item.idempotent, supported=True,
                input_schema=_operation_input_schema(item),
                pagination=dict(item.pagination),
                output_schema=dict(item.output_schema),
                operation_kind=("read" if item.method == "GET" else "delete" if item.method == "DELETE" else "update" if item.method in ("PUT", "PATCH") else "create_or_submit"),
                changes_state=item.method != "GET",
                reversible=item.risk == RiskLevel.REVERSIBLE,
            )
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
            status_detail="Header-based API-key/bearer authentication with identity validation and reviewed operations; writes require trusted-UI confirmation.",
            authentication_documentation_url=str(documentation_url or "")[:2048],
            identity_validation_method="Configured HTTPS identity endpoint; response must contain the declared stable identity field",
            supports_token_expiration=False,
            supports_refresh=False,
            supports_revocation=False,
            pagination_strategy="Provider-specific; no automatic multi-page traversal is inferred from OpenAPI.",
            rate_limit_behavior="Surface 401/403/429 as typed errors; no automatic replay.",
            setup_requirements=("Official provider API credential", "Documented identity endpoint", "Trusted HTTPS host allowlist", "Reviewed OpenAPI JSON specification", "Credential sent only in an explicit header"),
            limitations=("Header API-key and bearer authentication only; query-string and cookie credentials are disabled.", "Only explicitly reviewed operations with supported JSON schemas are executable.", "Provider-specific key rotation/revocation must be handled by the provider's security settings."),
            configuration_fields=("provider_id", "display_name", "auth_type", "trusted_hosts", "api_key_header", "identity_url", "identity_field", "documentation_url", "OpenAPI JSON"),
            capabilities=tuple(capabilities),
        )

    def _request(self, url: str, api_key: str, *, method: str = "GET", json_body: Any = None, content_type: str = "application/json") -> HttpResponse:
        _check_https_url(url, allowed_hosts=self.trusted_hosts)
        options: dict[str, Any] = {
            "method": method, "api_key": api_key, "api_key_header": self.api_key_header, "timeout": 8.0,
        }
        if json_body is not None:
            options["json_body"] = json_body
            options["content_type"] = content_type
        if self._requester is not None:
            return self._requester(url, **options)
        return _default_header_requester(
            url, api_key=api_key, header_name=self.api_key_header,
            trusted_hosts=self.trusted_hosts, bearer_token=(self.credential_auth_type == "bearer"),
            method=method, json_body=json_body, content_type=content_type, timeout=8.0,
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
        pagination = dict(operation.pagination or {})
        pagination_parameters = {
            str(pagination.get("cursor_parameter") or ""),
            str(pagination.get("page_size_parameter") or ""),
        } - {""}
        allowed_args = set(operation.path_params) | (set(operation.query_params) - pagination_parameters)
        if operation.body_schema is not None:
            allowed_args.add("body")
        if set(args) - allowed_args:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Operation arguments contain parameters not declared by the reviewed API contract.")
        if operation.body_required and "body" not in args:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "This operation requires a JSON request body.")
        body = args.get("body") if operation.body_schema is not None and "body" in args else None
        if operation.body_schema is not None and "body" in args:
            _validate_operation_value(body, operation.body_schema)
        path = operation.path
        for name in operation.path_params:
            value = args.get(name)
            if value is None:
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "A required path parameter is missing.")
            schema = operation.path_param_schemas.get(name)
            if schema is not None:
                _validate_operation_value(value, schema)
            elif isinstance(value, (dict, list, bool)) or len(str(value)) > 256 or not str(value):
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "A required path parameter is invalid.")
            if isinstance(value, (dict, list, bool)) or len(str(value)) > 256 or not str(value):
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "A path parameter cannot be encoded safely.")
            path = path.replace("{" + name + "}", urllib.parse.quote(str(value), safe=""))
        if _PATH_PARAM_RE.search(path):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Operation path contains an unresolved path parameter.")
        url = self.api_base_url + "/" + path.lstrip("/")
        query_items = []
        for name in operation.query_params:
            if name not in args:
                if name in operation.required_query_params:
                    raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "A required query parameter is missing.")
                continue
            value = args[name]
            schema = operation.query_param_schemas.get(name)
            if schema is not None:
                _validate_operation_value(value, schema)
            values = value if isinstance(value, (list, tuple)) else [value]
            if len(values) > 20:
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Query parameter contains too many values.")
            for item in values:
                if item is None or isinstance(item, (dict, list, tuple)) or len(str(item)) > 512 or any(ch in str(item) for ch in "\r\n"):
                    raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "A query parameter is invalid.")
                query_items.append((name, str(item)))
        if query_items:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query_items)
        parsed = _check_https_url(url, allowed_hosts=self.trusted_hosts)
        base = urllib.parse.urlparse(self.api_base_url)
        if _normalized_host(parsed.hostname or "") != _normalized_host(base.hostname or ""):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Resolved operation URL left the configured API host.")
        if operation.pagination:
            result, evidence = _execute_cursor_pagination(
                url, operation, lambda page_url: self._request(page_url, api_key, method="GET")
            )
            return _sanitize_payload(result), evidence
        response = self._request(url, api_key, method=operation.method, json_body=body, content_type=operation.body_content_type)
        self._status_error(response, operation="API")
        if operation.output_schema and response.status != 204:
            try:
                _validate_operation_value(response.data, operation.output_schema)
            except IntegrationError as exc:
                raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "Provider response did not match the declared response schema.") from exc
        return _sanitize_payload(response.data), {
            "verified": operation.method == "GET",
            "source": "reviewed OpenAPI operation",
            "operation": operation.action,
            "method": operation.method,
            "http_status": response.status,
        }



@dataclass(frozen=True)
class GraphQLOperation:
    action: str
    operation_name: str
    document: str
    summary: str
    operation_type: str
    required_scopes: tuple[str, ...]
    risk: RiskLevel
    schema: Any
    variable_definitions: tuple[Any, ...]
    variable_names: tuple[str, ...]
    input_schema: Mapping[str, Any]
    idempotent: bool = False


def _graphql_library():
    try:
        from graphql import (
            build_client_schema, build_schema, get_named_type, get_variable_values,
            is_enum_type, is_input_object_type, is_list_type, is_non_null_type,
            is_scalar_type, parse, type_from_ast, validate, validate_schema,
        )
    except ImportError as exc:
        raise IntegrationError(IntegrationErrorCode.UNSUPPORTED_ACTION, "GraphQL support requires the packaged graphql-core dependency.") from exc
    return {
        "build_client_schema": build_client_schema, "build_schema": build_schema,
        "get_named_type": get_named_type, "get_variable_values": get_variable_values,
        "is_enum_type": is_enum_type, "is_input_object_type": is_input_object_type,
        "is_list_type": is_list_type, "is_non_null_type": is_non_null_type,
        "is_scalar_type": is_scalar_type, "parse": parse, "type_from_ast": type_from_ast,
        "validate": validate, "validate_schema": validate_schema,
    }


def _graphql_type_schema(value: Any, lib: Mapping[str, Any], *, depth: int = 0, seen: frozenset[str] = frozenset()) -> dict[str, Any]:
    if depth > 10:
        return {"description": "Validated against the imported GraphQL schema."}
    if lib["is_non_null_type"](value):
        return _graphql_type_schema(value.of_type, lib, depth=depth + 1, seen=seen)
    if lib["is_list_type"](value):
        return {"type": "array", "items": _graphql_type_schema(value.of_type, lib, depth=depth + 1, seen=seen), "maxItems": 100}
    named = lib["get_named_type"](value)
    if lib["is_scalar_type"](named):
        simple = {"String": "string", "ID": "string", "Int": "integer", "Float": "number", "Boolean": "boolean"}
        return {"type": simple[named.name]} if named.name in simple else {"description": "Custom GraphQL scalar " + named.name + " is runtime-validated by graphql-core."}
    if lib["is_enum_type"](named):
        return {"type": "string", "enum": sorted(named.values.keys())[:100]}
    if lib["is_input_object_type"](named):
        if named.name in seen:
            return {"type": "object", "description": "Recursive input type validated by graphql-core: " + named.name}
        props, required = {}, []
        for field_name, field in list(named.fields.items())[:_MAX_SPEC_NODES]:
            props[field_name] = _graphql_type_schema(field.type, lib, depth=depth + 1, seen=seen | {named.name})
            if lib["is_non_null_type"](field.type) and field.default_value is None:
                required.append(field_name)
        return {"type": "object", "properties": props, "required": required, "additionalProperties": False}
    return {"description": "Validated by the imported GraphQL schema."}


def _load_graphql_schema(schema_document: str | Mapping[str, Any]) -> tuple[Any, Mapping[str, Any]]:
    lib = _graphql_library()
    if isinstance(schema_document, Mapping):
        raw, source = dict(schema_document), json.dumps(schema_document, ensure_ascii=False)
    elif isinstance(schema_document, str):
        source = schema_document
        try:
            raw = json.loads(source) if source.lstrip().startswith("{") else None
        except json.JSONDecodeError:
            raw = None
    else:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "GraphQL schema must be SDL or JSON introspection data.")
    if not source.strip() or len(source.encode("utf-8")) > 1024 * 1024:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "GraphQL schema is empty or exceeds the 1 MiB limit.")
    try:
        if isinstance(raw, Mapping):
            introspection = raw.get("data", raw)
            if not isinstance(introspection, Mapping) or not isinstance(introspection.get("__schema"), Mapping):
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "GraphQL JSON schema must contain an introspection __schema object.")
            schema = lib["build_client_schema"](dict(introspection))
        else:
            schema = lib["build_schema"](source)
        schema_errors = lib["validate_schema"](schema)
        if schema_errors:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Invalid GraphQL schema: " + str(schema_errors[0].message)[:220])
    except IntegrationError:
        raise
    except Exception as exc:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "GraphQL SDL/introspection schema could not be validated.") from exc
    return schema, lib


def _build_graphql_operations(schema: Any, lib: Mapping[str, Any], *, provider_id: str, endpoint: str, declarations: Any):
    if not isinstance(declarations, list) or not declarations or len(declarations) > _MAX_OPERATIONS:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "GraphQL operation count exceeds the imported-document safety limit.")
    built, queries, mutations, warnings, seen = [], [], [], [], set()
    total_bytes = 0
    for raw in declarations:
        if not isinstance(raw, Mapping):
            warnings.append("Skipped a GraphQL operation with a non-object declaration.")
            continue
        op_id = str(raw.get("operation_id") or raw.get("operationId") or "").strip()
        summary = str(raw.get("summary") or op_id or "GraphQL operation").strip()[:300]
        document = str(raw.get("document") or raw.get("query") or "")
        total_bytes += len(document.encode("utf-8"))
        if total_bytes > 256 * 1024:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Configured GraphQL documents exceed the 256 KiB total limit.")
        action = provider_id + "." + op_id
        if not _OPERATION_RE.fullmatch(op_id) or op_id in seen:
            warnings.append("Skipped a GraphQL operation with a missing, invalid, or duplicate operation ID.")
            continue
        seen.add(op_id)
        try:
            if not document.strip() or len(document.encode("utf-8")) > 16 * 1024:
                raise ValueError("Document is empty or exceeds 16 KiB.")
            ast = lib["parse"](document)
            definitions = [item for item in ast.definitions if item.__class__.__name__ == "OperationDefinitionNode"]
            if len(definitions) != 1:
                raise ValueError("Each action must declare exactly one operation definition.")
            op_ast = definitions[0]
            operation_name = op_ast.name.value if op_ast.name is not None else ""
            operation_type = str(op_ast.operation.value)
            if not operation_name or not _OPERATION_RE.fullmatch(operation_name):
                raise ValueError("GraphQL operations must have an explicit stable name.")
            if operation_type not in ("query", "mutation"):
                raise ValueError("Only GraphQL queries and mutations are supported.")
            errors = lib["validate"](schema, ast, max_errors=25)
            if errors:
                raise ValueError("Schema validation failed: " + str(errors[0].message)[:200])
            var_defs = tuple(op_ast.variable_definitions or ())
            var_names = tuple(item.variable.name.value for item in var_defs)
            if len(set(var_names)) != len(var_names):
                raise ValueError("Duplicate GraphQL variable declaration.")
            properties, required = {}, []
            for item in var_defs:
                gql_type = lib["type_from_ast"](schema, item.type)
                if gql_type is None:
                    raise ValueError("Could not resolve a GraphQL variable type.")
                name = item.variable.name.value
                properties[name] = _graphql_type_schema(gql_type, lib)
                if lib["is_non_null_type"](gql_type) and item.default_value is None:
                    required.append(name)
            input_schema = {"type": "object", "properties": properties, "required": required, "additionalProperties": False}
            raw_scopes = raw.get("required_scopes", [])
            if not isinstance(raw_scopes, list) or any(not isinstance(x, str) or not x.strip() for x in raw_scopes):
                raise ValueError("required_scopes must be a list of nonempty strings.")
            scopes = tuple(sorted(set(x.strip() for x in raw_scopes)))
            if operation_type == "query":
                risk, idempotent = RiskLevel.READ_ONLY, True
            else:
                risk = _operation_risk("POST", {"summary": summary, "description": document}, "/graphql/" + operation_name)
                if raw.get("risk"):
                    configured = RiskLevel(str(raw["risk"]))
                    ranks = {RiskLevel.READ_ONLY: 0, RiskLevel.REVERSIBLE: 1, RiskLevel.EXTERNAL_COMMUNICATION: 2, RiskLevel.PUBLICATION: 3, RiskLevel.FINANCIAL: 4, RiskLevel.DESTRUCTIVE: 5}
                    if ranks[configured] > ranks[risk]:
                        risk = configured
                idempotent = raw.get("idempotent") is True
            op = GraphQLOperation(action, operation_name, document, summary, operation_type, scopes, risk, schema, var_defs, var_names, input_schema, idempotent)
            candidate = {
                "action": action, "operation_id": op_id, "operation_name": operation_name, "summary": summary,
                "method": "GRAPHQL QUERY" if operation_type == "query" else "GRAPHQL MUTATION",
                "path": endpoint, "document": document, "protocol": "graphql",
                "required_scopes": list(scopes), "input_schema": input_schema, "risk": risk.value,
                "idempotent": idempotent, "operation_type": operation_type,
                "supported": True, "requires_manual_approval": True,
            }
            built.append(op)
            (queries if operation_type == "query" else mutations).append(candidate)
        except Exception as exc:
            message = str(exc)[:250]
            warnings.append("Skipped GraphQL operation " + op_id + ": " + message)
            mutations.append({
                "action": action, "operation_id": op_id, "summary": summary, "method": "GRAPHQL",
                "path": endpoint, "document": document[:1200], "protocol": "graphql", "risk": "unsupported",
                "supported": False, "requires_manual_approval": True, "unsupported_reason": message,
            })
    return tuple(built), queries, mutations, warnings


def _graphql_capabilities(provider_id: str, operations: Sequence[GraphQLOperation]) -> tuple[Capability, ...]:
    capabilities = [Capability(
        action=op.action, description=op.summary, risk=op.risk, required_scopes=op.required_scopes,
        idempotent=op.idempotent, supported=True, input_schema=dict(op.input_schema),
        operation_kind="read" if op.operation_type == "query" else "graphql_mutation",
        changes_state=op.operation_type == "mutation",
        reversible=op.operation_type == "mutation" and op.risk == RiskLevel.REVERSIBLE,
    ) for op in operations]
    capabilities.append(Capability(
        action=provider_id + ".connection.test",
        description="Validate the configured identity endpoint and provider credential.",
        risk=RiskLevel.READ_ONLY, operation_kind="connection_test",
    ))
    return tuple(capabilities)


def _graphql_preview(provider_id: str, schema_document: str | Mapping[str, Any], endpoint: str,
                     queries: list[dict[str, Any]], mutations: list[dict[str, Any]], warnings: list[str]) -> dict[str, Any]:
    source = schema_document if isinstance(schema_document, str) else json.dumps(schema_document, sort_keys=True)
    return {
        "provider_id": provider_id, "protocol": "graphql",
        "schema_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(), "server_url": endpoint,
        "read_only_candidates": queries, "mutation_candidates": mutations,
        "review_candidates": queries + [item for item in mutations if item.get("supported") is True],
        "warnings": warnings, "registered": False,
        "note": "Inert preview. Only explicitly configured, schema-validated operations are enabled; each mutation requires individual confirmation.",
    }


def _execute_graphql(endpoint: str, action: str, arguments: Mapping[str, Any], operation: GraphQLOperation,
                     requester: Callable[..., HttpResponse], *, token: str = "", api_key: str = "", api_key_header: str = ""):
    if set(arguments) - set(operation.variable_names):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "GraphQL arguments include variables not declared by the reviewed operation.")
    try:
        encoded = json.dumps(dict(arguments), ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > 256 * 1024:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "GraphQL variables exceed the 256 KiB limit.")
    except (TypeError, ValueError) as exc:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "GraphQL variables must be JSON-serializable.") from exc
    lib = _graphql_library()
    try:
        variables = lib["get_variable_values"](operation.schema, operation.variable_definitions, dict(arguments), max_errors=25)
    except Exception as exc:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "GraphQL variables do not match the imported schema.") from exc
    if isinstance(variables, list):
        detail = str(variables[0].message)[:180] if variables else "invalid variables"
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Invalid GraphQL variables: " + detail)
    payload = {"query": operation.document, "operationName": operation.operation_name, "variables": variables}
    kwargs = {"method": "POST", "json_body": payload, "content_type": "application/json", "timeout": 8.0}
    if api_key:
        kwargs.update({"api_key": api_key, "api_key_header": api_key_header})
    else:
        kwargs["token"] = token
    try:
        response = requester(endpoint, **kwargs)
    except (TypeError, ValueError) as exc:
        raise IntegrationError(IntegrationErrorCode.NETWORK_ERROR, "GraphQL transport does not support authenticated JSON POST requests.") from exc
    if response.status == 401:
        raise IntegrationError(IntegrationErrorCode.AUTHENTICATION_REQUIRED, "GraphQL provider rejected the credential; reconnect the account.")
    if response.status == 403:
        raise IntegrationError(IntegrationErrorCode.MISSING_PERMISSION, "GraphQL provider denied this operation.")
    if response.status == 429:
        raise IntegrationError(IntegrationErrorCode.RATE_LIMITED, "GraphQL provider rate limit reached; retry later.", retryable=True)
    if response.status < 200 or response.status >= 300:
        raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "GraphQL provider request failed.")
    if not isinstance(response.data, Mapping):
        raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "GraphQL provider returned a malformed response.")
    errors = response.data.get("errors")
    if errors:
        if not isinstance(errors, list):
            raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "GraphQL provider returned a malformed error envelope.")
        raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "GraphQL provider reported operation errors; success was not claimed.")
    data = response.data.get("data")
    if not isinstance(data, Mapping):
        raise IntegrationError(IntegrationErrorCode.PROVIDER_ERROR, "GraphQL response did not contain a valid data object.")
    evidence = {"source": "schema-validated GraphQL operation", "operation": action, "operation_type": operation.operation_type, "http_status": response.status}
    if operation.operation_type == "query":
        evidence["verified"] = True
    return _sanitize_payload(data), evidence


class GraphQLAPIKeyConnector(APIKeyConnector):
    """GraphQL schema adapter reusing the existing API-key/bearer credential lifecycle."""
    def __init__(self, *, graphql_endpoint_url: str, graphql_schema: Any,
                 graphql_operations: Sequence[GraphQLOperation], credential_auth_type: str = "api_key", **kwargs):
        pseudo = tuple(OpenAPIOperation(
            action=op.action, summary=op.summary, path="/graphql",
            required_scopes=op.required_scopes, method="GET" if op.operation_type == "query" else "PATCH",
            auth_type=credential_auth_type, api_key_header=kwargs["api_key_header"],
            risk=op.risk, idempotent=op.idempotent,
        ) for op in graphql_operations)
        self.graphql_endpoint_url, self.graphql_schema = graphql_endpoint_url, graphql_schema
        self.graphql_operations = {op.action: op for op in graphql_operations}
        super().__init__(operations=pseudo, api_base_url=graphql_endpoint_url, **kwargs)
        # Keep the explicit auth mode authoritative. An API key named Authorization
        # must not silently be converted to a Bearer credential.
        self.credential_auth_type = credential_auth_type
        self.operations = self.graphql_operations
        old = self.manifest
        self.manifest = IntegrationManifest(
            provider_id=self.provider_id, display_name=old.display_name, auth_method=old.auth_method,
            capabilities=_graphql_capabilities(self.provider_id, graphql_operations),
            documentation_url=old.documentation_url, status=ConnectionStatus.LIMITED_SUPPORT,
            status_detail="Schema-validated GraphQL operations with explicit mutation confirmation.",
            authentication_documentation_url=old.authentication_documentation_url,
            identity_validation_method=old.identity_validation_method,
            pagination_strategy="Provider-declared cursor fields only; pagination is not guessed.",
            rate_limit_behavior="GraphQL 401/403/429 and error envelopes are surfaced; no automatic replay.",
            setup_requirements=("Official HTTPS GraphQL endpoint", "Validated SDL/introspection JSON", "Reviewed named query/mutation documents", "Documented credential header", "Stable identity endpoint"),
            limitations=("GraphQL queries and mutations are explicitly configured; introspection is not fetched automatically.", "Mutation responses remain accepted-but-unverified without a separate readback.", "Subscriptions and multipart uploads need a dedicated adapter."),
            configuration_fields=("protocol=graphql", "graphql_endpoint_url", "graphql_operations", "GraphQL schema SDL/introspection JSON", "auth_type", "identity_url"),
        )

    def execute(self, action: str, arguments: Mapping[str, Any], credentials: Mapping[str, Any]):
        if action == self.provider_id + ".connection.test":
            identity, scopes = self.validate_credentials(credentials)
            return {"identity": identity, "scopes": list(scopes)}, {"verified": True, "source": "configured identity endpoint", "identity": identity, "http_status": 200}
        operation = self.graphql_operations.get(action)
        if operation is None:
            raise IntegrationError(IntegrationErrorCode.UNSUPPORTED_ACTION, "The provider does not declare this GraphQL operation.")
        scopes = credentials.get("scopes") or ()
        if isinstance(scopes, str):
            scopes = scopes.split()
        if set(operation.required_scopes) - {str(x).strip() for x in scopes if str(x).strip()}:
            raise IntegrationError(IntegrationErrorCode.MISSING_PERMISSION, "The connected account lacks a scope required by this GraphQL operation.")
        api_key = str(credentials.get("api_key") or "")
        if not api_key:
            raise IntegrationError(IntegrationErrorCode.AUTHENTICATION_REQUIRED, "Provider API key is missing.")
        return _execute_graphql(
            self.graphql_endpoint_url, action, arguments or {}, operation,
            lambda url, **kw: self._request(url, api_key, method=kw.get("method", "POST"), json_body=kw.get("json_body"), content_type=kw.get("content_type", "application/json")),
            api_key=api_key, api_key_header=self.api_key_header,
        )


class GraphQLOAuth2PKCEConnector(OAuth2PKCEConnector):
    """GraphQL adapter reusing the existing OAuth2/OIDC PKCE lifecycle."""
    def __init__(self, *, graphql_endpoint_url: str, graphql_schema: Any,
                 graphql_operations: Sequence[GraphQLOperation], **kwargs):
        use_oidc = bool(kwargs.get("use_oidc", True))
        pseudo = tuple(OpenAPIOperation(
            action=op.action, summary=op.summary, path="/graphql",
            required_scopes=op.required_scopes, method="GET" if op.operation_type == "query" else "PATCH",
            auth_type="oidc" if use_oidc else "oauth2", risk=op.risk, idempotent=op.idempotent,
        ) for op in graphql_operations)
        self.graphql_endpoint_url, self.graphql_schema = graphql_endpoint_url, graphql_schema
        self.graphql_operations = {op.action: op for op in graphql_operations}
        super().__init__(operations=pseudo, api_base_url=graphql_endpoint_url, **kwargs)
        self.operations = self.graphql_operations
        old = self.manifest
        self.manifest = IntegrationManifest(
            provider_id=self.provider_id, display_name=old.display_name, auth_method=old.auth_method,
            capabilities=_graphql_capabilities(self.provider_id, graphql_operations),
            documentation_url=old.documentation_url, status=ConnectionStatus.LIMITED_SUPPORT,
            status_detail="Schema-validated GraphQL operations with explicit mutation confirmation.",
            authentication_documentation_url=old.authentication_documentation_url,
            identity_validation_method=old.identity_validation_method, supports_token_expiration=True,
            supports_refresh=True, supports_revocation=bool(self.metadata.revocation_endpoint),
            pagination_strategy="Provider-declared GraphQL cursor fields only.",
            rate_limit_behavior="GraphQL 401/403/429 and error envelopes are surfaced; no automatic replay.",
            setup_requirements=("Official HTTPS GraphQL endpoint", "Validated SDL/introspection JSON", "Reviewed operation documents", "OAuth2/OIDC client and scopes"),
            limitations=("Mutation responses remain accepted-but-unverified without readback.", "Subscriptions and multipart uploads need a dedicated adapter."),
            configuration_fields=("protocol=graphql", "graphql_endpoint_url", "graphql_operations", "GraphQL schema SDL/introspection JSON", "OAuth2/OIDC client/issuer/endpoints", "minimum scopes"),
        )

    def execute(self, action: str, arguments: Mapping[str, Any], credentials: Mapping[str, Any]):
        if action == self.provider_id + ".connection.test":
            identity, scopes = self.validate_credentials(credentials)
            return {"identity": identity, "scopes": list(scopes)}, {"verified": True, "source": "configured UserInfo endpoint", "identity": identity, "http_status": 200}
        operation = self.graphql_operations.get(action)
        if operation is None:
            raise IntegrationError(IntegrationErrorCode.UNSUPPORTED_ACTION, "The provider does not declare this GraphQL operation.")
        raw_scopes = credentials.get("scopes") or ()
        if isinstance(raw_scopes, str):
            raw_scopes = raw_scopes.split()
        if set(operation.required_scopes) - {str(x).strip() for x in raw_scopes if str(x).strip()}:
            raise IntegrationError(IntegrationErrorCode.MISSING_PERMISSION, "The connected account lacks a scope required by this GraphQL operation.")
        try:
            expires_at = float(credentials.get("expires_at") or 0)
        except (TypeError, ValueError):
            expires_at = 0
        if expires_at and expires_at <= time.time():
            raise IntegrationError(IntegrationErrorCode.AUTHORIZATION_EXPIRED, "Provider token expired; refresh or reconnect the account.")
        return _execute_graphql(
            self.graphql_endpoint_url, action, arguments or {}, operation,
            lambda url, **kw: self._requester(url, **kw), token=str(credentials.get("access_token") or ""),
        )


def _configure_graphql_connector(config: Mapping[str, Any], schema_document: str | Mapping[str, Any], *,
                                 provider_id: str, display_name: str, auth_type: str,
                                 trusted_hosts: Sequence[str], requester: Callable[..., HttpResponse] | None):
    endpoint = str(config.get("graphql_endpoint_url") or "").strip()
    if not endpoint:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "GraphQL protocol requires the documented graphql_endpoint_url.")
    parsed = _check_https_url(endpoint, allowed_hosts=trusted_hosts)
    if parsed.query or parsed.fragment:
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "GraphQL endpoint may not contain a query or fragment.")
    schema, lib = _load_graphql_schema(schema_document)
    operations, queries, mutations, warnings = _build_graphql_operations(
        schema, lib, provider_id=provider_id, endpoint=endpoint, declarations=config.get("graphql_operations"),
    )
    if not operations:
        raise IntegrationError(IntegrationErrorCode.UNSUPPORTED_ACTION, "GraphQL configuration has no valid explicitly declared query or mutation operations.")
    if auth_type in ("api_key", "bearer"):
        identity_url = str(config.get("identity_url") or "").strip()
        if not identity_url:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Configure a documented identity endpoint to validate GraphQL credentials.")
        header = str(config.get("api_key_header") or ("Authorization" if auth_type == "bearer" else "")).strip()
        if auth_type == "bearer":
            header = "Authorization"
        if not header:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Configure the documented GraphQL credential header.")
        connector = GraphQLAPIKeyConnector(
            provider_id=provider_id, display_name=display_name, identity_url=identity_url,
            graphql_endpoint_url=endpoint, graphql_schema=schema, graphql_operations=operations,
            api_key_header=header, credential_auth_type=auth_type, trusted_hosts=trusted_hosts,
            identity_field=str(config.get("identity_field") or "id"),
            documentation_url=str(config.get("documentation_url") or "")[:2048], requester=requester,
        )
    else:
        client_id, redirect_uri = str(config.get("client_id") or "").strip(), str(config.get("redirect_uri") or "").strip()
        if not client_id or not redirect_uri:
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "GraphQL OAuth requires official client_id and exact registered redirect_uri.")
        raw_scopes = config.get("requested_scopes")
        if not isinstance(raw_scopes, (list, tuple)) or not raw_scopes or any(not isinstance(x, str) or not x.strip() for x in raw_scopes):
            raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "requested_scopes must explicitly list minimum provider permissions.")
        requested_scopes = tuple(dict.fromkeys(str(x).strip() for x in raw_scopes))
        identity_field = "sub"
        if auth_type == "oidc":
            issuer = str(config.get("issuer") or "").strip()
            if not issuer or "openid" not in requested_scopes:
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "GraphQL OIDC requires its official issuer and openid scope.")
            cached = config.get("oidc_metadata")
            if isinstance(cached, Mapping):
                if str(cached.get("issuer") or "") != issuer:
                    raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "Cached OIDC issuer does not match the configured issuer.")
                algs = tuple(x for x in cached.get("id_token_signing_alg_values_supported", []) if isinstance(x, str) and x in ("RS256", "ES256")) if isinstance(cached.get("id_token_signing_alg_values_supported"), list) else ()
                supported = tuple(x for x in cached.get("scopes_supported", []) if isinstance(x, str) and x.strip()) if isinstance(cached.get("scopes_supported"), list) else ()
                metadata = OAuthProviderMetadata(
                    issuer=issuer, authorization_endpoint=str(cached.get("authorization_endpoint") or ""),
                    token_endpoint=str(cached.get("token_endpoint") or ""), userinfo_endpoint=str(cached.get("userinfo_endpoint") or ""),
                    jwks_uri=str(cached.get("jwks_uri") or ""), revocation_endpoint=str(cached.get("revocation_endpoint") or ""),
                    scopes_supported=supported, id_token_signing_alg_values_supported=algs,
                )
                if not all((metadata.authorization_endpoint, metadata.token_endpoint, metadata.userinfo_endpoint, metadata.jwks_uri)) or not algs:
                    raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "Cached GraphQL OIDC metadata is incomplete.")
                for url in (issuer, metadata.authorization_endpoint, metadata.token_endpoint, metadata.userinfo_endpoint, metadata.jwks_uri, metadata.revocation_endpoint):
                    if url:
                        _check_https_url(url, allowed_hosts=trusted_hosts)
            else:
                metadata = discover_oidc_metadata(issuer, trusted_hosts=trusted_hosts, requester=requester)
            if metadata.scopes_supported and not set(requested_scopes).issubset(set(metadata.scopes_supported)):
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "GraphQL OIDC requests scopes not advertised by the provider.")
        else:
            endpoints = config.get("oauth_endpoints")
            if not isinstance(endpoints, Mapping):
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "GraphQL OAuth2 requires documented authorization, token and UserInfo endpoints.")
            metadata = OAuthProviderMetadata(
                issuer=str(endpoints.get("issuer") or ""), authorization_endpoint=str(endpoints.get("authorization_endpoint") or ""),
                token_endpoint=str(endpoints.get("token_endpoint") or ""), userinfo_endpoint=str(endpoints.get("userinfo_endpoint") or ""),
                jwks_uri=str(endpoints.get("jwks_uri") or ""), revocation_endpoint=str(endpoints.get("revocation_endpoint") or ""),
                scopes_supported=tuple(x for x in endpoints.get("scopes_supported", ()) if isinstance(x, str)),
            )
            if not all((metadata.authorization_endpoint, metadata.token_endpoint, metadata.userinfo_endpoint)):
                raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "GraphQL OAuth2 metadata must define authorization, token and UserInfo endpoints.")
            for url in (metadata.authorization_endpoint, metadata.token_endpoint, metadata.userinfo_endpoint, metadata.jwks_uri, metadata.revocation_endpoint):
                if url:
                    _check_https_url(url, allowed_hosts=trusted_hosts)
            identity_field = str(config.get("identity_field") or "id")
        connector = GraphQLOAuth2PKCEConnector(
            provider_id=provider_id, display_name=display_name, client_id=client_id,
            client_secret=str(config.get("client_secret") or ""), redirect_uri=redirect_uri,
            metadata=metadata, api_base_url=endpoint, graphql_endpoint_url=endpoint, graphql_schema=schema,
            graphql_operations=operations, requested_scopes=requested_scopes, identity_field=identity_field,
            trusted_hosts=trusted_hosts, requester=requester, use_oidc=(auth_type == "oidc"),
        )
    source = schema_document if isinstance(schema_document, str) else json.dumps(schema_document, sort_keys=True)
    preview = {
        "provider_id": provider_id, "protocol": "graphql",
        "schema_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(), "server_url": endpoint,
        "read_only_candidates": queries, "mutation_candidates": mutations,
        "review_candidates": queries + [x for x in mutations if x.get("supported") is True],
        "warnings": warnings, "registered": False,
        "note": "Inert preview. Only explicitly configured, schema-validated operations are enabled; each mutation requires individual confirmation.",
    }
    return connector, preview


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
    REST OpenAPI and GraphQL previews are both inert. Callers must review the
    declared schema and capabilities, then explicitly register the connector.
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
    protocol = str(config.get("protocol") or "openapi").strip().lower()
    if protocol == "graphql":
        return _configure_graphql_connector(config, openapi_spec, provider_id=provider_id, display_name=display_name, auth_type=auth_type, trusted_hosts=trusted_hosts, requester=requester)
    if protocol not in ("openapi", "rest", "rest_openapi"):
        raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "Choose a supported protocol: openapi/rest or graphql.")
    preview = analyze_openapi_spec(
        openapi_spec,
        provider_id=provider_id,
        trusted_server_hosts=trusted_hosts,
        max_operations=int(config.get("max_operations", _MAX_OPERATIONS)),
    )
    operations = operations_from_openapi(preview)
    if not operations:
        raise IntegrationError(IntegrationErrorCode.UNSUPPORTED_ACTION, "The reviewed API specification contains no supported, authenticated operations for this provider.")
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
            cached = config.get("oidc_metadata")
            if isinstance(cached, Mapping):
                cached_issuer = str(cached.get("issuer") or "")
                if cached_issuer != issuer:
                    raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "Cached OIDC metadata issuer does not match the configured issuer.")
                parsed_issuer = _check_https_url(issuer, allowed_hosts=trusted_hosts)
                if parsed_issuer.query or parsed_issuer.fragment:
                    raise IntegrationError(IntegrationErrorCode.INVALID_REQUEST, "OIDC issuer identifiers may not contain a query or fragment.")
                raw_scopes_supported = cached.get("scopes_supported", [])
                raw_algorithms = cached.get("id_token_signing_alg_values_supported", [])
                algorithms = tuple(dict.fromkeys(x for x in raw_algorithms if isinstance(x, str) and x in ("RS256", "ES256"))) if isinstance(raw_algorithms, list) else ()
                scopes_supported = tuple(dict.fromkeys(x for x in raw_scopes_supported if isinstance(x, str) and x.strip())) if isinstance(raw_scopes_supported, list) else ()
                metadata = OAuthProviderMetadata(
                    issuer=cached_issuer,
                    authorization_endpoint=str(cached.get("authorization_endpoint") or ""),
                    token_endpoint=str(cached.get("token_endpoint") or ""),
                    userinfo_endpoint=str(cached.get("userinfo_endpoint") or ""),
                    jwks_uri=str(cached.get("jwks_uri") or ""),
                    revocation_endpoint=str(cached.get("revocation_endpoint") or ""),
                    scopes_supported=scopes_supported,
                    id_token_signing_alg_values_supported=algorithms,
                )
                required_endpoints = (metadata.authorization_endpoint, metadata.token_endpoint, metadata.userinfo_endpoint, metadata.jwks_uri)
                if not all(required_endpoints) or not metadata.id_token_signing_alg_values_supported:
                    raise IntegrationError(IntegrationErrorCode.INVALID_AUTH_RESPONSE, "Cached OIDC metadata is incomplete or advertises no supported signing algorithm.")
                for endpoint in required_endpoints + ((metadata.revocation_endpoint,) if metadata.revocation_endpoint else ()):
                    _check_https_url(endpoint, allowed_hosts=trusted_hosts)
            else:
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
