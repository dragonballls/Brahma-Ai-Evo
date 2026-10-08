from __future__ import annotations

import os
import zipfile
from pathlib import Path


def _fake_shell(root: Path):
    class Shortcut:
        Targetpath = ""
        WorkingDirectory = ""
        IconLocation = ""
        WindowStyle = 0

        def save(self):
            return None

    class Shell:
        def __init__(self):
            self.special_folders = {
                "Desktop": root / "Desktop",
                "Programs": root / "Programs",
                "Startup": root / "Startup",
            }
            for path in self.special_folders.values():
                path.mkdir(parents=True, exist_ok=True)

        def SpecialFolders(self, name):
            return str(self.special_folders[name])

        def CreateShortCut(self, _path):
            return Shortcut()

    return Shell()


def _payload_for_test(path: Path) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("BrahmaEvo.exe", b"synthetic-app")
        archive.writestr("BrahmaEvoSupervisor.exe", b"synthetic-supervisor")
        archive.writestr("assets/runtime.txt", b"runtime")
    return path


def test_install_thread_performs_end_to_end_payload_activation(tmp_path, monkeypatch):
    from installer import install_wizard

    supplied_payload = os.environ.get("BRAHMA_CI_INSTALLER_PAYLOAD")
    payload = (
        Path(supplied_payload)
        if supplied_payload
        else _payload_for_test(tmp_path / "fixture-payload.zip")
    )
    assert payload.is_file()

    target = tmp_path / "Brahma_Evo"
    errors = []
    monkeypatch.setattr(
        install_wizard.win32com.client,
        "Dispatch",
        lambda _name: _fake_shell(tmp_path),
    )

    thread = install_wizard.InstallThread(None, target, payload)
    thread.error.connect(errors.append)
    thread.run()

    assert errors == [], errors
    assert (target / "BrahmaEvo.exe").is_file()
    assert (target / "BrahmaEvoSupervisor.exe").is_file()
    assert (target / "assets" / "runtime.txt").is_file()
    assert (target / "BrahmaEvo.exe").stat().st_size > 0
    assert (target / "BrahmaEvoSupervisor.exe").stat().st_size > 0

    leftovers = [
        p.name
        for p in target.parent.iterdir()
        if p.name.startswith(f".{target.name}.staging-")
        or p.name.startswith(f".{target.name}.backup-")
    ]
    assert leftovers == []
