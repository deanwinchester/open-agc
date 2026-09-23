# -*- coding: utf-8 -*-
"""_build_mcp_section：MCP 工具必须在系统提示里带描述和优先使用规则。

背景：通用扩展工具列表只有裸名字，模型不知道 `zxs_es_es_search` 是什么，
从不主动唤醒（用户每次都要手动提醒），唤醒前甚至手写 HTTP 直连 MCP 端点。
"""
from types import SimpleNamespace

from agent.agent import OpenAGCAgent


def _agent_with_tools(tools):
    agent = OpenAGCAgent.__new__(OpenAGCAgent)
    agent.full_available_tools = tools
    agent.active_tool_names = set()
    return agent


class _FakeMcpTool:
    def __init__(self, name, description, server):
        self.name = name
        self.description = description
        self.mcp_server_name = server


def test_mcp_section_lists_description_and_priority_rule():
    tools = {
        "zxs_es_es_search": _FakeMcpTool("zxs_es_es_search", "全文检索 ES 稿件", "zxs_es"),
        "zxs_es_es_indices": _FakeMcpTool("zxs_es_es_indices", "列出索引", "zxs_es"),
        "read_file": SimpleNamespace(name="read_file", description="读文件"),
    }
    agent = _agent_with_tools(tools)
    section = agent._build_tool_list_section()
    # MCP 工具带服务分组与描述
    assert "**zxs_es**" in section
    assert "全文检索 ES 稿件" in section
    assert "列出索引" in section
    # 优先使用规则
    assert "必须优先" in section
    assert "禁止用 execute_python" in section


def test_mcp_section_absent_without_mcp_tools():
    tools = {"read_file": SimpleNamespace(name="read_file", description="读文件")}
    agent = _agent_with_tools(tools)
    assert "MCP 服务" not in agent._build_tool_list_section()
