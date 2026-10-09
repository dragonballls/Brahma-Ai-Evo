import os
import urllib.request
from pathlib import Path
import core.network_safety as network_safety
import dashboard.server as dashboard_server


def _set_missing_asset(monkeypatch, tmp_path: Path) -> Path:
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    target = static_dir / "crypto-js.min.js"
    monkeypatch.setattr(dashboard_server, "_CRYPTOJS_FILE", target)
    return target


def _install_fake_network(monkeypatch, payload=b"/* deterministic test bundle */"):
    calls = {"validate": [], "download": [], "urlretrieve": []}
    def validate(url, *, allowed_hosts):
        calls["validate"].append((url, set(allowed_hosts)))
        return url
    def download(url, output, **kwargs):
        calls["download"].append((url, dict(kwargs)))
        output.write(payload)
        return len(payload)
    def legacy_urlretrieve(url, filename, *args, **kwargs):
        calls["urlretrieve"].append((url, dict(kwargs)))
        Path(filename).write_bytes(payload)
        return filename, None
    monkeypatch.setattr(network_safety, "validate_fixed_https_url", validate)
    monkeypatch.setattr(network_safety, "download_public_to_file", download)
    monkeypatch.setattr(urllib.request, "urlretrieve", legacy_urlretrieve)
    return calls


def test_existing_bundled_crypto_asset_never_uses_network(tmp_path, monkeypatch):
    target = _set_missing_asset(monkeypatch, tmp_path)
    target.write_bytes(b"bundled asset")
    calls = _install_fake_network(monkeypatch)
    dashboard_server._ensure_crypto_js()
    assert target.read_bytes() == b"bundled asset"
    assert not calls["validate"], "existing bundle caused URL validation/network work"
    assert not calls["download"], "existing bundle caused a download"
    assert not calls["urlretrieve"], "legacy urlretrieve path was used"


def test_missing_asset_uses_fixed_host_bounded_download_and_atomic_replace(tmp_path, monkeypatch):
    target = _set_missing_asset(monkeypatch, tmp_path)
    payload = b"/* deterministic test bundle */"
    calls = _install_fake_network(monkeypatch, payload)
    dashboard_server._ensure_crypto_js()
    assert target.read_bytes() == payload, "fallback did not install downloaded bytes"
    assert len(calls["validate"]) == 1, "fixed HTTPS destination was not validated"
    assert calls["validate"][0][1] == {"cdnjs.cloudflare.com"}, "unexpected allowed destination"
    assert len(calls["download"]) == 1, "bounded download helper was not used"
    kwargs = calls["download"][0][1]
    assert kwargs.get("timeout") == dashboard_server._CRYPTOJS_TIMEOUT_SECONDS, "download timeout missing"
    assert kwargs.get("max_response_bytes") == dashboard_server._CRYPTOJS_MAX_BYTES, "response cap missing"
    assert kwargs.get("require_https") is True, "HTTPS was not required"
    assert kwargs.get("allowed_redirect_hosts") == (), "redirects were not disabled"
    assert kwargs.get("max_redirects") == 0, "redirect limit was not zero"
    assert not calls["urlretrieve"], "unsafe urlretrieve path was used"
    assert list(target.parent.glob(".crypto-js-*.tmp")) == [], "temporary file was not cleaned up"


def test_timeout_download_leaves_no_partial_asset(tmp_path, monkeypatch, capsys):
    target = _set_missing_asset(monkeypatch, tmp_path)
    calls = _install_fake_network(monkeypatch)
    def fail_download(url, output, **kwargs):
        calls["download"].append((url, dict(kwargs)))
        output.write(b"partial")
        raise TimeoutError("synthetic failure")
    monkeypatch.setattr(network_safety, "download_public_to_file", fail_download)
    dashboard_server._ensure_crypto_js()
    assert not target.exists(), "partial asset was installed"
    assert list(target.parent.glob(".crypto-js-*.tmp")) == [], "failed download left temporary file"
    assert "synthetic failure" not in capsys.readouterr().out, "exception detail leaked to logs"
    assert not calls["urlretrieve"], "unsafe urlretrieve path was used"


def test_oversized_response_leaves_no_partial_asset(tmp_path, monkeypatch):
    target = _set_missing_asset(monkeypatch, tmp_path)
    calls = _install_fake_network(monkeypatch)
    def oversized(url, output, **kwargs):
        calls["download"].append((url, dict(kwargs)))
        assert kwargs.get("max_response_bytes") == dashboard_server._CRYPTOJS_MAX_BYTES
        output.write(b"partial")
        raise ValueError("oversized response")
    monkeypatch.setattr(network_safety, "download_public_to_file", oversized)
    dashboard_server._ensure_crypto_js()
    assert not target.exists(), "oversized asset was installed"
    assert list(target.parent.glob(".crypto-js-*.tmp")) == [], "oversize error left temporary file"
    assert not calls["urlretrieve"], "unsafe urlretrieve path was used"


def test_disallowed_fixed_destination_fails_closed_before_download(tmp_path, monkeypatch):
    target = _set_missing_asset(monkeypatch, tmp_path)
    calls = _install_fake_network(monkeypatch)
    def reject_url(url, *, allowed_hosts):
        calls["validate"].append((url, set(allowed_hosts)))
        raise ValueError("destination denied")
    monkeypatch.setattr(network_safety, "validate_fixed_https_url", reject_url)
    dashboard_server._ensure_crypto_js()
    assert not target.exists(), "disallowed destination produced an asset"
    assert not calls["download"], "download started before validation"
    assert not calls["urlretrieve"], "unsafe urlretrieve path was used"


def test_redirect_response_is_rejected_and_cannot_replace_asset(tmp_path, monkeypatch):
    target = _set_missing_asset(monkeypatch, tmp_path)
    calls = _install_fake_network(monkeypatch)
    def redirect_rejected(url, output, **kwargs):
        calls["download"].append((url, dict(kwargs)))
        assert kwargs.get("allowed_redirect_hosts") == (), "redirect allowlist was not empty"
        assert kwargs.get("max_redirects") == 0, "redirects were not disabled"
        output.write(b"partial redirected content")
        raise ValueError("redirect denied by network policy")
    monkeypatch.setattr(network_safety, "download_public_to_file", redirect_rejected)
    dashboard_server._ensure_crypto_js()
    assert not target.exists(), "redirected content was installed"
    assert list(target.parent.glob(".crypto-js-*.tmp")) == [], "redirect rejection left temporary file"
    assert not calls["urlretrieve"], "unsafe urlretrieve path was used"


def test_atomic_replace_failure_cleans_temp_and_allows_retry(tmp_path, monkeypatch, capsys):
    target = _set_missing_asset(monkeypatch, tmp_path)
    calls = _install_fake_network(monkeypatch)
    real_replace = os.replace
    def fail_replace(source, destination):
        if Path(destination) == target:
            raise OSError("synthetic replacement failure")
        return real_replace(source, destination)
    monkeypatch.setattr(os, "replace", fail_replace)
    dashboard_server._ensure_crypto_js()
    assert not target.exists(), "asset appeared despite failed atomic replacement"
    assert list(target.parent.glob(".crypto-js-*.tmp")) == [], "replace failure left temporary file"
    monkeypatch.setattr(os, "replace", real_replace)
    dashboard_server._ensure_crypto_js()
    assert target.is_file(), "retry did not recover after atomic replacement failure"
    assert len(calls["download"]) == 2, "retry did not make one additional download"
    assert not calls["urlretrieve"], "unsafe urlretrieve path was used"
    assert "synthetic replacement failure" not in capsys.readouterr().out, "exception detail leaked to logs"
