import urllib.error

def test_live_cricket_network_failure_is_not_fabricated(monkeypatch):
    from features import live_cricket_scores as cricket

    def fail(*_args, **_kwargs):
        raise OSError("network down")

    monkeypatch.setattr(cricket.urllib.request, "urlopen", fail)
    result = cricket.execute()
    assert "error" in result
    assert result["matches"] == []
    assert "India 285/5" not in str(result)
    assert "England 198/4" not in str(result)


def test_live_cricket_oversized_response_is_rejected(monkeypatch):
    from features import live_cricket_scores as cricket

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self, n):
            return b"x" * n

    class Opener:
        def open(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(cricket.urllib.request, "build_opener", lambda *_args: Opener())
    result = cricket.execute()
    assert "512 KiB safety limit" in result["error"]


def test_live_cricket_redirects_are_rejected_before_following(monkeypatch):
    from features import live_cricket_scores as cricket

    class Opener:
        def open(self, *_args, **_kwargs):
            raise urllib.error.URLError("redirect blocked")

    monkeypatch.setattr(cricket.urllib.request, "build_opener", lambda *_args: Opener())
    result = cricket.execute()
    assert "unavailable" in result["error"].lower()
