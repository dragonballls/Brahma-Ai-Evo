from pathlib import Path


def test_dashboard_firewall_never_reclassifies_public_networks():
    source = (
        Path(__file__).resolve().parents[1] / "dashboard" / "server.py"
    ).read_text(encoding="utf-8")

    assert "Set-NetConnectionProfile -NetworkCategory Private" not in source
    assert "profile=Private" in source
    assert 'localport={port} action=allow profile=Private' in source
    assert 'program="{py_exe}" enable=yes profile=Private' in source
