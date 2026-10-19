"""Provider-neutral intelligence and live-voice primitives.

This package is additive: importing it does not change Brahma's existing request paths.
The host application must opt into it explicitly.
"""
from .contracts import FunctionTool, ModelReply, ModelRequest, ProviderFailure, Route, RunPolicy, TaskResult, ToolCall
from .engine import ReasoningEngine
from .providers import AnthropicMessagesProvider, GeminiContentProvider, OpenAICompatibleProvider, OpenAIResponsesProvider

__all__ = [
    "AnthropicMessagesProvider", "FunctionTool", "GeminiContentProvider",
    "ModelReply", "ModelRequest", "OpenAICompatibleProvider",
    "OpenAIResponsesProvider", "ProviderFailure", "ReasoningEngine",
    "Route", "RunPolicy", "TaskResult", "ToolCall",
]
