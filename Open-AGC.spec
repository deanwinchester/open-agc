# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import copy_metadata

datas = [('static', 'static'), ('build_data', 'data'), ('skills', 'skills'), ('agent', 'agent'), ('core', 'core'), ('tools', 'tools'), ('api', 'api')]
binaries = []
hiddenimports = ['uvicorn', 'uvicorn.logging', 'uvicorn.loops', 'uvicorn.loops.auto', 'uvicorn.protocols', 'uvicorn.protocols.http', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.websockets', 'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan', 'uvicorn.lifespan.on', 'fastapi', 'starlette', 'starlette.routing', 'starlette.middleware', 'starlette.responses', 'starlette.staticfiles', 'starlette.websockets', 'litellm', 'pydantic', 'dotenv', 'rich', 'duckduckgo_search', 'requests', 'bs4', 'httptools', 'websockets', 'tiktoken', 'tiktoken_ext', 'tiktoken_ext.openai_public', 'vllm', 'vllm.entrypoints.openai.api_server', 'api.server', 'agent.agent', 'core.llm_client', 'core.vllm_manager', 'tools.shell', 'tools.filesystem', 'tools.python_repl', 'tools.computer', 'tools.memory', 'tools.web_search', 'tools.system_mac', 'webview', 'webview.platforms.winforms']
datas += copy_metadata('litellm')
datas += copy_metadata('tiktoken')
datas += copy_metadata('vllm')
hiddenimports += collect_submodules('tiktoken')
hiddenimports += collect_submodules('tiktoken_ext')
hiddenimports += collect_submodules('vllm')
tmp_ret = collect_all('litellm')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('tiktoken')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('openai')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('vllm')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['gui_app.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    [],
    exclude_binaries=True,
    name='Open-AGC',
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
    icon=['static\\icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Open-AGC',
)
