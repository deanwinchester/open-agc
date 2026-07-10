# -*- mode: python ; coding: utf-8 -*-
"""
Open-AGC PyInstaller Spec File
Packages the Python backend + static frontend into a single app bundle.
Only bundles essential files — runtime data is created in ~/.open-agc on first launch.
"""
import os
import glob
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

import sys ; sys.setrecursionlimit(sys.getrecursionlimit() * 5)

block_cipher = None

# Collect data files from packages that need them at runtime
# include_py_files=True is needed for importlib.resources to work in Python 3.9
litellm_datas = collect_data_files('litellm', include_py_files=True)
openai_datas = collect_data_files('openai', include_py_files=False)
litellm_submodules = collect_submodules('litellm')
tiktoken_datas = collect_data_files('tiktoken')
tiktoken_submodules = collect_submodules('tiktoken')
httpx_submodules = collect_submodules('httpx')
httpcore_submodules = collect_submodules('httpcore')
anyio_submodules = collect_submodules('anyio')
aiohttp_submodules = collect_submodules('aiohttp')

# ---- Collect data files selectively ----
datas = [
    # Static frontend (required)
    ('static', 'static'),
    # Skills (bundled defaults)
    ('skills', 'skills'),
    # Python source packages (needed as data since we import dynamically)
    ('agent', 'agent'),
    ('core', 'core'),
    ('tools', 'tools'),
    ('api', 'api'),
]

# Bundle build_data/ as data/ (only contains API-key-free config template).
# Real data/config.json with user API keys is NEVER bundled.
if os.path.exists('build_data'):
    datas.append(('build_data', 'data'))

# Do NOT bundle data/ directory as it may contain sensitive user data (API keys).
# The initial config template is provided via build_data/ above.

# Add .env.example
if os.path.exists('.env.example'):
    datas.append(('.env.example', '.'))

# VERSION file (required by core/version.py)
if os.path.exists('VERSION'):
    datas.append(('VERSION', '.'))

# Merge package data files
datas += litellm_datas
datas += openai_datas
datas += tiktoken_datas

a = Analysis(
    ['gui_app.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[
        # Uvicorn
        'uvicorn',
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        # FastAPI / Starlette
        'fastapi',
        'starlette',
        'starlette.routing',
        'starlette.middleware',
        'starlette.responses',
        'starlette.staticfiles',
        'starlette.websockets',
        # LLM / AI
        'litellm',
        'pydantic',
        'dotenv',
        'rich',
        'duckduckgo_search',
        'requests',
        'bs4',
        # Server extras
        'httptools',
        'websockets',
        # App modules
        'api.server',
        'agent.agent',
        'core.llm_client',

        'tools.shell',
        'tools.filesystem',
        'tools.python_repl',
        'tools.computer',
        'tools.memory',
        'tools.web_search',
        'tools.system_mac',
        'webview',
        'webview.platforms.cocoa',
        'webview.platforms.winforms',
    ] + litellm_submodules + tiktoken_submodules + httpx_submodules + httpcore_submodules + anyio_submodules + aiohttp_submodules + ['tiktoken_ext.openai_public'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Qt/PySide (pulled by pywebview but we use WinForms/Cocoa, not Qt)
        'PySide6', 'PySide2', 'PyQt5', 'PyQt6',
        'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtPositioning', 'PySide6.QtWebEngine',
        'PySide6.QtWebEngineWidgets', 'PySide6.QtWebChannel',
        'PySide6.QtNetwork', 'PySide6.QtSensors', 'PySide6.QtSvg',
        'PySide6.QtMultimedia', 'PySide6.QtPrintSupport', 'PySide6.QtXml',
        'shiboken6',
        # Heavy ML frameworks (not needed for agent/server)
        'modelscope',
        'tkinter',
        'matplotlib',
        'numpy',
        'scipy',
        'pandas',
        # Training dependencies (handled by plugin)
        'torch',
        'transformers',
        'peft',
        'accelerate',
        'datasets',
        'bitsandbytes',
        'sentencepiece',
        'sklearn',
        'scikit-learn',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# Determine target architecture
import platform
target_arch = os.environ.get('TARGET_ARCH', platform.machine())

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Open-AGC',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    target_arch=target_arch,
    icon='static/icon.ico' if os.path.exists('static/icon.ico') else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Open-AGC',
)

# macOS .app bundle
app = BUNDLE(
    coll,
    name='Open-AGC.app',
    icon='static/icon.icns' if os.path.exists('static/icon.icns') else None,
    bundle_identifier='com.openagc.panda',
    info_plist={
        'CFBundleName': 'Open-AGC',
        'CFBundleDisplayName': 'Open-AGC Panda',
        'CFBundleShortVersionString': '1.0.0',
        'CFBundleVersion': '1.0.0',
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '10.15.0',
    },
)
