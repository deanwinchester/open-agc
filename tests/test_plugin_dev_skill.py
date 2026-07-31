# -*- coding: utf-8 -*-
"""插件开发生态回归：agent 开发插件未用 develop_plugin、另起独立服务/端口、
LLM 不跟随系统设置（用户反馈）。修复：插件开发技能 + 系统提示架构红线 +
脚手架模板 LLM 示例。"""
import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class TestPluginDevSkill:
    def test_skill_file_exists_with_key_contracts(self):
        path = os.path.join(PROJECT_ROOT, "skills", "plugin_development.md")
        assert os.path.isfile(path)
        content = open(path, encoding="utf-8").read()
        assert content.startswith("# ")
        for needle in ("develop_plugin", "POST /api/plugins/scan",
                       "core.llm_client", "禁止独立服务", "无需重启服务"):
            assert needle in content, f"技能缺少关键契约: {needle}"

    def test_skill_registered_in_index(self):
        for idx in ("skills/index.json", "data/skills/index.json"):
            path = os.path.join(PROJECT_ROOT, idx)
            if not os.path.isfile(path):
                continue
            data = json.load(open(path, encoding="utf-8"))
            files = [s["filename"] for s in data["skills"]]
            assert "plugin_development.md" in files, f"{idx} 未登记"

    def test_skill_retrievable_for_plugin_query(self):
        from core.skill_store import SkillStore
        ss = SkillStore(skills_dir=os.path.join(PROJECT_ROOT, "skills"))
        hits = ss.retrieve("帮我开发一个插件，给系统加个页面")
        assert hits and hits[0]["filename"] == "plugin_development.md"


class TestArchitectureRedLine:
    def test_system_prompt_has_red_line(self):
        src = open(os.path.join(PROJECT_ROOT, "agent", "agent.py"),
                   encoding="utf-8").read()
        assert "架构红线" in src
        assert "develop_plugin 插件架构" in src
        assert "POST /api/plugins/scan" in src
        assert "core.llm_client.LLMClient()" in src

    def test_scaffold_template_has_llm_example(self):
        src = open(os.path.join(PROJECT_ROOT, "tools", "plugin_dev.py"),
                   encoding="utf-8").read()
        assert "from core.llm_client import LLMClient" in src
        assert "禁止自行硬编码" in src
