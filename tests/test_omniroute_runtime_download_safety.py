from __future__ import annotations

import scripts.prepare_omniroute_runtime as runtime


def test_runtime_download_rejects_redirects(monkeypatch, tmp_path):
    class FakeOpener:
        def open(self, *_args, **_kwargs):
            raise RuntimeError("redirect rejected")
    monkeypatch.setattr(runtime, "build_opener", lambda *_args: FakeOpener())
    try:
        runtime._download("https://nodejs.org/dist/test.zip", tmp_path / "x", max_bytes=1024)
    except RuntimeError as exc:
        assert "redirect" in str(exc).lower()
    else:
        raise AssertionError("redirected runtime downloads must fail closed")


def test_runtime_download_rejects_unapproved_host(tmp_path):
    try:
        runtime._download("https://example.com/runtime.zip", tmp_path / "x", max_bytes=1024)
    except RuntimeError as exc:
        assert "unapproved host" in str(exc).lower()
    else:
        raise AssertionError("unapproved runtime download hosts must be rejected")


def test_runtime_download_caps_response(monkeypatch, tmp_path):
    class FakeResponse:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self, size):
            return b"x" * size

    class FakeOpener:
        def open(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(runtime, "build_opener", lambda *_args: FakeOpener())
    try:
        runtime._download("https://nodejs.org/dist/test.zip", tmp_path / "x", max_bytes=1024)
    except RuntimeError as exc:
        assert "safety limit" in str(exc)
    else:
        raise AssertionError("oversized runtime downloads must be rejected")
