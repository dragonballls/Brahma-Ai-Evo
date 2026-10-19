"""Provider-neutral intelligence and live-voice primitives.

This package is additive; host apps opt into it explicitly.
"""
from .contracts import FunctionTool, ModelReply, ModelRequest, ProviderFailure, Route, RunPolicy, TaskResult, ToolCall
from .engine import ReasoningEngine
from .providers import AnthropicMessagesProvider, CallableProvider, GeminiContentProvider, OpenAICompatibleProvider, OpenAIResponsesProvider
from .voice import BoundedPlaybackBuffer, GPTLiveVoiceAdapter, LiveDelegationCoordinator, OpenAIRealtimeVoiceAdapter, VoicePolicy

__all__ = [
    "AnthropicMessagesProvider", "BoundedPlaybackBuffer", "CallableProvider",
    "FunctionTool", "GeminiContentProvider", "GPTLiveVoiceAdapter", "LiveDelegationCoordinator",
    "ModelReply", "ModelRequest", "OpenAICompatibleProvider",
    "OpenAIRealtimeVoiceAdapter", "OpenAIResponsesProvider", "ProviderFailure",
    "ReasoningEngine", "Route", "RunPolicy", "TaskResult", "ToolCall", "VoicePolicy",
]
