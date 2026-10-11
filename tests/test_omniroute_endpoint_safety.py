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

def test_omniroute_runtime_forces_loopback_bind(monkeypatch):
    from core.omniroute_setup import OmniRouteProvisioner

    # Even a broad bind request inherited from the environment must not expose
    # the credential-bearing local gateway to other devices on the network.
    monkeypatch.setenv("OMNIROUTE_SERVER_HOST", "0.0.0.0")
    provisioner = OmniRouteProvisioner()
    assert provisioner.environment()["OMNIROUTE_SERVER_HOST"] == "127.0.0.1"

def test_omniroute_reads_abacus_shared_key_and_prefers_explicit_override(monkeypatch, tmp_path):
    from core.omniroute_setup import OmniRouteProvisioner

    shared_key = "abacus-shared-client-key-123456"
    monkeypatch.delenv("BRAHMA_OMNIROUTE_API_KEY", raising=False)
    monkeypatch.setenv("BRAHMA_OMNIROUTE_DATA_DIR", str(tmp_path))
    key_file = tmp_path / "abacusai-bot-client-api-key"
    key_file.write_text(shared_key + "\n", encoding="utf-8")

    provisioner = OmniRouteProvisioner()
    assert provisioner.client_api_key() == shared_key

    override = "explicit-brahma-client-key-654321"
    monkeypatch.setenv("BRAHMA_OMNIROUTE_API_KEY", override)
    assert provisioner.client_api_key() == override


def test_omniroute_probe_authenticates_with_shared_key_file(monkeypatch, tmp_path):
    import urllib.request

    from core.omniroute_setup import OmniRouteProvisioner

    shared_key = "abacus-shared-client-key-123456"
    monkeypatch.delenv("BRAHMA_OMNIROUTE_API_KEY", raising=False)
    monkeypatch.setenv("BRAHMA_OMNIROUTE_DATA_DIR", str(tmp_path))
    (tmp_path / "abacusai-bot-client-api-key").write_text(shared_key, encoding="utf-8")

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"data": []}'

    class FakeOpener:
        def open(self, request, timeout):
            authorization = request.get_header("Authorization") or request.headers.get("Authorization")
            assert authorization == f"Bearer {shared_key}"
            assert timeout > 0
            return FakeResponse()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_args: FakeOpener())
    provisioner = OmniRouteProvisioner()
    assert provisioner.probe_only()

