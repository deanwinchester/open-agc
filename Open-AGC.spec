# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules

datas = [('static', 'static'), ('data', 'data'), ('skills', 'skills'), ('agent', 'agent'), ('core', 'core'), ('tools', 'tools'), ('api', 'api')]
hiddenimports = ['uvicorn', 'uvicorn.logging', 'uvicorn.loops', 'uvicorn.loops.auto', 'uvicorn.protocols', 'uvicorn.protocols.http', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.websockets', 'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan', 'uvicorn.lifespan.on', 'fastapi', 'starlette', 'starlette.routing', 'starlette.middleware', 'starlette.responses', 'starlette.staticfiles', 'starlette.websockets', 'litellm', 'pydantic', 'dotenv', 'rich', 'duckduckgo_search', 'requests', 'bs4', 'httptools', 'websockets', 'api.server', 'agent.agent', 'core.llm_client', 'tools.shell', 'tools.filesystem', 'tools.python_repl', 'tools.computer', 'tools.memory', 'tools.web_search', 'tools.system_mac', 'webview', 'webview.platforms.winforms']
datas += collect_data_files('litellm')
datas += collect_data_files('openai')
hiddenimports += collect_submodules('litellm')


a = Analysis(
    ['gui_app.py'],
    pathex=[],
    binaries=[],
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
