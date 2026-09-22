# -*- coding: utf-8 -*-
"""MCP 客户端 Streamable HTTP transport 测试。

{"url": ...} 形态的服务器配置走 streamablehttp_client（远端 MCP server，
如 zxs_es 的 POST /mcp）；{"command", "args"} 形态仍走 stdio。
"""
import socket
import threading
import time

import pytest


@pytest.fixture(scope="module")
def mcp_http_server():
    from mcp.server.fastmcp import FastMCP
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = FastMCP("stub", host="127.0.0.1", port=port)

    @srv.tool()
    def echo(text: str) -> str:
        """回显文本。"""
        return f"echo:{text}"

    t = threading.Thread(target=lambda: srv.run(transport="streamable-http"),
                         daemon=True)
    t.start()
    for _ in range(60):
        try:
            c = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            c.close()
            break
        except OSError:
            time.sleep(0.2)
    else:
        pytest.fail("stub MCP http server 未能启动")
    yield f"http://127.0.0.1:{port}/mcp/"


def test_http_transport_load_and_call(mcp_http_server):
    from tools.mcp_tool import MCPClientManager
    mgr = MCPClientManager()
    tools = mgr.load_servers({"stub": {"url": mcp_http_server}})
    assert "stub_echo" in tools
    out = mgr.call_tool_sync("stub", "echo", {"text": "你好"})
    assert "echo:你好" in out


def test_url_config_survives_resolve():
    """url 形态配置过 resolve_mcp_config 不丢失（占位符只处理 command/args）。"""
    from tools.mcp_tool import resolve_mcp_config
    cfg = {"zxs_es": {"url": "http://127.0.0.1:8080/mcp", "headers": {"X-Key": "k"}}}
    out = resolve_mcp_config(cfg)
    assert out["zxs_es"]["url"] == "http://127.0.0.1:8080/mcp"
    assert out["zxs_es"]["headers"] == {"X-Key": "k"}


class _RecordingManager:
    def __init__(self):
        self.received = None

    def call_tool_sync(self, server_name, tool_name, arguments, **kw):
        self.received = arguments
        return "ok"


def test_wrapper_strips_framework_injected_kwargs():
    """框架注入的 interrupt_check（函数）/_agent_context 不得转发给 MCP
    server——否则 MCP SDK 序列化报 Unable to serialize unknown type
    （生产实证）。"""
    from tools.mcp_tool import MCPToolWrapper
    mgr = _RecordingManager()
    w = MCPToolWrapper(mcp_server_name="s", tool_name="t", description="d",
                       input_schema={}, mcp_manager=mgr)

    class FakeAgent:
        pass

    out = w.execute(query="a", interrupt_check=lambda: False,
                    _agent_context=FakeAgent(), _sudo_password="x")
    assert out == "ok"
    assert mgr.received == {"query": "a"}
