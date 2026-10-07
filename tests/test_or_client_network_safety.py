from __future__ import annotations

import or_client


class FakeResponse:
    def __init__(self, chunks):
        self._chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size):
        assert chunk_size == or_client._RESPONSE_CHUNK_BYTES
        yield from self._chunks

    def close(self):
        self.closed = True


def test_llm_response_reader_caps_streamed_body():
    response = FakeResponse([b"x" * (or_client.MAX_LLM_RESPONSE_BYTES + 1)])

    try:
        or_client._read_bounded_json(response)
    except ValueError as exc:
        assert "2 MiB safety limit" in str(exc)
    else:
        raise AssertionError("oversized LLM response must be rejected")
    assert response.closed


def test_llm_response_reader_rejects_non_object_json():
    response = FakeResponse([b"[]"])

    try:
        or_client._read_bounded_json(response)
    except ValueError as exc:
        assert "must be an object" in str(exc)
    else:
        raise AssertionError("non-object JSON must be rejected")
    assert response.closed


def test_openrouter_direct_request_uses_streaming_and_closes_on_error(monkeypatch):
    response = FakeResponse([b'{"error":"bad"}'], status=500)

    monkeypatch.setattr(or_client.requests, "post", lambda *args, **kwargs: response)

    client = or_client.OpenRouterClient()
    monkeypatch.setattr(client, "_request_headers", lambda: {"Authorization": "Bearer test"})
    monkeypatch.setattr(client, "_is_rate_limited", lambda _model: False)
    monkeypatch.setattr(client, "_is_temporarily_failed", lambda _model: False)

    result = client._call(
        "model/test",
        [{"role": "user", "content": "x"}],
        max_tokens=1,
    )

    assert result is None
    assert response.closed
