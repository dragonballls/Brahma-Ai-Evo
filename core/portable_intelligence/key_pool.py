"""Credential rotation and shared quota accounting for portable provider pools.

Secrets are supplied by the host or environment at process start. This module never
writes credentials to disk, logs key values, or treats duplicate API keys as extra quota.
Providers such as Gemini/OpenRouter often enforce quotas above the individual-key level;
callers must assign the same quota_group to credentials that share that quota.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from .contracts import ModelReply, ModelRequest, ProviderFailure
from .providers import (
    AnthropicMessagesProvider,
    GeminiContentProvider,
    OpenAICompatibleProvider,
    OpenAIResponsesProvider,
    validate_base_url,
)

_PROVIDER_DEFAULTS = {
    "openai_responses": ("https://api.openai.com/v1", OpenAIResponsesProvider),
    "openai_compatible": ("http://127.0.0.1:20128/v1", OpenAICompatibleProvider),
    "anthropic_messages": ("https://api.anthropic.com/v1", AnthropicMessagesProvider),
    "gemini_generate_content": ("https://generativelanguage.googleapis.com/v1beta", GeminiContentProvider),
}


def _normalize_endpoint(value: str) -> str:
    checked = validate_base_url(value)
    parsed = urlsplit(checked)
    return f"{parsed.scheme.casefold()}://{(parsed.hostname or '').casefold()}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}{parsed.path.rstrip('/')}"


@dataclass(frozen=True)
class CredentialSpec:
    """One upstream credential. api_key is deliberately absent from repr/comparison."""
    credential_id: str
    provider_family: str
    api_key: str = field(repr=False, compare=False)
    quota_group: str = ""
    base_url: str = ""
    priority: int = 100
    source_label: str = "host"
    enabled: bool = True

    def __post_init__(self) -> None:
        credential_id = str(self.credential_id or "").strip()
        family = str(self.provider_family or "").strip().casefold()
        if not credential_id:
            raise ValueError("credential_id is required.")
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", family):
            raise ValueError("provider_family must be a short lowercase identifier.")
        if family not in _PROVIDER_DEFAULTS and not str(self.base_url or "").strip():
            raise ValueError("Custom provider families must configure an explicit HTTPS base_url.")
        if not str(self.api_key or "").strip():
            raise ValueError("Credential API key must not be empty.")
        if not str(self.quota_group or "").strip():
            raise ValueError(
                "quota_group is required. Use the same group for keys that share a provider project/account quota."
            )
        if self.priority < 0:
            raise ValueError("Credential priority cannot be negative.")
        default_url = _PROVIDER_DEFAULTS[family][0] if family in _PROVIDER_DEFAULTS else self.base_url
        normalized = validate_base_url(self.base_url or default_url)
        object.__setattr__(self, "credential_id", credential_id)
        object.__setattr__(self, "provider_family", family)
        object.__setattr__(self, "api_key", str(self.api_key).strip())
        object.__setattr__(self, "quota_group", str(self.quota_group).strip())
        object.__setattr__(self, "base_url", normalized)
        object.__setattr__(self, "source_label", str(self.source_label or "host").strip()[:80])

    @property
    def fingerprint(self) -> str:
        """Non-secret key fingerprint used only to identify duplicate credentials."""
        payload = f"{self.provider_family}\0{_normalize_endpoint(self.base_url)}\0{self.api_key}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:20]


@dataclass(frozen=True)
class CredentialLoadResult:
    credentials: tuple[CredentialSpec, ...]
    skipped: tuple[Mapping[str, str], ...]


def load_credentials_from_environment(
    rows: Sequence[Mapping[str, Any]],
    *,
    environ: Mapping[str, str] | None = None,
) -> CredentialLoadResult:
    """Load an explicit public config against environment variables; never log secret values.

    A row contains: id, provider_family, api_key_env, quota_group, and optionally
    base_url, priority, source_label, enabled. Missing API-key env vars are reported
    by variable name only and skipped, not replaced with fake or blank keys.
    """
    env = os.environ if environ is None else environ
    specs: list[CredentialSpec] = []
    skipped: list[Mapping[str, str]] = []
    seen_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("Each credential entry must be an object.")
        credential_id = str(row.get("id") or "").strip()
        env_name = str(row.get("api_key_env") or "").strip()
        if not credential_id or credential_id in seen_ids:
            raise ValueError("Credential IDs must be non-empty and unique.")
        seen_ids.add(credential_id)
        if row.get("enabled", True) is False:
            skipped.append({"credential_id": credential_id, "reason": "disabled"})
            continue
        if not env_name or any(ch.isspace() for ch in env_name):
            raise ValueError(f"Credential {credential_id} needs a valid api_key_env variable name.")
        secret = str(env.get(env_name, "") or "").strip()
        if not secret:
            skipped.append({
                "credential_id": credential_id,
                "reason": "environment variable missing or empty",
                "api_key_env": env_name,
            })
            continue
        specs.append(CredentialSpec(
            credential_id=credential_id,
            provider_family=str(row.get("provider_family") or ""),
            api_key=secret,
            quota_group=str(row.get("quota_group") or ""),
            base_url=str(row.get("base_url") or ""),
            priority=int(row.get("priority", 100)),
            source_label=str(row.get("source_label") or "host"),
        ))
    return CredentialLoadResult(tuple(specs), tuple(skipped))


@dataclass
class _CredentialState:
    credential: CredentialSpec
    adapter: Any = None
    disabled: bool = False
    cooldown_until: float = 0.0
    consecutive_failures: int = 0
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    last_category: str = ""
    duplicate_aliases: int = 0


class CredentialPoolState:
    """Share quota-group cooldowns and concurrency state across provider pools in one process."""
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.scope_cooldown_until: dict[str, float] = {}
        self.scope_blocked_reason: dict[str, str] = {}
        self.scope_in_flight: dict[str, int] = {}
        self.credential_cooldown_until: dict[str, float] = {}
        self.max_parallel_by_scope: dict[str, int] = {}
        self.scope_metrics: dict[str, dict[str, int]] = {}

    def reset_scope(self, quota_group: str) -> None:
        """Operator action after a confirmed provider-side quota/billing issue is fixed."""
        with self.lock:
            self.scope_blocked_reason.pop(quota_group, None)
            self.scope_cooldown_until.pop(quota_group, None)


class CredentialPoolProvider:
    """ProviderAdapter that rotates keys safely without pretending keys create quota.

    Build one pool for one provider family and endpoint. For credentials that share an
    account/project quota, set a common quota_group. Use a shared CredentialPoolState
    when multiple host integrations/pools need to honor the same quota scopes.
    """
    def __init__(
        self,
        provider_id: str,
        credentials: Sequence[CredentialSpec],
        *,
        state: CredentialPoolState | None = None,
        adapter_factory: Callable[[CredentialSpec], Any] | None = None,
        diversity_group: str | None = None,
        max_parallel_per_scope: int = 2,
        default_rate_limit_cooldown_seconds: float = 60.0,
        transient_cooldown_seconds: float = 2.0,
        auth_disable: bool = True,
        capabilities: Sequence[str] | None = None,
    ) -> None:
        if not str(provider_id or "").strip():
            raise ValueError("provider_id is required.")
        if not credentials:
            raise ValueError("At least one enabled credential is required.")
        if max_parallel_per_scope < 1 or default_rate_limit_cooldown_seconds <= 0 or transient_cooldown_seconds < 0:
            raise ValueError("Invalid pool concurrency/cooldown settings.")
        family_endpoints = {(c.provider_family, _normalize_endpoint(c.base_url)) for c in credentials}
        if len(family_endpoints) != 1:
            raise ValueError("A pool must contain one provider family and endpoint. Create separate pools otherwise.")
        family, _endpoint = next(iter(family_endpoints))
        selected_factory = adapter_factory
        if selected_factory is None and family not in _PROVIDER_DEFAULTS:
            raise ValueError(f"Custom provider family {family!r} requires an adapter_factory.")
        ordered = sorted((c for c in credentials if c.enabled), key=lambda c: (c.priority, c.credential_id))
        if not ordered:
            raise ValueError("At least one enabled credential is required.")
        deduped: list[CredentialSpec] = []
        by_fingerprint: dict[str, CredentialSpec] = {}
        duplicate_counts: dict[str, int] = {}
        for credential in ordered:
            prior = by_fingerprint.get(credential.fingerprint)
            if prior is not None:
                if prior.quota_group != credential.quota_group:
                    raise ValueError(
                        "The same key/endpoint was configured with conflicting quota_group values; use one shared quota group."
                    )
                duplicate_counts[prior.fingerprint] = duplicate_counts.get(prior.fingerprint, 0) + 1
                continue
            by_fingerprint[credential.fingerprint] = credential
            deduped.append(credential)
        self.provider_id = str(provider_id).strip()
        self.family = family
        self.base_url = deduped[0].base_url
        self.diversity_group = str(diversity_group or family).strip()
        default_capabilities = frozenset({"text", "tools", "vision"}) if family in _PROVIDER_DEFAULTS else frozenset({"text"})
        selected_capabilities = frozenset(capabilities) if capabilities is not None else default_capabilities
        if "text" not in selected_capabilities or not selected_capabilities.issubset({"text", "tools", "vision"}):
            raise ValueError("Provider pool capabilities must include text and may only include tools/vision explicitly.")
        self.capabilities = selected_capabilities
        self.state = state or CredentialPoolState()
        self.max_parallel_per_scope = max_parallel_per_scope
        self.default_rate_limit_cooldown_seconds = default_rate_limit_cooldown_seconds
        self.transient_cooldown_seconds = transient_cooldown_seconds
        self.auth_disable = auth_disable
        self._factory = selected_factory or self._make_builtin_adapter
        self._credentials = [
            _CredentialState(c, duplicate_aliases=duplicate_counts.get(c.fingerprint, 0))
            for c in deduped
        ]
        self._cursor = 0
        self._condition = threading.Condition(self.state.lock)
        with self.state.lock:
            for credential in deduped:
                existing_limit = self.state.max_parallel_by_scope.get(credential.quota_group, max_parallel_per_scope)
                self.state.max_parallel_by_scope[credential.quota_group] = min(existing_limit, max_parallel_per_scope)

    @staticmethod
    def _make_builtin_adapter(credential: CredentialSpec) -> Any:
        default_url, factory = _PROVIDER_DEFAULTS[credential.provider_family]
        kwargs = {"api_key": credential.api_key, "base_url": credential.base_url or default_url}
        if credential.provider_family in {"openai_responses", "anthropic_messages", "gemini_generate_content"}:
            return factory(**kwargs)
        return factory(**kwargs)

    def _pick(self, excluded_fingerprints: set[str] | None = None) -> _CredentialState:
        """Pick an eligible credential not already attempted by this request.

        The excluded set matters under concurrency: other requests can move the
        round-robin cursor between attempts, so the cursor alone cannot guarantee
        that one request will not revisit a credential it already tried.
        """
        now = time.monotonic()
        with self.state.lock:
            total = len(self._credentials)
            for offset in range(total):
                index = (self._cursor + offset) % total
                candidate = self._credentials[index]
                cred = candidate.credential
                if excluded_fingerprints and cred.fingerprint in excluded_fingerprints:
                    continue
                if candidate.disabled or candidate.cooldown_until > now:
                    continue
                if self.state.scope_blocked_reason.get(cred.quota_group):
                    continue
                if self.state.scope_cooldown_until.get(cred.quota_group, 0.0) > now:
                    continue
                shared_limit = self.state.max_parallel_by_scope.get(cred.quota_group, self.max_parallel_per_scope)
                if self.state.scope_in_flight.get(cred.quota_group, 0) >= shared_limit:
                    continue
                self._cursor = (index + 1) % total
                self.state.scope_in_flight[cred.quota_group] = self.state.scope_in_flight.get(cred.quota_group, 0) + 1
                candidate.attempts += 1
                return candidate
        raise ProviderFailure(
            self.provider_id, "credential_pool_unavailable",
            "All configured credentials are disabled, cooling down, or at their shared quota concurrency limit.",
            status_code=429, retryable=True,
        )

    def _release(self, credential: _CredentialState) -> None:
        with self.state.lock:
            scope = credential.credential.quota_group
            self.state.scope_in_flight[scope] = max(0, self.state.scope_in_flight.get(scope, 1) - 1)
            self._condition.notify_all()

    def _adapter_for(self, state: _CredentialState) -> Any:
        with self.state.lock:
            if state.adapter is None:
                state.adapter = self._factory(state.credential)
                # Preserve the host-facing identity; the pool is one logical model backend.
                try:
                    state.adapter.provider_id = self.provider_id
                except Exception:
                    pass
            return state.adapter

    def complete(self, request: ModelRequest, model: str) -> ModelReply:
        attempted: set[str] = set()
        failures: list[ProviderFailure] = []
        for _ in range(len(self._credentials)):
            try:
                selected = self._pick(excluded_fingerprints=attempted)
            except ProviderFailure as exc:
                if failures:
                    break
                raise
            credential = selected.credential
            scope = credential.quota_group
            attempted.add(credential.fingerprint)
            try:
                reply = self._adapter_for(selected).complete(request, model)
                if not isinstance(reply, ModelReply):
                    raise ProviderFailure(self.provider_id, "adapter_contract_violation")
                with self.state.lock:
                    selected.successes += 1
                    selected.consecutive_failures = 0
                    selected.last_category = ""
                    self.state.scope_metrics.setdefault(scope, {}).setdefault("successes", 0)
                    self.state.scope_metrics[scope]["successes"] += 1
                    if reply.input_tokens is not None:
                        selected.input_tokens += max(0, int(reply.input_tokens))
                    if reply.output_tokens is not None:
                        selected.output_tokens += max(0, int(reply.output_tokens))
                metadata = dict(reply.metadata)
                metadata.update({
                    "credential_id": credential.credential_id,
                    "credential_fingerprint": credential.fingerprint,
                    "quota_group": scope,
                    "credential_source": credential.source_label,
                })
                return ModelReply(
                    text=reply.text, provider_id=self.provider_id, model=reply.model or model,
                    tool_calls=reply.tool_calls, input_tokens=reply.input_tokens, output_tokens=reply.output_tokens,
                    response_id=reply.response_id, metadata=metadata,
                )
            except ProviderFailure as exc:
                failures.append(exc)
                with self.state.lock:
                    selected.failures += 1
                    selected.last_category = exc.category
                    selected.consecutive_failures += 1
                    self.state.scope_metrics.setdefault(scope, {}).setdefault("failures", 0)
                    self.state.scope_metrics[scope]["failures"] += 1
                    now = time.monotonic()
                    if exc.category in {"authentication_or_permission"} and self.auth_disable:
                        selected.disabled = True
                    elif exc.category == "credential_limit_exhausted":
                        if exc.retry_after_seconds is None:
                            selected.disabled = True
                        else:
                            selected.cooldown_until = max(
                                selected.cooldown_until, now + exc.retry_after_seconds
                            )
                    elif exc.category == "quota_exhausted":
                        self.state.scope_blocked_reason[scope] = "provider quota/billing requires operator review"
                    elif exc.category == "quota_cooldown":
                        cooldown = exc.retry_after_seconds if exc.retry_after_seconds is not None else self.default_rate_limit_cooldown_seconds
                        self.state.scope_cooldown_until[scope] = max(
                            self.state.scope_cooldown_until.get(scope, 0.0), now + cooldown
                        )
                    elif exc.status_code == 429 or exc.category in {"rate_limited"}:
                        cooldown = (
                            exc.retry_after_seconds
                            if exc.retry_after_seconds is not None
                            else self.default_rate_limit_cooldown_seconds
                        )
                        self.state.scope_cooldown_until[scope] = max(
                            self.state.scope_cooldown_until.get(scope, 0.0), now + cooldown
                        )
                    elif exc.retryable:
                        delay = min(60.0, self.transient_cooldown_seconds * (2 ** min(5, selected.consecutive_failures - 1)))
                        selected.cooldown_until = max(selected.cooldown_until, now + delay)
                    else:
                        raise
            finally:
                self._release(selected)
        if failures:
            last = failures[-1]
            raise ProviderFailure(
                self.provider_id,
                "credential_pool_all_failed",
                f"Pool attempted {len(failures)} credential(s); last failure category was {last.category}.",
                status_code=last.status_code,
                retryable=any(f.retryable for f in failures),
                retry_after_seconds=last.retry_after_seconds,
            ) from last
        raise ProviderFailure(self.provider_id, "credential_pool_unavailable", retryable=True, status_code=429)

    def reset_credential(self, credential_id: str) -> None:
        """Re-enable a credential after correcting its value/permissions."""
        with self.state.lock:
            for item in self._credentials:
                if item.credential.credential_id == credential_id:
                    item.disabled = False
                    item.cooldown_until = 0.0
                    item.consecutive_failures = 0
                    return
        raise KeyError("Unknown credential_id.")

    def snapshot(self) -> dict[str, Any]:
        """Return secret-free in-memory metrics. No API key values are included."""
        now = time.monotonic()
        with self.state.lock:
            credentials = []
            for item in self._credentials:
                c = item.credential
                scope_until = self.state.scope_cooldown_until.get(c.quota_group, 0.0)
                credentials.append({
                    "credential_id": c.credential_id,
                    "fingerprint": c.fingerprint,
                    "provider_family": c.provider_family,
                    "quota_group": c.quota_group,
                    "source_label": c.source_label,
                    "disabled": item.disabled,
                    "cooldown_remaining_seconds": round(max(0.0, item.cooldown_until - now), 3),
                    "shared_cooldown_remaining_seconds": round(max(0.0, scope_until - now), 3),
                    "scope_blocked": bool(self.state.scope_blocked_reason.get(c.quota_group)),
                    "last_category": item.last_category,
                    "attempts": item.attempts,
                    "successes": item.successes,
                    "failures": item.failures,
                    "input_tokens": item.input_tokens,
                    "output_tokens": item.output_tokens,
                    "duplicate_aliases_collapsed": item.duplicate_aliases,
                })
            return {
                "provider_id": self.provider_id,
                "diversity_group": self.diversity_group,
                "provider_family": self.family,
                "credential_count": len(credentials),
                "disabled_count": sum(1 for c in credentials if c["disabled"]),
                "credentials": credentials,
                "quota_scopes": {
                    scope: {
                        "blocked": bool(self.state.scope_blocked_reason.get(scope)),
                        "cooldown_remaining_seconds": round(max(0.0, self.state.scope_cooldown_until.get(scope, 0.0) - now), 3),
                        "in_flight": self.state.scope_in_flight.get(scope, 0),
                        "metrics": dict(self.state.scope_metrics.get(scope, {})),
                    }
                    for scope in sorted({s.credential.quota_group for s in self._credentials})
                },
            }


def build_credential_pools(
    credentials: Sequence[CredentialSpec],
    *,
    state: CredentialPoolState | None = None,
    adapter_factory: Callable[[CredentialSpec], Any] | None = None,
    adapter_factories: Mapping[str, Callable[[CredentialSpec], Any]] | None = None,
    capabilities_by_family: Mapping[str, Sequence[str]] | None = None,
    max_parallel_per_scope: int = 2,
    diversity_groups: Mapping[str, str] | None = None,
) -> dict[str, CredentialPoolProvider]:
    """Group a merged set of host/provider credential entries into stable endpoint pools.

    Feed entries from every source the host is allowed to use (for example, direct
    provider settings from a desktop app and provider-specific entries managed by a
    local gateway). Identical provider/endpoint/key tuples collapse into one credential.
    Opaque gateway client keys are not upstream provider keys and should instead be
    represented as one openai_compatible gateway pool.
    """
    groups: dict[tuple[str, str], list[CredentialSpec]] = {}
    for credential in credentials:
        if not credential.enabled:
            continue
        key = (credential.provider_family, _normalize_endpoint(credential.base_url))
        groups.setdefault(key, []).append(credential)
    shared_state = state or CredentialPoolState()
    result: dict[str, CredentialPoolProvider] = {}
    for (family, endpoint), group_credentials in sorted(groups.items()):
        suffix = hashlib.sha256(f"{family}|{endpoint}".encode("utf-8")).hexdigest()[:8]
        provider_id = f"{family}_{suffix}"
        if provider_id in result:
            raise ValueError("Provider-pool ID collision.")
        family_factory = (adapter_factories or {}).get(family, adapter_factory)
        result[provider_id] = CredentialPoolProvider(
            provider_id,
            group_credentials,
            state=shared_state,
            adapter_factory=family_factory,
            capabilities=(capabilities_by_family or {}).get(family),
            diversity_group=(diversity_groups or {}).get(provider_id, f"{family}:{endpoint}"),
            max_parallel_per_scope=max_parallel_per_scope,
        )
    return result


def merge_credential_specs(
    sources: Mapping[str, Sequence[CredentialSpec]],
) -> tuple[CredentialSpec, ...]:
    """Merge already-loaded credentials from multiple host integrations in memory.

    Each source can adapt an existing settings/key store (for example Abacus, an
    OmniRoute gateway config, or direct provider settings) to CredentialSpec. IDs
    are namespaced by source, but identical actual key/provider/endpoint values
    remain detectable by build_credential_pools(). This does not discover or open
    another app's private config; the host must explicitly supply its authorized
    credential objects. Keys never enter logs or generated source files.
    """
    merged: list[CredentialSpec] = []
    for source_name, specs in sorted(sources.items(), key=lambda item: str(item[0]).casefold()):
        label = str(source_name or "").strip()
        if not label:
            raise ValueError("Credential source labels must be non-empty.")
        for spec in specs:
            if not isinstance(spec, CredentialSpec):
                raise TypeError("Credential sources must provide CredentialSpec objects.")
            merged.append(CredentialSpec(
                credential_id=f"{label}:{spec.credential_id}",
                provider_family=spec.provider_family,
                api_key=spec.api_key,
                quota_group=spec.quota_group,
                base_url=spec.base_url,
                priority=spec.priority,
                source_label=label,
                enabled=spec.enabled,
            ))
    return tuple(merged)


def load_credentials_from_sources(
    sources: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    environ: Mapping[str, str] | None = None,
) -> CredentialLoadResult:
    """Load any number of rows from multiple public configs via environment keys."""
    rows: list[dict[str, Any]] = []
    for source_name, source_rows in sorted(sources.items(), key=lambda item: str(item[0]).casefold()):
        label = str(source_name or "").strip()
        if not label:
            raise ValueError("Credential source labels must be non-empty.")
        for row in source_rows:
            if not isinstance(row, Mapping):
                raise ValueError("Each source credential row must be an object.")
            entry = dict(row)
            original_id = str(entry.get("id") or "").strip()
            if not original_id:
                raise ValueError(f"Credential from {label} has no ID.")
            entry["id"] = f"{label}:{original_id}"
            entry["source_label"] = label
            rows.append(entry)
    return load_credentials_from_environment(rows, environ=environ)
