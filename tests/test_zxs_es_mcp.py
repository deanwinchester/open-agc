# -*- coding: utf-8 -*-
"""zxs_es MCP 封装与占位符替换测试。

- resolve_mcp_config：{app_exe}/{pyrun}/{app_dir} 在源码/冻结两种形态下的展开
- MCP 端到端：stub HTTP 后端 + 真实 stdio MCP 握手 + 三个工具调用
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tools.mcp_tool import resolve_mcp_config

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestResolveMcpConfig:
    CFG = {"zxs_es": {"command": "{app_exe}",
                      "args": ["{pyrun}", "{app_dir}/mcp_servers/zxs_es_mcp.py"]}}

    def test_source_mode(self, monkeypatch):
        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        out = resolve_mcp_config(self.CFG)["zxs_es"]
        assert out["command"] == sys.executable
        assert out["args"] == [f"{REPO_ROOT}/mcp_servers/zxs_es_mcp.py"]

    def test_frozen_mode(self, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", r"C:\bundle", raising=False)
        out = resolve_mcp_config(self.CFG)["zxs_es"]
        assert out["args"] == ["--pyrun", r"C:\bundle/mcp_servers/zxs_es_mcp.py"]


# ── stub 后端 ──

_DOC = {"id": "42", "index": "mcp_ncbdocument", "title": "测试标题", "doccontent": "正文" * 100}


class _StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/es/indices":
            self._json({"indices": [{"name": "mcp_ncbdocument", "docs": 1234}]})
        elif self.path.startswith("/api/es/doc/"):
            self._json(_DOC)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/api/es/search":
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            self._json({"total": 1, "hits": [{"title": "命中:" + body.get("query", ""),
                                              "size": body.get("size")}]})
        else:
            self._json({"error": "not found"}, 404)


@pytest.fixture(scope="module")
def stub_backend():
    srv = HTTPServer(("127.0.0.1", 0), _StubHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


@pytest.mark.skipif(not os.path.exists(f"{REPO_ROOT}/mcp_servers/zxs_es_mcp.py"),
                    reason="zxs_es_mcp.py 不在该分支")
def test_mcp_end_to_end(stub_backend):
    """真实 MCP 握手 + 三个工具调用（stub 后端）。"""
    from tools.mcp_tool import MCPClientManager
    mgr = MCPClientManager()
    tools = mgr.load_servers({"zxs_es": {
        "command": sys.executable,
        "args": [f"{REPO_ROOT}/mcp_servers/zxs_es_mcp.py"],
        "env": {"AGC_BACKEND_URL": stub_backend},
    }})
    names = set(tools)
    assert {"zxs_es_es_indices", "zxs_es_es_search", "zxs_es_es_get_doc"} <= names

    out = mgr.call_tool_sync("zxs_es", "es_indices", {})
    assert "mcp_ncbdocument" in out and "1234" in out

    out = mgr.call_tool_sync("zxs_es", "es_search", {"query": "国庆", "size": 5})
    assert "命中:国庆" in out and '"size": 5' in out

    out = mgr.call_tool_sync("zxs_es", "es_get_doc", {"index": "mcp_ncbdocument", "doc_id": "42"})
    assert "测试标题" in out
