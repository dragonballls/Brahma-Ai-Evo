import threading
import time

from core.omniroute import OmniRouteGateway


def test_provider_configuration_blocks_concurrent_stop_until_credentials_finish():
    gateway = OmniRouteGateway(base_url="http://127.0.0.1:20128/v1")

    started = threading.Event()
    release = threading.Event()
    stopped = threading.Event()
    configured = threading.Event()

    class FakeProvisioner:
        base_url = "http://127.0.0.1:20128/v1"

        def ensure_running(self, *, wait_seconds):
            return True

        def configure_provider(self, provider, api_key):
            started.set()
            assert release.wait(timeout=2)
            configured.set()
            return {"ok": True, "provider": provider, "configured": True}

        def stop(self):
            stopped.set()

    gateway.provisioner = FakeProvisioner()

    configure_thread = threading.Thread(
        target=lambda: gateway.configure_provider("openrouter", "test-key-123456"),
        daemon=True,
    )
    configure_thread.start()
    assert started.wait(timeout=1)

    stop_thread = threading.Thread(target=gateway.stop, daemon=True)
    stop_thread.start()
    time.sleep(0.05)
    assert not stopped.is_set()

    release.set()
    configure_thread.join(timeout=2)
    stop_thread.join(timeout=2)

    assert configured.is_set()
    assert stopped.is_set()
    assert not configure_thread.is_alive()
    assert not stop_thread.is_alive()
