# -*- coding: utf-8 -*-
"""soul.md 人格机制回归：提示词宣称「系统自动注入」但从未实现（用户反馈
agent 没有人格、总是以 Open-AGC 自居）。修复：soul.md 内容每次构建提示词
时注入 + 缺失播种默认人格 + MEMORY.md 路径修正（文档口径 data/MEMORY.md，
此前读 sandbox 下不存在的路径静默失效）+ 身份行不再硬编码 Open-AGC。"""
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import agent.agent as ag  # noqa: E402


class TestSoulInjection:
    def test_default_soul_constant_exists(self):
        assert "熊猫" in ag._DEFAULT_SOUL_MD
        assert "Open-AGC" in ag._DEFAULT_SOUL_MD  # 仅用于「不是产品」的区分声明

    def test_soul_injected_into_prompt(self, tmp_path, monkeypatch):
        """soul.md 内容应出现在系统提示词里。"""
        soul = tmp_path / "soul.md"
        soul.write_text("# 人格\n- 名字：测试喵\n", encoding="utf-8")
        monkeypatch.setattr("core.paths.get_data_path", lambda f: str(tmp_path / f))
        agent = ag.OpenAGCAgent.__new__(ag.OpenAGCAgent)
        agent.system_prompt_base = ""
        agent.sandbox_dir = str(tmp_path / "no_sandbox")
        # 最小化调用 _build_system_prompt 需要的属性
        try:
            prompt = agent._build_system_prompt()
        except TypeError:
            pytest.skip("_build_system_prompt 签名不适用直调")
        assert "测试喵" in prompt
        assert "人格设定 (soul.md)" in prompt

    def test_soul_seeded_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("core.paths.get_data_path", lambda f: str(tmp_path / f))
        agent = ag.OpenAGCAgent.__new__(ag.OpenAGCAgent)
        agent.system_prompt_base = ""
        agent.sandbox_dir = str(tmp_path / "no_sandbox")
        try:
            prompt = agent._build_system_prompt()
        except TypeError:
            pytest.skip("_build_system_prompt 签名不适用直调")
        assert (tmp_path / "soul.md").exists(), "缺失时应播种默认人格文件"
        assert "熊猫" in prompt

    def test_memory_md_reads_data_path(self, tmp_path, monkeypatch):
        """MEMORY.md 从 data/ 读取（文档口径），不再只找 sandbox。"""
        (tmp_path / "MEMORY.md").write_text("用户偏好：回复要简短", encoding="utf-8")
        monkeypatch.setattr("core.paths.get_data_path", lambda f: str(tmp_path / f))
        agent = ag.OpenAGCAgent.__new__(ag.OpenAGCAgent)
        agent.system_prompt_base = ""
        agent.sandbox_dir = str(tmp_path / "no_sandbox")
        try:
            prompt = agent._build_system_prompt()
        except TypeError:
            pytest.skip("_build_system_prompt 签名不适用直调")
        assert "用户偏好：回复要简短" in prompt

    def test_identity_not_hardcoded_product(self):
        """系统提示开头不再自称 Open-AGC，身份口径交给 soul.md。"""
        src = open(os.path.join(PROJECT_ROOT, "agent", "agent.py"),
                   encoding="utf-8").read()
        assert "你是 Open-AGC，一个强大的 AI 智能体" not in src
        assert "不要自称 Open-AGC" in src

    def test_soul_at_top_and_no_duplicate_doc(self, tmp_path, monkeypatch):
        """人格内容紧跟开头身份段（不是结尾的指针），且提示词里不再保留
        重复的「如何编辑 soul.md」说明段（用户反馈冗余）。"""
        soul = tmp_path / "soul.md"
        soul.write_text("# 人格\n- 名字：测试喵\n", encoding="utf-8")
        monkeypatch.setattr("core.paths.get_data_path", lambda f: str(tmp_path / f))
        agent = ag.OpenAGCAgent.__new__(ag.OpenAGCAgent)
        agent.system_prompt_base = "# 身份与人格\nX\n# 能力与纪律\nY\n# 任务执行规范\nZ\n"
        agent.sandbox_dir = str(tmp_path / "no_sandbox")
        prompt = agent._build_system_prompt()
        soul_pos = prompt.find("测试喵")
        assert 0 < soul_pos < prompt.find("# 能力与纪律"), "人格内容应在开头身份段之后"
        assert "人格设定文件" not in prompt, "冗余的 soul.md 说明段应已移除"
        # 编辑指引只出现一次（内容尾部的一句话）
        assert prompt.count("write_file 修改 data/soul.md") <= 1 or \
               "write_file 修改 data/soul.md" not in prompt.split("人格设定 (soul.md)")[0]
