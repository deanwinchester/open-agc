# -*- coding: utf-8 -*-
"""插件验收（verify）回归：用户要求插件开发完成后必须有测试环节，
避免功能缺陷带病交付（apiFetch.request 形状猜错、暗色主题与主应用
不一致等生产实证）。verify = 语法(esbuild)/契约/已知错误用法/主题告警。"""
import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tools.plugin_dev import DevelopPluginTool  # noqa: E402


@pytest.fixture()
def plugin_dir(tmp_path, monkeypatch):
    d = tmp_path / "plugins" / "demo"
    (d / "static").mkdir(parents=True)
    monkeypatch.setattr(DevelopPluginTool, "_plugins_base",
                        lambda self: str(tmp_path / "plugins"))
    return d


def _write_manifest(d, vue_entry="vue-entry.js"):
    (d / "plugin.json").write_text(json.dumps(
        {"name": "demo", "version": "1.0.0", "vue_entry": vue_entry}),
        encoding="utf-8")


def _write_init(d):
    (d / "__init__.py").write_text(
        "def init_plugin(context):\n    return None\n", encoding="utf-8")


def _write_entry(d, body="export default function setup(ctx){ return {views: []}; }\n"):
    (d / "static" / "vue-entry.js").write_text(body, encoding="utf-8")


class TestVerifyAction:
    def test_good_plugin_passes(self, plugin_dir):
        _write_manifest(plugin_dir)
        _write_init(plugin_dir)
        _write_entry(plugin_dir)
        out = DevelopPluginTool().execute(action="verify", plugin_name="demo")
        assert "语法检查（esbuild）" in out
        for bad in ("❌ vue-entry", "❌ 已知错误用法扫描", "❌ plugin.json",
                    "❌ __init__.py"):
            assert bad not in out

    def test_syntax_error_detected(self, plugin_dir):
        _write_manifest(plugin_dir)
        _write_init(plugin_dir)
        _write_entry(plugin_dir, "export default function setup(ctx { return ;;; }\n")
        out = DevelopPluginTool().execute(action="verify", plugin_name="demo")
        assert "❌ vue-entry 语法检查" in out

    def test_apifetch_request_detected(self, plugin_dir):
        """生产实证错误：apiFetch.request 必须被抓出来。"""
        _write_manifest(plugin_dir)
        _write_init(plugin_dir)
        _write_entry(plugin_dir,
                     "export default async function setup(ctx){"
                     " await ctx.apiFetch.request('/api/plugin/demo/x'); return {views:[]}; }\n")
        out = DevelopPluginTool().execute(action="verify", plugin_name="demo")
        assert "apiFetch 本身就是函数" in out
        assert "❌ 已知错误用法扫描" in out

    def test_dark_theme_warns_not_fails(self, plugin_dir):
        _write_manifest(plugin_dir)
        _write_init(plugin_dir)
        _write_entry(plugin_dir,
                     "const css = `.x { background: #1e1e2e; }`;\n"
                     "export default function setup(ctx){ return {views:[]}; }\n")
        out = DevelopPluginTool().execute(action="verify", plugin_name="demo")
        assert "⚠️ 主题风格" in out and "❌ 已知错误用法扫描" not in out

    def test_missing_manifest_and_init(self, plugin_dir):
        out = DevelopPluginTool().execute(action="verify", plugin_name="demo")
        assert "❌ plugin.json 有效" in out
        assert "❌ __init__.py 含 init_plugin" in out
        assert "未通过" in out

    def test_scaffold_summary_guides_verify(self, tmp_path, monkeypatch):
        monkeypatch.setattr(DevelopPluginTool, "_plugins_base",
                            lambda self: str(tmp_path / "plugins"))
        out = DevelopPluginTool().execute(
            action="scaffold", plugin_name="demo2", menu_label="演示")
        assert 'action="verify"' in out
        # 脚手架模板本身的 apiFetch 说明必须正确（历史误导措辞回归）
        entry = tmp_path / "plugins" / "demo2" / "static" / "vue-entry.js"
        src = entry.read_text(encoding="utf-8")
        assert "本身就是函数" in src and "没有 .request 方法" in src
        assert "request(url, options)，自动 JSON 解析" not in src
        # 模板自身应能通过 verify（语法 + 契约）
        out2 = DevelopPluginTool().execute(action="verify", plugin_name="demo2")
        assert "❌ vue-entry 语法检查" not in out2
        assert "❌ 已知错误用法扫描" not in out2


class TestVerifyPromptContracts:
    def test_skill_has_verify_step_and_theme_rule(self):
        content = open(os.path.join(PROJECT_ROOT, "skills",
                                    "plugin_development.md"), encoding="utf-8").read()
        assert 'action="verify"' in content
        assert "默认贴近主应用" in content
        assert "用户明确要求自定义主题时例外" in content

    def test_registry_surfaces_plugin_errors(self):
        src = open(os.path.join(PROJECT_ROOT, "vue-app", "src", "plugins",
                                "registry.js"), encoding="utf-8").read()
        assert "pluginErrors" in src and "setPluginError" in src
