# -*- mode: python ; coding: utf-8 -*-
import os
import sys

# PyInstaller executes .spec files as code without defining __file__. Resolve
# the repository from the invocation directory, with a safe parent fallback
# for callers that invoke PyInstaller from the installer directory.
cwd = os.path.abspath(os.getcwd())
if not os.path.exists(os.path.join(cwd, 'core')):
    parent = os.path.abspath(os.path.join(cwd, '..'))
    if os.path.exists(os.path.join(parent, 'core')):
        cwd = parent

a = Analysis(
    [os.path.join(cwd, 'core', 'process_supervisor.py')],
    pathex=[cwd],
    binaries=[],
    datas=[
        (os.path.join(cwd, 'core', 'crash_recovery.py'), 'core'),
        (os.path.join(cwd, 'core', 'runtime_paths.py'), 'core'),
        (os.path.join(cwd, 'core', 'user_paths.py'), 'core'),
        (os.path.join(cwd, 'core', 'single_instance.py'), 'core'),
        (os.path.join(cwd, 'memory'), 'memory'),
        (os.path.join(cwd, 'config'), 'config'),
    ],
    hiddenimports=['core.crash_recovery', 'actions.auto_heal_engine'],
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
    name='BrahmaEvoSupervisor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(cwd, 'assets', 'Brahma_Lite_Logo.ico') if os.path.exists(os.path.join(cwd, 'assets', 'Brahma_Lite_Logo.ico')) else None,
)
