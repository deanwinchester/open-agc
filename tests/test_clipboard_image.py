# -*- coding: utf-8 -*-
"""剪贴板图片接口测试：Linux WebKitGTK 粘贴兜底通道。"""
import base64

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import routes_system


def _client():
    app = FastAPI()
    app.include_router(routes_system.router)
    return TestClient(app)


def test_404_when_no_image(monkeypatch):
    """xclip 不存在 + GTK 取不到 → 404（前端静默走默认粘贴）。"""
    monkeypatch.setattr(routes_system.shutil, "which", lambda _: None) \
        if hasattr(routes_system, "shutil") else None
    import shutil as _sh
    monkeypatch.setattr(_sh, "which", lambda _: None)
    monkeypatch.setattr(routes_system, "_gtk_clipboard_png", lambda timeout=5.0: None)
    r = _client().get("/api/system/clipboard-image")
    assert r.status_code == 404


def test_xclip_returns_image(monkeypatch):
    """xclip 可用时返回 dataURL。"""
    import shutil as _sh
    import subprocess as _sp
    png = b"\x89PNG\r\n\x1a\nfake"
    monkeypatch.setattr(_sh, "which", lambda cmd: "/usr/bin/xclip" if cmd == "xclip" else None)

    class R:
        returncode = 0
        stdout = png

    monkeypatch.setattr(_sp, "run", lambda *a, **k: R())
    r = _client().get("/api/system/clipboard-image")
    assert r.status_code == 200
    assert r.json()["image"] == "data:image/png;base64," + base64.b64encode(png).decode()


def test_gtk_fallback(monkeypatch):
    """xclip 不存在时走 GTK 剪贴板兜底。"""
    import shutil as _sh
    png = b"\x89PNGgtk"
    monkeypatch.setattr(_sh, "which", lambda _: None)
    monkeypatch.setattr(routes_system, "_gtk_clipboard_png", lambda timeout=5.0: png)
    r = _client().get("/api/system/clipboard-image")
    assert r.status_code == 200
    assert r.json()["image"].endswith(base64.b64encode(png).decode())
