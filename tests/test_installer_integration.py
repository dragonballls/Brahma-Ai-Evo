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
    assert (target / "BrahmaEvo.exe").stat().st_size > 0
    assert (target / "BrahmaEvoSupervisor.exe").stat().st_size > 0

    # Verify the complete installed tree against the payload actually supplied
    # to the installer. The built production payload does not contain the
    # synthetic fixture's assets/runtime.txt, so assert payload fidelity rather
    # than requiring a file that exists only in the unit-test ZIP.
    with zipfile.ZipFile(payload, "r") as archive:
        expected = {
            Path(member.filename).as_posix(): member
            for member in archive.infolist()
            if not member.is_dir()
        }
    installed = {
        path.relative_to(target).as_posix(): path
        for path in target.rglob("*")
        if path.is_file()
    }
    assert set(installed) == set(expected), (
        f"Installed file tree differs from payload: "
        f"missing={sorted(set(expected) - set(installed))[:10]}, "
        f"unexpected={sorted(set(installed) - set(expected))[:10]}"
    )
    for name, member in expected.items():
        installed_path = installed[name]
        assert installed_path.stat().st_size == member.file_size, (
            f"Installed payload file has incorrect size: {name}"
        )

    leftovers = [
        p.name
        for p in target.parent.iterdir()
        if p.name.startswith(f".{target.name}.staging-")
        or p.name.startswith(f".{target.name}.backup-")
    ]
    assert leftovers == []


def test_pyinstaller_hidden_imports_reference_available_module_names():
    import ast

    repository = Path(__file__).resolve().parents[1]
    spec_path = repository / "installer" / "BrahmaEvo.spec"
    tree = ast.parse(spec_path.read_text(encoding="utf-8"))
    hidden_imports = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "Analysis":
            continue
        keyword = next((item for item in node.keywords if item.arg == "hiddenimports"), None)
        if keyword is not None:
            hidden_imports = ast.literal_eval(keyword.value)
            break

    assert isinstance(hidden_imports, list)
    assert "bs4" in hidden_imports  # Code imports BeautifulSoup from the bs4 module.
    for stale_or_distribution_name in ("keyboard", "passlib", "plyer", "beautifulsoup4"):
        assert stale_or_distribution_name not in hidden_imports
