import pytest

def test_fixed_https_network_helper_rejects_non_global_resolution(monkeypatch):
    from core import network_safety

    monkeypatch.setattr(network_safety.socket, "getaddrinfo", lambda *a, **k: [
        (2, 1, 6, "", ("192.168.1.8", 443))
    ])
    with pytest.raises(ValueError, match="non-global"):
        network_safety.validate_fixed_https_url(
            "https://example.com/data",
            {"example.com"},
        )


def test_fixed_https_network_helper_rejects_credentials_and_scheme():
    from core.network_safety import validate_fixed_https_url

    with pytest.raises(ValueError):
        validate_fixed_https_url("http://example.com/data", {"example.com"})
    with pytest.raises(ValueError):
        validate_fixed_https_url("https://user:pass@example.com/data", {"example.com"})
