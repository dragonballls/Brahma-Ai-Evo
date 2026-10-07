import pytest


def test_life360_rejects_oversized_home_assistant_response(monkeypatch):
    from core.selected_capabilities import Life360Provider

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, n=-1):
            assert n == 64 * 1024 + 1
            return b"x" * (64 * 1024 + 1)

    class _Opener:
        def open(self, request, timeout=None):
            return _Response()

    provider = Life360Provider(
        enabled=True,
        base_url="http://127.0.0.1:8123",
        token="secret",
        entity_ids=["device_tracker.alice"],
    )
    monkeypatch.setattr(
        "core.selected_capabilities.urlrequest.build_opener",
        lambda *_handlers: _Opener(),
    )

    with pytest.raises(ValueError, match="exceeded the safety limit"):
        provider._request_json("/api/states")
