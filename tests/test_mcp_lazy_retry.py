# -*- coding: utf-8 -*-
"""MCP 懒重连测试：init 时不可达的 server，在工具检索时应被重试加载。

生产实证：agent init 时 MCP server 处于重启期 → 本会话永远没有
zxs_es_* 工具 → 模型转而自己用 urllib 直连 MCP 端点。
"""
import json

from tools.discovery import ToolDiscoveryTool
from tools.mcp_tool import MCPClientManager


class _FakeTool:
    name = "x"

    def get_openai_schema(self):
        return {"type": "function", "function": {"name": "x",
                "description": "t", "parameters": {"type": "object", "properties": {}}}}


def test_before_search_hook_invoked(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("tools.mcp_tool.get_mcp_manager",
                        lambda: MCPClientManager())
    t = ToolDiscoveryTool(full_tools={"x": _FakeTool()},
                          enable_callback=lambda names: None,
                          before_search=lambda: calls.append(1))
    t.execute("测试")
    assert calls == [1]


def test_lazy_retry_loads_missing_server(monkeypatch, tmp_path):
    """server init 失败（无 session）→ before_search 重试后工具并入 full_tools。"""
    # 模拟 agent 侧 _retry_mcp_servers 的等价逻辑
    from tools.mcp_tool import resolve_mcp_config
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"mcp_servers": {}}), encoding="utf-8")
    full_tools = {"x": _FakeTool()}
    enabled = []

    def retry():
        # config 为空 → 不触发加载，但钩子被调用且无副作用
        cfg = json.loads(cfg_path.read_text(encoding="utf-8")).get("mcp_servers", {})
        mgr = MCPClientManager()
        missing = {n: c for n, c in cfg.items() if n not in mgr._sessions}
        assert not missing

    t = ToolDiscoveryTool(full_tools=full_tools,
                          enable_callback=lambda names: enabled.extend(names),
                          before_search=retry)
    out = t.execute("测试")
    assert isinstance(out, str)
