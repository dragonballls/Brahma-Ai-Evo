from pathlib import Path
from unittest.mock import Mock

import or_client


def test_normalize_openrouter_key():
    assert or_client.normalize_api_key("  Bearer sk-or-v1-test  ") == "sk-or-v1-test"
    assert or_client.normalize_api_key('"sk-or-v1-test"') == "sk-or-v1-test"


def test_validate_openrouter_key_format():
    assert or_client.validate_api_key_format("sk-or-v1-test")[0] is True
    assert or_client.validate_api_key_format("sk-test")[0] is False
    assert or_client.validate_api_key_format("sk-or-v1 test")[0] is False


def test_save_api_key_preserves_other_credentials(tmp_path, monkeypatch):
    path = tmp_path / "api_keys.json"
    path.write_text(
        '{"gemini_api_key":"gemini-test","openrouter_api_key":"sk-or-v1-old","other":"keep"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(or_client, "API_KEY_PATH", path)

    ok, error = or_client.save_api_key("Bearer sk-or-v1-new")
    assert ok is True
    assert error == ""

    payload = __import__("json").loads(path.read_text(encoding="utf-8"))
    assert payload["openrouter_api_key"] == "sk-or-v1-new"
    assert payload["gemini_api_key"] == "gemini-test"
    assert payload["other"] == "keep"


def test_client_refreshes_key(monkeypatch):
    values = iter(["sk-or-v1-old", "sk-or-v1-new"])
    monkeypatch.setattr(or_client, "_load_api_key", lambda: next(values))
    client = or_client.OpenRouterClient()
    assert client.api_key == "sk-or-v1-old"
    client._refresh_api_key()
    assert client.api_key == "sk-or-v1-new"
    assert client._headers["Authorization"] == "Bearer sk-or-v1-new"


def test_client_test_api_key_uses_key_endpoint(monkeypatch):
    response = Mock()
    response.status_code = 200
    response.content = b'{"data":{"label":"Brahma"}}'
    response.json.return_value = {"data": {"label": "Brahma"}}

    calls = []
    def fake_get(url, headers, timeout):
        calls.append((url, headers, timeout))
        return response

    monkeypatch.setattr(or_client.requests, "get", fake_get)
    client = or_client.OpenRouterClient()
    ok, message, data = client.test_api_key("sk-or-v1-test")

    assert ok is True
    assert "verified" in message.lower()
    assert data["data"]["label"] == "Brahma"
    assert calls[0][0] == "https://openrouter.ai/api/v1/key"
    assert calls[0][1]["Authorization"] == "Bearer sk-or-v1-test"
    assert calls[0][2] == 10


def test_model_pool_puts_free_router_first(monkeypatch):
    client = or_client.OpenRouterClient()
    monkeypatch.setattr(
        client,
        "_get_model_catalog",
        lambda: {
            "openrouter/free": {"id": "openrouter/free"},
            "meta-llama/llama-3.3-70b-instruct:free": {"id": "meta-llama/llama-3.3-70b-instruct:free"},
        },
    )
    monkeypatch.setattr(or_client, "_load_api_key", lambda: "sk-or-v1-test")

    pool = client._model_pool()
    assert pool[0] == "openrouter/free"
    assert "meta-llama/llama-3.3-70b-instruct:free" in pool
