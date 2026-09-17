# -*- coding: utf-8 -*-
"""分身提示词运行环境段 + 远程操作指引的回归测试。

生产实证：旧启发式自动委派拆出的分身，其 standalone 系统提示词没有任何
系统环境信息（「操作系统：Windows」只在主 agent 提示词里），简报里又全是
远程服务器的 POSIX 路径——分身拿着 ps/ss/bash 命令打本地 cmd 沙箱全灭，
直到 hostname 返回才发现自己不在目标机上。
"""
import sys

from agent.sub_agent import SubAgent


class _FakeTool:
    name = "fake_tool"

    def get_openai_schema(self):
        return {"type": "function", "function": {"name": "fake_tool",
                "description": "t", "parameters": {"type": "object",
                "properties": {}}}}


def _make_sub():
    return SubAgent(task="在服务器上部署 X", tools=["fake_tool"],
                    parent_tools={"fake_tool": _FakeTool()},
                    max_iterations=3, llm_client=None)


def test_standalone_prompt_has_os_line():
    sub = _make_sub()
    content = sub.messages[0]["content"]
    assert "- 操作系统：" in content
    if sys.platform.startswith("win"):
        assert "Windows" in content


def test_standalone_prompt_has_remote_exec_guidance():
    sub = _make_sub()
    content = sub.messages[0]["content"]
    assert "paramiko" in content
    assert "本机" in content  # 明确本机≠目标机


def test_shell_command_description_platform_aware():
    """execute_shell 的 command 参数描述必须按平台生成——写死 bash 示例
    会抵消工具描述里的 Windows 说明（生产实证）。"""
    from tools.shell import ShellTool
    schema = ShellTool().get_openai_schema()
    desc = schema["function"]["parameters"]["properties"]["command"]["description"]
    if sys.platform.startswith("win"):
        assert "cmd" in desc and "ls -la" not in desc
    else:
        assert "bash" in desc
