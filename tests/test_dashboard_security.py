from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
    block = source[start:source.index('@app.get("/login"', start)]
    assert "RedirectResponse" not in block
    assert "status_code=503" in block


def test_dashboard_command_cannot_report_empty_command_as_success():
    source = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
    start = source.index('@app.post("/api/command")')
    end = source.index('@app.post("/api/wake")', start)
    block = source[start:end]
    assert 'if not text:' in block
    assert 'Command text is required.' in block
    assert 'return JSONResponse({"ok": True, "status": "queued"})' in block


def test_dashboard_websocket_rejects_non_object_json_without_session_crash():
    source = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
    start = source.index("                    try:\n                        data = json.loads(raw_message)")
    end = source.index('                    if data.get("type") == "command":', start)
    block = source[start:end]
    assert "if not isinstance(data, dict):" in block
    assert "WebSocket message must be a JSON object." in block
