from __future__ import annotations

import pytest
import llm_client


class FakeResponse:
    def __init__(self, chunks, status=200):
        self._chunks = chunks
        self.status_code = status
        self.closed = False

    def iter_content(self, chunk_size):
        assert chunk_size == llm_client._LOCAL_LLM_RESPONSE_CHUNK_BYTES
        yield from self._chunks

    def close(self):
        self.closed = True


def test_local_llm_response_is_bounded_and_closed():
    response = FakeResponse([b"x" * (llm_client.MAX_LOCAL_LLM_RESPONSE_BYTES + 1)])

    try:
        llm_client._read_bounded_local_json(response)
    except ValueError as exc:
        assert "2 MiB safety limit" in str(exc)
    else:
        raise AssertionError("oversized local LLM response must be rejected")
    assert response.closed


def test_local_llm_redirects_are_not_accepted(monkeypatch):
    response = FakeResponse([], status=302)

    calls = {}

    def fake_post(*args, **kwargs):
        calls.update(kwargs)
        return response

    monkeypatch.setattr(llm_client.requests, "post", fake_post)

    client = llm_client.UnifiedAIClient.__new__(llm_client.UnifiedAIClient)
    client._local_url = "http://127.0.0.1:20128/v1"
    client._local_model = "test-model"

    result = client._local_chat_completion([{"role": "user", "content": "hello"}])

    assert result is None
    assert calls["allow_redirects"] is False
    assert calls["stream"] is True
    assert response.closed


def test_local_chat_complete_rejects_non_object_json(monkeypatch):
    from core import local_brain

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
        def read(self):
            return b"[]"

    monkeypatch.setattr(local_brain.urllib.request, "urlopen", lambda *a, **k: Response())
    with pytest.raises(ValueError, match="JSON object"):
        local_brain.LocalBrain().chat_complete([{"role": "user", "content": "x"}])


def test_unified_local_chat_json_rejects_non_object_json(monkeypatch):
    import llm_client

    client = llm_client.UnifiedAIClient()
    client._provider = "Local"
    monkeypatch.setattr(
        client,
        "_local_chat_completion",
        lambda *args, **kwargs: "[]",
    )
    with pytest.raises(ValueError, match="must be an object"):
        client.chat_json("return an object")
