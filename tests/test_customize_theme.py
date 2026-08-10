# -*- coding: utf-8 -*-
"""界面风格定制（customize_theme）回归：用户通过会话调整主题色/Logo。
工具写 config.json ui_theme 节 + WS 广播 theme_updated；GET /api/theme
供前端拉取。Logo 只接受 uploads/ 下的文件名（防任意路径引用）。"""
import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tools.theme_tool import CustomizeThemeTool  # noqa: E402


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    store = {"data": {}}
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("core.paths.get_data_path", lambda f: str(cfg_file))
    monkeypatch.setattr("api.config.CONFIG_PATH", str(cfg_file))
    return cfg_file


class TestCustomizeThemeTool:
    def test_set_color(self, cfg):
        out = CustomizeThemeTool().execute(primary_color="#4CAF50")
        assert "✅" in out and "#4CAF50" in out
        saved = json.loads(open(cfg, encoding="utf-8").read())
        assert saved["ui_theme"]["primary_color"] == "#4CAF50"

    def test_color_without_hash_normalized(self, cfg):
        CustomizeThemeTool().execute(primary_color="4CAF50")
        saved = json.loads(open(cfg, encoding="utf-8").read())
        assert saved["ui_theme"]["primary_color"] == "#4CAF50"

    def test_bad_color_rejected(self, cfg):
        out = CustomizeThemeTool().execute(primary_color="green")
        assert "Error" in out
        assert "ui_theme" not in json.loads(open(cfg, encoding="utf-8").read())

    def test_logo_path_safety(self, cfg):
        out = CustomizeThemeTool().execute(logo="../evil.png")
        assert "Error" in out
        out2 = CustomizeThemeTool().execute(logo="some/dir/x.png")
        assert "Error" in out2

    def test_logo_reset(self, cfg):
        CustomizeThemeTool().execute(primary_color="#111111")
        out = CustomizeThemeTool().execute(logo="reset")
        assert "✅" in out
        saved = json.loads(open(cfg, encoding="utf-8").read())
        assert saved["ui_theme"]["logo_file"] == ""
        assert saved["ui_theme"]["primary_color"] == "#111111"  # 共存不互相清

    def test_no_params_error(self, cfg):
        assert "Error" in CustomizeThemeTool().execute()

    def test_page_color(self, cfg):
        out = CustomizeThemeTool().execute(page_color="#1a1a2e")
        assert "✅" in out and "页面底色" in out
        saved = json.loads(open(cfg, encoding="utf-8").read())
        assert saved["ui_theme"]["page_color"] == "#1a1a2e"
        CustomizeThemeTool().execute(page_color="reset")
        saved = json.loads(open(cfg, encoding="utf-8").read())
        assert saved["ui_theme"]["page_color"] == ""

    def test_dark_toggle(self, cfg):
        out = CustomizeThemeTool().execute(dark="on")
        assert "✅" in out and "暗色模式" in out
        saved = json.loads(open(cfg, encoding="utf-8").read())
        assert saved["ui_theme"]["dark"] is True
        CustomizeThemeTool().execute(dark="off")
        saved = json.loads(open(cfg, encoding="utf-8").read())
        assert saved["ui_theme"]["dark"] is False

    def test_sidebar_color_and_bg_image(self, cfg):
        out = CustomizeThemeTool().execute(sidebar_color="#1f2d3d")
        assert "✅" in out
        saved = json.loads(open(cfg, encoding="utf-8").read())
        assert saved["ui_theme"]["sidebar_color"] == "#1f2d3d"
        # reset 恢复
        CustomizeThemeTool().execute(sidebar_color="reset",
                                     chat_bg_image="reset")
        saved = json.loads(open(cfg, encoding="utf-8").read())
        assert saved["ui_theme"]["sidebar_color"] == ""
        assert saved["ui_theme"]["chat_bg_image"] == ""

    def test_bg_image_path_safety(self, cfg):
        assert "Error" in CustomizeThemeTool().execute(chat_bg_image="../x.png")

    def test_toggles_and_decor(self, cfg):
        out = CustomizeThemeTool().execute(glass="on", animations="开",
                                           bordered="off", decor="petals")
        assert "✅" in out
        saved = json.loads(open(cfg, encoding="utf-8").read())["ui_theme"]
        assert saved["glass"] is True and saved["animations"] is True
        assert saved["bordered"] is False and saved["decor"] == "petals"
        # 非法值
        assert "Error" in CustomizeThemeTool().execute(glass="maybe")
        assert "Error" in CustomizeThemeTool().execute(decor="roses")


class TestThemeEndpoint:
    def test_get_theme(self, cfg, monkeypatch):
        import asyncio
        import api.routes.routes_system as rsys
        cfg.write_text(json.dumps(
            {"ui_theme": {"primary_color": "#ff0000",
                          "sidebar_color": "#1f2d3d",
                          "logo_file": "paste_a.png",
                          "chat_bg_image": "paste_bg.png"}}), encoding="utf-8")
        data = asyncio.run(rsys.get_theme())
        assert data["primary_color"] == "#ff0000"
        assert data["sidebar_color"] == "#1f2d3d"
        assert data["logo_url"] == "/api/upload/paste_a.png"
        assert data["chat_bg_url"] == "/api/upload/paste_bg.png"

    def test_get_theme_defaults(self, cfg):
        import asyncio
        import api.routes.routes_system as rsys
        data = asyncio.run(rsys.get_theme())
        assert data["primary_color"] == "" and data["logo_url"] == ""
        assert data["sidebar_color"] == "" and data["chat_bg_url"] == ""


class TestRegistration:
    def test_tool_registered(self):
        src = open(os.path.join(PROJECT_ROOT, "agent", "agent.py"),
                   encoding="utf-8").read()
        assert "CustomizeThemeTool()" in src
        assert '"customize_theme"' in src
