# -*- coding: utf-8 -*-
"""主题导出/导入/市场回归：POST /api/theme 与 customize_theme 同口径校验；
/api/theme/market 内置预设兜底 + 远程索引合并。"""
import asyncio
import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import api.routes.routes_system as rsys  # noqa: E402
from fastapi import HTTPException  # noqa: E402


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("core.paths.get_data_path", lambda f: str(cfg_file))
    monkeypatch.setattr("api.config.CONFIG_PATH", str(cfg_file))
    return cfg_file


class TestSaveTheme:
    def test_save_and_broadcast(self, cfg):
        out = asyncio.run(rsys.save_theme({"theme": {
            "primary_color": "16a34a", "glass": True, "decor": "stars"}}))
        assert out["status"] == "success"
        assert out["theme"]["primary_color"] == "#16a34a"  # 补 # 号
        saved = json.loads(open(cfg, encoding="utf-8").read())
        assert saved["ui_theme"]["glass"] is True
        assert saved["ui_theme"]["decor"] == "stars"

    def test_bad_color_400(self, cfg):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(rsys.save_theme({"theme": {"primary_color": "red"}}))
        assert ei.value.status_code == 400

    def test_bad_decor_400(self, cfg):
        with pytest.raises(HTTPException):
            asyncio.run(rsys.save_theme({"theme": {"decor": "roses"}}))

    def test_evil_css_rejected(self, cfg):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(rsys.save_theme({"theme": {
                "custom_css": "body{background:url(https://evil.com/x.gif)}"}}))
        assert ei.value.status_code == 400

    def test_safe_css_accepted(self, cfg):
        out = asyncio.run(rsys.save_theme({"theme": {
            "custom_css": ".msg-bubble{border-radius:20px}"}}))
        assert out["status"] == "success"
        assert "border-radius" in out["theme"]["custom_css"]


class TestThemeMarket:
    def test_builtin_presets_fallback(self, cfg, monkeypatch):
        """远程不可达时内置预设兜底。"""
        monkeypatch.setitem(__import__("urllib.request").__dict__, "urlopen", None) \
            if False else None
        # 强制远程失败：给一个不可达地址
        cfg.write_text(json.dumps(
            {"theme_market_url": "http://127.0.0.1:9/none.json"}),
            encoding="utf-8")
        data = asyncio.run(rsys.theme_market())
        names = [t["name"] for t in data["themes"]]
        assert "猫娘粉" in names and "暗夜黑" in names
        assert all(t["source"] == "preset" for t in data["themes"])

    def test_remote_merge(self, cfg, monkeypatch, tmp_path):
        idx = tmp_path / "themes.json"
        idx.write_text(json.dumps({"themes": [{
            "name": "好友主题", "desc": "分享来的", "author": "friend",
            "theme": {"primary_color": "#123456"}}]}), encoding="utf-8")
        cfg.write_text(json.dumps(
            {"theme_market_url": idx.as_uri()}), encoding="utf-8")
        data = asyncio.run(rsys.theme_market())
        names = [t["name"] for t in data["themes"]]
        assert "好友主题" in names
        market = [t for t in data["themes"] if t["name"] == "好友主题"][0]
        assert market["source"] == "market" and market["author"] == "friend"
        # 内置预设仍在
        assert any(t["source"] == "preset" for t in data["themes"])

    def test_market_theme_appliable(self, cfg, monkeypatch, tmp_path):
        """市场主题能直接进 POST /api/theme 应用（契约一致性）。"""
        data = asyncio.run(rsys.theme_market())
        for t in data["themes"]:
            out = asyncio.run(rsys.save_theme({"theme": t["theme"]}))
            assert out["status"] == "success", f"预设 {t['name']} 不可应用"

    def test_merge_preserves_untouched_fields(self, cfg):
        """merge（默认）：应用局部预设不得抹掉未提供的字段（生产实证：
        应用市场主题后 logo_file 被清空）。"""
        asyncio.run(rsys.save_theme({"theme": {
            "logo_file": "my_logo.png", "custom_css": ".a{}"}}))
        out = asyncio.run(rsys.save_theme({"theme": {"primary_color": "#123456"}}))
        assert out["theme"]["logo_file"] == "my_logo.png"
        assert out["theme"]["custom_css"] == ".a{}"
        assert out["theme"]["primary_color"] == "#123456"

    def test_light_presets_carry_dark_false(self, cfg):
        """非暗色预设必须显式 dark:False——否则 merge 语义下用户从暗色
        主题切浅色主题时 dark 标志残留（生产实证：切默认/猫娘粉仍全站黑）。"""
        data = asyncio.run(rsys.theme_market())
        for t in data["themes"]:
            if t["name"] == "暗夜黑":
                assert t["theme"].get("dark") is True
            else:
                assert t["theme"].get("dark") is False, f"{t['name']} 缺少 dark:False"

    def test_switch_dark_to_light(self, cfg):
        """暗色 → 猫娘粉：dark 必须被关回去。"""
        asyncio.run(rsys.save_theme({"theme": {"dark": True}}))
        catgirl = {"primary_color": "#E88FB0", "sidebar_color": "#8E5B78",
                   "dark": False, "decor": "petals"}
        out = asyncio.run(rsys.save_theme({"theme": catgirl}))
        assert out["theme"]["dark"] is False

    def test_replace_clears_all(self, cfg):
        """replace 模式：整节替换（恢复默认用）。"""
        asyncio.run(rsys.save_theme({"theme": {
            "logo_file": "my_logo.png", "primary_color": "#123456"}}))
        out = asyncio.run(rsys.save_theme({"mode": "replace", "theme": {}}))
        assert out["theme"].get("logo_file", "") == ""
        assert out["theme"].get("primary_color", "") == ""
