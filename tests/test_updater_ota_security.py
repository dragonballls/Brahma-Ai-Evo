from __future__ import annotations

import hashlib
from pathlib import Path

import pytest


def _release(url: str, payload: bytes) -> dict:
    return {
        "assets": [
            {
                "name": "BrahmaEvo_Setup_Update.exe",
                "browser_download_url": url,
                "size": len(payload),
                "digest": f"sha256:{hashlib.sha256(payload).hexdigest()}",
            }
        ]
    }


def test_ota_success_launches_unique_verified_temp_installer(monkeypatch, tmp_path):
    from core import updater_ota

    url = "https://release-assets.githubusercontent.com/update.exe"
    payload = b"MZ" + b"x" * 98
    release = _release(url, payload)
    launched = {}
    progress = []

    monkeypatch.setattr(updater_ota, "_get_release", lambda: release)
    monkeypatch.setattr(updater_ota, "get_current_version", lambda: "1.0.0")
    import core.user_paths as user_paths
    monkeypatch.setattr(user_paths, "get_user_data_dir", lambda: tmp_path / "BrahmaAI")

    def fake_download(download_url, output, **kwargs):
        launched["download_url"] = download_url
        launched["download_kwargs"] = kwargs
        output.write(payload[:50])
        output.write(payload[50:])
        return len(payload)

    monkeypatch.setattr(updater_ota, "download_public_to_file", fake_download)

    def fake_popen(argv, **kwargs):
        launched["argv"] = argv
        launched["popen_kwargs"] = kwargs

    monkeypatch.setattr(updater_ota.subprocess, "Popen", fake_popen)

    with pytest.raises(SystemExit) as exc:
        updater_ota.download_and_apply_update(
            url,
            ui_callback=progress.append,
            expected_sha256=release["assets"][0]["digest"].split(":", 1)[1],
            expected_size=len(payload),
        )

    assert exc.value.code == 0
    assert launched["download_url"] == url
    assert launched["download_kwargs"]["require_https"] is True
    assert launched["download_kwargs"]["max_response_bytes"] == updater_ota._MAX_INSTALLER_BYTES
    assert progress[-1] == 100
    launch_path = Path(launched["argv"][0])
    assert launch_path.name != "BrahmaEvo_Setup_Update.exe"
    assert launch_path.name.startswith("BrahmaEvo_Update_")
    assert launch_path.suffix == ".download"
    assert launched["argv"][1] == "--silent"
    assert launched["popen_kwargs"]["creationflags"] == (
        int(getattr(updater_ota.subprocess, "CREATE_NO_WINDOW", 0))
        | int(getattr(updater_ota.subprocess, "DETACHED_PROCESS", 0x00000008))
    )


def test_ota_rejects_caller_size_that_disagrees_with_current_release(monkeypatch, tmp_path):
    from core import updater_ota
    url = "https://release-assets.githubusercontent.com/update.exe"
    payload = b"MZpayload"
    release = _release(url, payload)
    called = {"download": False, "popen": False}

    monkeypatch.setattr(updater_ota, "_get_release", lambda: release)
    import core.user_paths as user_paths
    monkeypatch.setattr(user_paths, "get_user_data_dir", lambda: tmp_path / "BrahmaAI")
    monkeypatch.setattr(
        updater_ota,
        "download_public_to_file",
        lambda *args, **kwargs: called.__setitem__("download", True),
    )
    monkeypatch.setattr(
        updater_ota.subprocess,
        "Popen",
        lambda *args, **kwargs: called.__setitem__("popen", True),
    )

    assert updater_ota.download_and_apply_update(url, expected_size=len(payload) + 1) is False
    assert called == {"download": False, "popen": False}


def test_ota_checksum_mismatch_cleans_partial_installer_and_never_launches(monkeypatch, tmp_path):
    from core import updater_ota
    url = "https://release-assets.githubusercontent.com/update.exe"
    expected_payload = b"MZ" + b"trusted"
    actual_payload = b"MZ" + b"tampered"
    release = _release(url, expected_payload)
    calls = {"popen": 0}

    monkeypatch.setattr(updater_ota, "_get_release", lambda: release)
    import core.user_paths as user_paths
    monkeypatch.setattr(user_paths, "get_user_data_dir", lambda: tmp_path / "BrahmaAI")

    def fake_download(download_url, output, **kwargs):
        output.write(actual_payload)
        return len(actual_payload)

    monkeypatch.setattr(updater_ota, "download_public_to_file", fake_download)
    monkeypatch.setattr(
        updater_ota.subprocess,
        "Popen",
        lambda *args, **kwargs: calls.__setitem__("popen", calls["popen"] + 1),
    )

    assert updater_ota.download_and_apply_update(url) is False
    update_dir = tmp_path / "BrahmaAI" / "updates"
    assert not list(update_dir.glob("*"))
    assert calls["popen"] == 0


def test_ota_rejects_url_not_present_in_current_release(monkeypatch, tmp_path):
    from core import updater_ota
    payload = b"MZpayload"
    release = _release("https://release-assets.githubusercontent.com/current.exe", payload)

    monkeypatch.setattr(updater_ota, "_get_release", lambda: release)
    import core.user_paths as user_paths
    monkeypatch.setattr(user_paths, "get_user_data_dir", lambda: tmp_path / "BrahmaAI")

    assert updater_ota.download_and_apply_update(
        "https://release-assets.githubusercontent.com/other.exe",
    ) is False
    assert not (tmp_path / "BrahmaAI" / "updates").exists()


def test_ota_popen_failure_cleans_temp_file(monkeypatch, tmp_path):
    from core import updater_ota
    url = "https://release-assets.githubusercontent.com/update.exe"
    payload = b"MZ" + b"verified"
    release = _release(url, payload)

    monkeypatch.setattr(updater_ota, "_get_release", lambda: release)
    import core.user_paths as user_paths
    monkeypatch.setattr(user_paths, "get_user_data_dir", lambda: tmp_path / "BrahmaAI")

    monkeypatch.setattr(
        updater_ota,
        "download_public_to_file",
        lambda _url, output, **_kwargs: (output.write(payload) or len(payload)),
    )
    monkeypatch.setattr(
        updater_ota.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("launch failed")),
    )

    assert updater_ota.download_and_apply_update(url) is False
    assert not list((tmp_path / "BrahmaAI" / "updates").glob("*"))
