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
    assert "requests.get(safe_url, **kwargs)" in source
