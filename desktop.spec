# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the ImageGenerater desktop app (Windows, one-dir).

Build with:
    pyinstaller --clean --noconfirm desktop.spec

Produces dist/ImageGenerater/ with ImageGenerater.exe at its root. The
one-dir output is then wrapped into a single setup.exe by the NSIS script.
"""

from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

# Optional app icon; drop a .ico next to this file to use it.
import os

_icon = "app_icon.ico" if os.path.exists("app_icon.ico") else None

# --- Collect static assets that must ship inside the exe --------------------
datas = [("templates", "templates"), ("static", "static")]
binaries = []
hiddenimports = []

# pywebview (WebView2/winforms) + its .NET (pythonnet/clr) runtime.
for _pkg in ("webview", "clr", "pythonnet", "clr_loader"):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

# uvicorn loads its protocol/loop/lifespan implementations dynamically.
hiddenimports += collect_submodules("uvicorn")

a = Analysis(
    ["desktop.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "unittest",
        "pytest",
        "mypy",
        "ruff",
        "setuptools",
        "pip",
    ],
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
    name="ImageGenerater",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # windowed app; no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ImageGenerater",
)
