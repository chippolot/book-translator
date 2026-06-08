# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Book Translator.

Build via build/build_mac.sh, NOT directly. The shell script passes
`--target-arch universal2` to pyinstaller so both Intel and Apple
Silicon Macs are covered by one bundle.
"""

import os
from pathlib import Path

import PyInstaller.config

# The spec is invoked with the repo root as CWD by build_mac.sh.
ROOT = Path(".").resolve()
SRC = ROOT / "src"

# Target architecture is driven by the TARGET_ARCH env var so build_mac.sh
# can flip it. PyInstaller rejects --target-arch on the command line when
# a .spec file is used, so we read it here. Default `None` means
# "whatever the running pyinstaller process arch is", which is what we want
# when running PyInstaller via `arch -<target> pyinstaller`.
TARGET_ARCH = os.environ.get("TARGET_ARCH") or None


block_cipher = None


a = Analysis(
    [str(ROOT / "run_gui.py")],
    pathex=[str(SRC)],  # so `import gui`, `import config`, etc. resolve
    binaries=[],
    datas=[
        # The _resource() helper in src/gui/app.py looks under <MEIPASS>/gui,
        # so these targets must match that layout.
        (str(SRC / "gui" / "resources" / "book.svg"), "gui/resources"),
        (str(SRC / "gui" / "style.qss"), "gui"),
    ],
    hiddenimports=[
        # Provider SDKs lazy-import inside src/providers.py — PyInstaller's
        # static analysis misses them.
        "anthropic",
        "google.genai",
        "openai",
        # Keyring's macOS backend imports dynamically through a registry.
        "keyring.backends.macOS",
        "keyring.backends.fail",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Trim default-included modules we don't use.
        "tkinter",
        "unittest",
        "pytest",
        "test",
        "tests",
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
    name=os.environ.get("APP_NAME", "Book Translator"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=TARGET_ARCH,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "build" / "book.icns"),
)

_app_name = os.environ.get("APP_NAME", "Book Translator")

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=_app_name,
)

app = BUNDLE(
    coll,
    name=f"{_app_name}.app",
    icon=str(ROOT / "build" / "book.icns"),
    bundle_identifier="com.bensmith.booktranslator",
    info_plist={
        "CFBundleName": "Book Translator",
        "CFBundleDisplayName": "Book Translator",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
        # Permission strings: macOS will deny access silently if these
        # aren't present. The app uses file pickers (no special perms) and
        # writes only inside the user-chosen output directory.
        "NSDocumentsFolderUsageDescription":
            "Book Translator reads input PDFs and writes translated outputs.",
        "NSDownloadsFolderUsageDescription":
            "Book Translator reads input PDFs you've downloaded.",
        # Ad-hoc-signed apps need this to access Keychain without a prompt
        # loop on every launch.
        "NSAppleEventsUsageDescription":
            "Book Translator uses Apple events to open the output folder.",
    },
)
