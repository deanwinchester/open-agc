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

# Bundle the API-key-free default config as data/config.json so first launch
# has something to seed from (seeded to ~/.open-agc/data/config.json).
# Real data/config.json with user API keys is NEVER bundled.
if os.path.exists('build_data/config.json'):
    datas.append(('build_data/config.json', 'data'))

# Do NOT bundle data/ directory as it may contain sensitive user data (API keys).

# Add .env.example
if os.path.exists('.env.example'):
    datas.append(('.env.example', '.'))

# VERSION file (required by core/version.py)
if os.path.exists('VERSION'):
    datas.append(('VERSION', '.'))

# Windows: 包内嵌 WebView2 fixed-version 运行时（edgechromium 用，支持文件
# 拖放与 Ctrl+C/V；目标机无需预装 WebView2 运行时）。由 build 脚本先调用
# scripts/download_webview2_runtime.py 下载解包到 build/webview2_runtime/。
if os.path.isdir('build/webview2_runtime'):
    datas.append(('build/webview2_runtime', 'webview2_runtime'))

# Merge package data files
datas += litellm_datas
datas += openai_datas
datas += tiktoken_datas

# ---- Linux: pywebview GTK backend (WebKit2 / JavaScriptCore typelibs) ----
# PyInstaller 自带 hook-gi.repository.Gtk 收集 Gtk/Gdk/Gio/GLib/GObject，
# 但没有 WebKit2/JavaScriptCore 的 hook，这里用 GiModuleInfo 手动收集。
# 需要构建环境安装 gir1.2-webkit2-4.0（typelib 在容器/目标机均为数据文件，
# 与 Python 版本无关）；未安装时 GiModuleInfo.available=False，安全跳过。
gi_binaries = []
gi_hiddenimports = []
try:
    from PyInstaller.utils.hooks.gi import GiModuleInfo
    for _gir_name, _gir_ver in [('WebKit2', '4.0'), ('JavaScriptCore', '4.0')]:
        try:
            _info = GiModuleInfo(_gir_name, _gir_ver)
            if _info.available:
                _b, _d, _h = _info.collect_typelib_data()
                gi_binaries += _b
                datas += _d
                gi_hiddenimports += _h
        except Exception:
            pass
except Exception:
    pass

a = Analysis(
    ['gui_app.py'],
    pathex=[],
    binaries=gi_binaries,
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
        # Linux GTK 后端（此前缺失，Linux 下 pywebview 找不到 GTK 后端）
        'webview.platforms.gtk',
    ] + litellm_submodules + tiktoken_submodules + httpx_submodules + httpcore_submodules + anyio_submodules + aiohttp_submodules + gi_hiddenimports + ['tiktoken_ext.openai_public'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Qt/PySide (pulled by pywebview but we use WinForms/Cocoa, not Qt)
        'PySide6', 'PySide2', 'PyQt5', 'PyQt6',
        'shiboken6',
        # Heavy ML frameworks (litellm pulls them but we don't need them)
        'tensorboardX', 'tensorboard',
        'modelscope', 'vllm',
        'gradio', 'spaces',
        'moviepy', 'imageio', 'imageio_ffmpeg',
        'onnxruntime', 'cpuinfo',
        'numba', 'llvmlite',
        'librosa', 'audioread', 'soxr', 'pooch',
        'kaldiio',
        'einops',
        'safetensors',
        'antlr4', 'omegaconf',
        'jieba',
        'oss2', 'aliyunsdkcore', 'aliyunsdkkms', 'crcmod',
        'Crypto', 'jmespath',
        'pyarrow',
        'fsspec',
        'pydub',
        'sentry_sdk',
        'prometheus_client', 'prometheus_fastapi_instrumentator',
        'grpc', 'grpc_reflection',
        # 注意：opentelemetry 不能排除——chromadb（memory_store/embedding 打分）
        # 的 import 链会 import 它，开发机上它只是 litellm/chromadb 的传递依赖
        # 所以无感，冻结包里一旦排除，全新机器启动即崩（生产实证）。
        'tkinter',
        'matplotlib', 'mpl_toolkits',
        'scipy',
        'pandas',
        'torch', 'torchvision', 'torchaudio', 'torchcodec',
        'transformers',
        'peft',
        'accelerate',
        'datasets',
        'bitsandbytes',
        'sentencepiece',
        'sklearn', 'scikit-learn',
        # Not needed for packaging
        'pip',
        'unittest', 'pytest', 'nose', 'doctest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# ---- Linux: GTK/GNOME 系统库不打进包 ----
# PyInstaller 的 gi hook 会把 libgtk/libglib/libwebkit2gtk 等系统库收进
# _internal，bootloader 又把它设为 LD_LIBRARY_PATH 优先加载——GTK 因此
# 加载不到系统输入法模块（im-fcitx 等 immodules），窗口内无法切中文
# （UOS ARM64 实测）。deb Depends 已保证目标机系统装有这些库，打包时
# 剔除，运行时回退系统库，行为与系统 Python 直跑一致。
if sys.platform.startswith('linux'):
    _SYS_LIB_PREFIXES = (
        'libgtk-3', 'libgdk-3', 'libgdk_pixbuf', 'libglib-2.0',
        'libgobject-2.0', 'libgio-2.0', 'libgmodule-2.0', 'libgthread-2.0',
        'libgirepository-1.0', 'libwebkit2gtk-4.0', 'libjavascriptcoregtk-4.0',
        'libpango', 'libcairo', 'libatk', 'libharfbuzz', 'libsoup',
        'libepoxy',
    )

    def _is_gnome_sys_lib(entry):
        # TOC 条目为 (name, path, typecode)
        names = [os.path.basename(str(entry[0])).lower()]
        if len(entry) > 1 and entry[1]:
            names.append(os.path.basename(str(entry[1])).lower())
        return any(
            n.startswith(p)
            for n in names
            for p in _SYS_LIB_PREFIXES
        )

    a.binaries = [b for b in a.binaries if not _is_gnome_sys_lib(b)]

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
