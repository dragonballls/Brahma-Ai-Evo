import pytest

from core.local_endpoint import validate_local_endpoint


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:11434/v1",
        "http://127.0.0.1:11434/v1",
        "http://192.168.1.20:1234/v1",
        "http://[::1]:11434/v1",
    ],
)
def test_local_endpoint_accepts_local_targets(endpoint):
    assert validate_local_endpoint(endpoint) == endpoint.rstrip("/")


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.com/v1",
        "http://8.8.8.8:11434/v1",
        "http://user:pass@127.0.0.1:11434/v1",
    ],
)
def test_local_endpoint_rejects_public_or_credentialed_targets(endpoint):
    with pytest.raises(ValueError):
        validate_local_endpoint(endpoint)
