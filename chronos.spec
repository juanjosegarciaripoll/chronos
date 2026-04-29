# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


ROOT = Path(__file__).resolve().parent
datas = [
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "CHANGELOG.md"), "."),
    (str(ROOT / "config-sample.toml"), "."),
    (str(ROOT / "README.md"), "."),
]

block_cipher = None

a = Analysis(
    [str(ROOT / "src" / "chronos" / "__main__.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="chronos",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="chronos",
)
