from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_settings_dispatch_uses_boundary_aware_alias_and_provider_matching():
    source = (ROOT / "core" / "settings_agent.py").read_text(encoding="utf-8")
    assert "def _contains_term" in source
    assert "_contains_term(clean, alias)" in source
    assert "_contains_term(lower, alias)" in source
    assert "_contains_term(lower, phrase)" in source
    assert 'if spec.key.replace("_", " ") in lower' not in source
