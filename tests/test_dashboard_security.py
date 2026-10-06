from pathlib import Path


def test_dashboard_firewall_never_reclassifies_public_networks():
    source = (
        Path(__file__).resolve().parents[1] / "dashboard" / "server.py"
    ).read_text(encoding="utf-8")

    assert "Set-NetConnectionProfile -NetworkCategory Private" not in source
    assert "profile=Private" in source
    assert 'localport={port} action=allow profile=Private' in source
    assert 'program="{py_exe}" enable=yes profile=Private' in source


def test_dashboard_crypto_asset_never_falls_back_to_external_cdn():
    source = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
    start = source.index('@app.get("/static/crypto.js")')
    block = source[start:source.index('@app.get("/login")', start)]
    assert "RedirectResponse" not in block
    assert "status_code=503" in block
