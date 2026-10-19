"""Provider-neutral intelligence and live-voice primitives.

This package is additive; host apps opt into it explicitly.
"""
from .contracts import FunctionTool, ModelReply, ModelRequest, ProviderFailure, Route, RunPolicy, TaskResult, ToolCall
from .engine import ReasoningEngine
from .key_pool import (
    CredentialLoadResult, CredentialPoolProvider, CredentialPoolState, CredentialSpec,
    build_credential_pools, load_credentials_from_environment,
    load_credentials_from_sources, merge_credential_specs,
)
from .providers import AnthropicMessagesProvider, CallableProvider, GeminiContentProvider, OpenAICompatibleProvider, OpenAIResponsesProvider
from .voice import BoundedPlaybackBuffer, GPTLiveVoiceAdapter, LiveDelegationCoordinator, OpenAIRealtimeVoiceAdapter, VoicePolicy

__all__ = [
    "AnthropicMessagesProvider", "BoundedPlaybackBuffer", "CallableProvider",
    "CredentialLoadResult", "CredentialPoolProvider", "CredentialPoolState", "CredentialSpec",
    "FunctionTool", "GeminiContentProvider", "GPTLiveVoiceAdapter", "LiveDelegationCoordinator",
    "ModelReply", "ModelRequest", "OpenAICompatibleProvider",
    "OpenAIRealtimeVoiceAdapter", "OpenAIResponsesProvider", "ProviderFailure",
    "ReasoningEngine", "Route", "RunPolicy", "TaskResult", "ToolCall", "VoicePolicy",
    "build_credential_pools", "load_credentials_from_environment",
    "load_credentials_from_sources", "merge_credential_specs",
]
