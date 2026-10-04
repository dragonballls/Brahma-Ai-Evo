# -*- mode: python ; coding: utf-8 -*-
import os

cwd = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

a = Analysis(
    [os.path.join(cwd, 'scripts', 'recovery_supervisor.py')],
    pathex=[cwd],
    binaries=[],
    datas=[],
    hiddenimports=[
        'actions.auto_heal_engine',
        'llm_client',
        'config',
        'core.boot_sentry',
        'core.runtime_paths',
    ],
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
    name='BrahmaEvo_Supervisor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
