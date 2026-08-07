# -*- coding: utf-8 -*-
"""PluginContext 标准化能力回归（实测插件开发暴露的缺口：agent 要读
llm_client 源码找用法、手写 JSON 存储——应提供标准 LLM/存储能力）。"""
import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.plugin_manager import PluginContext  # noqa: E402


def _ctx(tmp_path):
    return PluginContext(name="demo", plugin_dir=str(tmp_path),
                         db_dir=str(tmp_path / "data"))


class TestPluginContextStore:
    def test_kv_roundtrip(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.store.set("projects", [{"name": "第一本书"}])
        assert ctx.store.get("projects")[0]["name"] == "第一本书"
        assert "projects" in ctx.store.keys()
        ctx.store.delete("projects")
        assert ctx.store.get("projects", []) == []

    def test_persist_across_instances(self, tmp_path):
        _ctx(tmp_path).store.set("k", "v")
        assert _ctx(tmp_path).store.get("k") == "v"  # 新实例读得到
        data = json.loads((tmp_path / "data" / "_store.json").read_text(
            encoding="utf-8"))
        assert data["k"] == "v"

    def test_corrupt_store_returns_default(self, tmp_path):
        d = tmp_path / "data"
        d.mkdir()
        (d / "_store.json").write_text("{bad json", encoding="utf-8")
        assert _ctx(tmp_path).store.get("x", "d") == "d"


class TestPluginContextLlm:
    def test_llm_text_success(self, tmp_path, monkeypatch):
        class _Msg:
            content = "你好呀"

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        class _FakeClient:
            def chat(self, messages=None, **kw):
                return _Resp(), "kimi_code/k3"

        monkeypatch.setattr("core.llm_client.LLMClient", lambda: _FakeClient())
        assert _ctx(tmp_path).llm_text([{"role": "user", "content": "hi"}]) == "你好呀"

    def test_llm_text_failure_returns_default(self, tmp_path, monkeypatch):
        class _BadClient:
            def chat(self, **kw):
                raise RuntimeError("no key")

        monkeypatch.setattr("core.llm_client.LLMClient", lambda: _BadClient())
        assert _ctx(tmp_path).llm_text([], default="") == ""


class TestTemplateAndSkill:
    def test_template_uses_standard_helpers(self):
        src = open(os.path.join(PROJECT_ROOT, "tools", "plugin_dev.py"),
                   encoding="utf-8").read()
        assert "context.llm_text(" in src
        assert "context.store.set(" in src

    def test_skill_documents_helpers(self):
        content = open(os.path.join(PROJECT_ROOT, "skills",
                                    "plugin_development.md"), encoding="utf-8").read()
        assert "context.llm_text" in content
        assert "context.store" in content
