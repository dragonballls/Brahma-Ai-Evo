# -*- mode: python ; coding: utf-8 -*-
import os
import sys

cwd = os.path.abspath(os.path.join(os.getcwd(), '..'))
if not os.path.exists(os.path.join(cwd, 'installer')):
    cwd = os.path.abspath(os.getcwd()) # fallback

source_dir = os.path.join(cwd, 'dist', 'BrahmaEvo')
supervisor_exe = os.path.join(cwd, 'dist', 'BrahmaEvoSupervisor.exe')
installer_datas = [
    (source_dir, 'BrahmaEvo'),
    (os.path.join(cwd, 'assets'), 'assets'),
]
if os.path.exists(supervisor_exe):
    # Install the independent supervisor beside BrahmaEvo.exe.
    installer_datas.append((supervisor_exe, 'BrahmaEvo'))

a = Analysis(
    [os.path.join(cwd, 'installer', 'install_wizard.py')],
    pathex=[],
    binaries=[],
    datas=installer_datas,
    hiddenimports=['PyQt6', 'shutil', 'PyQt6.QtWebEngineWidgets', 'PyQt6.QtWebEngineCore'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='BrahmaEvo_Setup',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True,
    icon=os.path.join(cwd, 'assets/Brahma_Lite_Logo.ico') if os.path.exists(os.path.join(cwd, 'assets/Brahma_Lite_Logo.ico')) else None,
    version=os.path.join(cwd, 'version_setup.txt') if os.path.exists(os.path.join(cwd, 'version_setup.txt')) else None
)
