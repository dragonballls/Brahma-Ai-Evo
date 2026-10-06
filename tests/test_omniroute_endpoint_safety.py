from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_omniroute_uses_the_canonical_local_endpoint_validator():
    setup = (ROOT / "core" / "omniroute_setup.py").read_text(encoding="utf-8")
    gateway = (ROOT / "core" / "omniroute.py").read_text(encoding="utf-8")
    assert "validate_local_endpoint" in setup
    assert "validate_local_endpoint" in gateway
    assert "configured_base_url" in gateway


def test_omniroute_probe_rejects_redirects():
    setup = (ROOT / "core" / "omniroute_setup.py").read_text(encoding="utf-8")
    assert "class _NoRedirect(HTTPRedirectHandler)" in setup
    assert "Redirects are not permitted for OmniRoute probes." in setup
    assert "urllib.request.build_opener(_NoRedirect)" in setup
