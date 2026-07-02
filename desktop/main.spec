# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for sub2cli desktop (.app bundle).

Run from desktop/ via build.sh:
    ./build.sh

Outputs:
    dist/sub2cli      — onedir bundle (executable + _internal/)
    dist/sub2cli.app  — macOS .app wrapping the onedir bundle

Codesign + notarize are out-of-scope for the spec; do them after the
.app is produced (build.sh prints the recommended commands).
"""
import os

# Spec runs with cwd=desktop/, so '..' is repo root.
REPO_ROOT = os.path.abspath('..')

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        # The sub2cli script (no .py extension) — loaded via SourceFileLoader.
        # Put in pyscripts/ subdir to avoid name collision with the 'sub2cli' EXE.
        (os.path.join(REPO_ROOT, 'sub2cli'), 'pyscripts'),
        # Frontend assets
        ('ui', 'ui'),
    ],
    hiddenimports=[
        'webview',
        'webview.platforms.cocoa',
        'keyring',
        'keyring.backends.macOS',
        'requests',
        'websocket',
    ],
    hookspath=[],
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
    name='sub2cli',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    target_arch=None,  # native arch; build.sh sets `arch -arm64` / `-x86_64`
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='sub2cli',
)

app = BUNDLE(
    coll,
    name='sub2cli.app',
    icon='assets/sub2cli.icns',
    bundle_identifier='com.r266-tech.sub2cli',
    info_plist={
        'CFBundleName': 'sub2cli',
        'CFBundleDisplayName': 'sub2cli',
        'CFBundleShortVersionString': '0.2.12',
        'CFBundleVersion': '0.2.12',
        'LSMinimumSystemVersion': '12.0',
        'NSHighResolutionCapable': True,
        'NSRequiresAquaSystemAppearance': False,
        'CFBundleIdentifier': 'com.r266-tech.sub2cli',
        'CFBundleIconFile': 'sub2cli.icns',
        'CFBundleIconName': 'sub2cli',
    },
)
