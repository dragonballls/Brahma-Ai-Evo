from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ppt_template_fetch_rejects_private_destinations_and_credentials():
    source = (ROOT / "actions" / "ppt_template_workflow.py").read_text(encoding="utf-8")
    assert "def _validate_remote_fetch_url" in source
    assert "address.is_private" in source
    assert "address.is_link_local" in source
    assert "embedded credentials" in source


def test_ppt_template_fetch_disables_redirects_and_caps_response_size():
    source = (ROOT / "actions" / "ppt_template_workflow.py").read_text(encoding="utf-8")
    assert "MAX_HTTP_RESPONSE_BYTES = 25 * 1024 * 1024" in source
    assert 'kwargs["allow_redirects"] = False' in source
    assert "Remote template response exceeds the 25 MiB safety limit." in source
    assert "fetch_public_bytes(" in source


def test_ppt_template_safe_get_uses_pinned_transport_and_preserves_response_contract(monkeypatch):
    import actions.ppt_template_workflow as workflow

    captured = {}

    monkeypatch.setattr(
        workflow,
        "_validate_remote_fetch_url",
        lambda url: url,
    )

    def fake_fetch(url, *, timeout, max_response_bytes, headers):
        captured["url"] = url
        captured["timeout"] = timeout
        captured["max_response_bytes"] = max_response_bytes
        captured["headers"] = headers
        return 200, b"hello"

    monkeypatch.setattr(workflow, "fetch_public_bytes", fake_fetch)
    response = workflow._safe_get(
        "https://example.com/template.pptx",
        params={"q": "hello world"},
        headers={"User-Agent": "test"},
        timeout=3,
        stream=True,
        allow_redirects=False,
    )

    assert response.status_code == 200
    assert response.text == "hello"
    assert "q=hello+world" in response.url
    assert captured["timeout"] == 3.0
    assert captured["max_response_bytes"] == workflow.MAX_HTTP_RESPONSE_BYTES
    assert captured["headers"] == {"User-Agent": "test"}


def test_ppt_template_safe_get_rejects_redirect_responses(monkeypatch):
    import actions.ppt_template_workflow as workflow
    monkeypatch.setattr(workflow, "_validate_remote_fetch_url", lambda url: url)
    monkeypatch.setattr(
        workflow,
        "fetch_public_bytes",
        lambda *args, **kwargs: (302, b"redirect"),
    )

    import pytest
    with pytest.raises(ValueError, match="redirected"):
        workflow._safe_get("https://example.com/template.pptx", timeout=3)
