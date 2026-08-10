# -*- coding: utf-8 -*-
"""主题包完整性回归：导出内嵌 Logo/背景图（base64）与自定义 CSS、应用名；
导入时图片解码落盘 uploads/。用户反馈：主题包应包含头像与 agent 写的 CSS。"""
import asyncio
import base64
import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import api.routes.routes_system as rsys  # noqa: E402

_PNG = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000d49444154789c626001000000ffff030000060005"
    "57bfabd40000000049454e44ae426082")).decode()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    cfg_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("core.paths.get_data_path", lambda f: str(cfg_file))
    monkeypatch.setattr("api.config.CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr("api.routes.uploads._uploads_dir", lambda: str(uploads))
    return cfg_file, uploads


class TestThemePackage:
    def test_export_embeds_images_and_css(self, env):
        cfg_file, uploads = env
        (uploads / "logo.png").write_bytes(base64.b64decode(_PNG))
        cfg_file.write_text(json.dumps({"ui_theme": {
            "primary_color": "#E88FB0", "logo_file": "logo.png",
            "app_name": "猫娘", "custom_css": ".x{color:pink}",
            "decor": "petals"}}), encoding="utf-8")
        pkg = asyncio.run(rsys.export_theme())
        th = pkg["theme"]
        assert th["app_name"] == "猫娘"
        assert th["custom_css"] == ".x{color:pink}"
        assert th["logo_data"].startswith("data:image/png;base64,")

    def test_import_with_embedded_image(self, env):
        cfg_file, uploads = env
        out = asyncio.run(rsys.save_theme({"theme": {
            "app_name": "好友的主题",
            "logo_data": f"data:image/png;base64,{_PNG}"}}))
        assert out["status"] == "success"
        logo_file = out["theme"]["logo_file"]
        assert logo_file.startswith("theme_logo_")
        # 文件真实写入 uploads
        assert (uploads / logo_file).is_file()
        assert out["theme"]["app_name"] == "好友的主题"

    def test_import_bad_image_data_400(self, env):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            asyncio.run(rsys.save_theme({"theme": {"logo_data": "data:text/html;base64,PGI+"}}))
        assert ei.value.status_code == 400

    def test_app_name_length_guard(self, env):
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            asyncio.run(rsys.save_theme({"theme": {"app_name": "x" * 31}}))

    def test_get_theme_exposes_app_name(self, env):
        cfg_file, _ = env
        cfg_file.write_text(json.dumps(
            {"ui_theme": {"app_name": "熊猫"}}), encoding="utf-8")
        data = asyncio.run(rsys.get_theme())
        assert data["app_name"] == "熊猫"


class TestAppNameTool:
    def test_tool_sets_app_name(self, env):
        from tools.theme_tool import CustomizeThemeTool
        out = CustomizeThemeTool().execute(app_name="猫娘事务所")
        assert "✅" in out
        saved = json.loads(open(env[0], encoding="utf-8").read())
        assert saved["ui_theme"]["app_name"] == "猫娘事务所"
        CustomizeThemeTool().execute(app_name="reset")
        saved = json.loads(open(env[0], encoding='utf-8').read())
        assert saved["ui_theme"]["app_name"] == ""
