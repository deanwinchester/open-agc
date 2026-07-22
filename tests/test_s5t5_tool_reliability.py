# Open-AGC test suite
# -*- coding: utf-8 -*-
"""阶段5 Task5: 工具可靠性清偿测试。

1. shell 后台误判矩阵: npm start / start chrome / xxx & / start.py / echo start
2. shell 交互式误判: "Progress: 50%" 不判交互、行尾 >>> 判交互
3. download 直连: 404 抛错 / 大小不符抛错 / 正常 200 写盘
4. auto_tool AST 白名单: 恶意样本拒绝 (f-string 隐藏 os.system / __import__ / exec)
5. MCP: call_tool_sync 超时 + session 重建、死会话重连
"""
import asyncio
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tools.shell import _is_background_command, _detect_interactive_prompt
from tools.download import _download_direct
from tools.auto_tool import validate_tool_code
from tools.mcp_tool import MCPClientManager


# ───────────────────────── Fix 1: 后台命令误判矩阵 ─────────────────────────

class TestBackgroundCommand:
    """收窄后: 仅「首 token 为 Windows start 内建命令」或「行尾 &」算后台。"""

    @pytest.mark.parametrize("cmd", [
        "npm start",                 # start 是 npm 子命令参数，非 Windows 内建
        "npm run start",
        "python start.py",           # 文件名
        "start.py",                  # start 后非空白/结尾
        "echo start",                # start 不在首位
        'echo "start"',              # 引号内
        "net start mysql",           # net start 会立即返回，前台处理正确
        "cmd /c start notepad",      # 首 token 是 cmd；cmd 自身立即返回
        "ls -la",
        "pip install requests",
    ])
    def test_not_background(self, cmd):
        assert _is_background_command(cmd) is False

    @pytest.mark.parametrize("cmd", [
        "start chrome",              # Windows start 内建
        "start /min cmd /c run.bat",
        "START notepad",             # 大小写不敏感
        "start",                     # 裸 start
        "  start   explorer",        # 前导空白
        "xxx &",                     # Unix 后台操作符
        "python server.py &",
        "ls -la &",
        "sleep 10 &  ",
    ])
    def test_background(self, cmd):
        assert _is_background_command(cmd) is True


# ───────────────────────── Fix 2: 交互式提示误判矩阵 ─────────────────────────

class TestInteractivePrompt:
    """收窄为整行提示符模式：进度/键值输出不再误判为交互。"""

    @pytest.mark.parametrize("data", [
        b"Progress: 50%",                      # 旧 b'ress: ' 误伤
        b"Download in progress: 90%\r",
        b"Address: 192.168.1.1\n",             # 旧 b'ress: ' 误伤
        b"key: value\nfoo: bar\n",             # 旧 b' :' 误伤
        b"Loading...",                         # ... 必须整行才算续行提示
        b"Resuming download: 10%\rResuming download: 20%",
        b"Error: something failed: retrying\n",
        b"http://localhost:8080 started\n",
        b"",
        b"\n\n\n",
    ])
    def test_not_interactive(self, data):
        assert _detect_interactive_prompt(data) is False

    @pytest.mark.parametrize("data", [
        b"mysql> ",
        b"sqlite> ",
        b"psql> ",
        b"some banner output\n>>> ",           # 行尾 >>> 判交互
        b">>>",
        b"... ",                               # 整行 ... 为 Python 续行提示
        b"In [1]: ",
        b"> ",                                 # llama.cpp/Ollama 裸提示符
        b"password: ",
        b"Password for root: ",
        b"login: ",
        b"user@host:~$ ",                      # bash 风格提示符
        b"root@server:/var/log# ",
        b"Previous output line\nmysql> ",
    ])
    def test_interactive(self, data):
        assert _detect_interactive_prompt(data) is True


# ───────────────────────── Fix 3: 直连下载 HTTP 错误处理 ─────────────────────────

class _FakeResp:
    """Minimal requests.Response stand-in for _download_direct tests."""

    def __init__(self, status_code=200, body=b"", headers=None, raise_exc=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers if headers is not None else {}
        self._raise_exc = raise_exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        if self._raise_exc is not None:
            raise self._raise_exc

    def iter_content(self, size):
        for i in range(0, len(self._body), size):
            yield self._body[i:i + size]


class TestDirectDownload:
    def test_404_raises_and_writes_nothing(self, tmp_path, monkeypatch):
        import requests

        def fake_get(url, **kw):
            return _FakeResp(
                status_code=404,
                body=b"<html>404 Not Found</html>",
                raise_exc=requests.HTTPError("404 Client Error"),
            )

        monkeypatch.setattr(requests, "get", fake_get)
        target = str(tmp_path / "model.gguf")
        with pytest.raises(requests.HTTPError):
            _download_direct("http://example.com/model.gguf", target)
        # 404 错误页绝不能落盘
        assert not os.path.exists(target)
        assert not os.path.exists(target + ".partial")

    def test_500_raises(self, tmp_path, monkeypatch):
        import requests

        def fake_get(url, **kw):
            return _FakeResp(
                status_code=500,
                body=b"Internal Server Error",
                raise_exc=requests.HTTPError("500 Server Error"),
            )

        monkeypatch.setattr(requests, "get", fake_get)
        with pytest.raises(requests.HTTPError):
            _download_direct("http://example.com/x.bin", str(tmp_path / "x.bin"))

    def test_unexpected_status_raises(self, tmp_path, monkeypatch):
        import requests

        def fake_get(url, **kw):
            return _FakeResp(status_code=204, body=b"")

        monkeypatch.setattr(requests, "get", fake_get)
        with pytest.raises(RuntimeError, match="Unexpected HTTP status"):
            _download_direct("http://example.com/x.bin", str(tmp_path / "x.bin"))

    def test_size_mismatch_raises(self, tmp_path, monkeypatch):
        import requests

        def fake_get(url, **kw):
            return _FakeResp(
                status_code=200,
                body=b"only-50-bytes",
                headers={"content-length": "100"},
            )

        monkeypatch.setattr(requests, "get", fake_get)
        target = str(tmp_path / "x.bin")
        with pytest.raises(RuntimeError, match="incomplete"):
            _download_direct("http://example.com/x.bin", target)
        assert not os.path.exists(target)  # 不完整文件不得替换为成品

    def test_empty_body_raises(self, tmp_path, monkeypatch):
        import requests

        def fake_get(url, **kw):
            return _FakeResp(status_code=200, body=b"", headers={})

        monkeypatch.setattr(requests, "get", fake_get)
        with pytest.raises(RuntimeError, match="empty"):
            _download_direct("http://example.com/x.bin", str(tmp_path / "x.bin"))

    def test_success_writes_file(self, tmp_path, monkeypatch):
        import requests
        body = b"GGUF" + b"\x00" * 100

        def fake_get(url, **kw):
            return _FakeResp(
                status_code=200,
                body=body,
                headers={"content-length": str(len(body))},
            )

        monkeypatch.setattr(requests, "get", fake_get)
        target = str(tmp_path / "ok.bin")
        assert _download_direct("http://example.com/ok.bin", target) is True
        with open(target, "rb") as f:
            assert f.read() == body
        assert not os.path.exists(target + ".partial")  # partial 已转正


# ───────────────────────── Fix 4: auto_tool AST 白名单 ─────────────────────────

_GOOD_TOOL = '''
import json
import re
import os
import requests

TOOL_SCHEMA = {"name": "ok_tool", "description": "x", "parameters": {"type": "object", "properties": {}}}

def execute(**kwargs):
    data = requests.get("https://example.com/api", timeout=10).json()
    out_dir = os.path.join(os.getcwd(), "out")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join("out", "result.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    with open(path, "r") as f:
        loaded = json.load(f)
    return re.sub(r"\\s+", " ", str(loaded))
'''
class TestValidateToolCode:
    def test_benign_tool_accepted(self):
        assert validate_tool_code(_GOOD_TOOL) is True

    def test_benign_stdlib_accepted(self):
        code = (
            "import math\nimport datetime\nfrom collections import Counter\n"
            "TOOL_SCHEMA = {}\n"
            "def execute(**kwargs):\n"
            "    return str(math.sqrt(Counter('aab')['a']))\n"
        )
        assert validate_tool_code(code) is True

    def test_syntax_error_rejected(self):
        assert validate_tool_code("def execute(:\n    pass") is False

    @pytest.mark.parametrize("code", [
        # os 非白名单成员
        "import os\ndef execute(**kwargs):\n    return os.system('ls')\n",
        "import os\ndef execute(**kwargs):\n    os.remove('x.txt')\n",
        # f-string 属性访问中隐藏的 os.system（旧正则完全拦不住）
        "import os\ndef execute(**kwargs):\n    return f\"{os.system('ls')}\"\n",
        # import 别名也拦
        "import os as o\ndef execute(**kwargs):\n    return o.system('ls')\n",
        # import os.path 实际绑定整个 os 模块
        "import os.path\ndef execute(**kwargs):\n    return os.system('ls')\n",
        "from os import system\ndef execute(**kwargs):\n    return system('ls')\n",
        # 动态求值 / 动态导入
        "def execute(**kwargs):\n    return __import__('os').system('ls')\n",
        "def execute(**kwargs):\n    return exec('print(1)')\n",
        "def execute(**kwargs):\n    return eval('1+1')\n",
        "def execute(**kwargs):\n    return eval(compile('1', '<s>', 'eval'))\n",
        # getattr 绕过属性白名单
        "import os\ndef execute(**kwargs):\n    return getattr(os, 'system')('ls')\n",
        "import os\ndef execute(**kwargs):\n    return getattr(os, 'sys'+'tem')('ls')\n",
        # dunder 逃逸
        "def execute(**kwargs):\n    return ().__class__.__bases__[0].__subclasses__()\n",
        # 被禁模块
        "import subprocess\ndef execute(**kwargs):\n    return subprocess.run(['ls'])\n",
        "from subprocess import run\ndef execute(**kwargs):\n    return run(['ls'])\n",
        "import sys\ndef execute(**kwargs):\n    return sys.version\n",
        "import socket\ndef execute(**kwargs):\n    return socket.gethostname()\n",
        "import ctypes\n",
        "from urllib.request import urlopen\ndef execute(**kwargs):\n    return urlopen('http://x')\n",
        "import ftplib\n",
        "import importlib\ndef execute(**kwargs):\n    return importlib.import_module('os')\n",
        # shutil.rmtree 不在白名单
        "import shutil\ndef execute(**kwargs):\n    return shutil.rmtree('/')\n",
        # open 绝对路径（任意模式）
        "def execute(**kwargs):\n    return open('/etc/passwd').read()\n",
        "def execute(**kwargs):\n    return open('C:/Windows/win.ini').read()\n",
        "def execute(**kwargs):\n    return open('~/secret', 'w')\n",
        "def execute(**kwargs):\n    return open('out.txt', mode=kwargs['m'])\n",
        # 相对/星号导入
        "from . import helper\n",
        "from os import *\n",
    ])
    def test_malicious_rejected(self, code):
        assert validate_tool_code(code) is False

    @pytest.mark.parametrize("code", [
        # os 白名单成员
        "import os\ndef execute(**kwargs):\n    return os.path.join('a', 'b')\n",
        "import os\ndef execute(**kwargs):\n    os.makedirs('out', exist_ok=True)\n    return os.getcwd()\n",
        "from os.path import join\ndef execute(**kwargs):\n    return join('a', 'b')\n",
        # requests 白名单方法
        "import requests\ndef execute(**kwargs):\n    return requests.get('http://x', timeout=5).text\n",
        # 相对路径写文件允许
        "def execute(**kwargs):\n    open('out.txt', 'w').write('hi')\n    return 'ok'\n",
        # 非字面量路径 + 写字段模式允许（静态无法判定，报告已记录残余风险）
        "def execute(**kwargs):\n    p = kwargs.get('p', 'o.txt')\n    open(p, 'w').write('hi')\n    return p\n",
        # urllib.parse 允许（纯计算）
        "from urllib.parse import quote\ndef execute(**kwargs):\n    return quote('a b')\n",
        # shutil 白名单成员
        "import shutil\ndef execute(**kwargs):\n    return shutil.which('python')\n",
    ])
    def test_whitelisted_accepted(self, code):
        assert validate_tool_code(code) is True


class TestASTBypassRegression:
    """评审发现的 3 个 AST 校验绕过孔的回归用例。

    孔1: import builtins 绕过（builtins.exec / builtins.open 曾放行）
    孔2: 模块对象别名穿透（x = os; x.system(...) 曾放行）
    孔3: io.open / codecs.open / pathlib 读写绕过
    """

    @pytest.mark.parametrize("code", [
        # ── 孔1: builtins 模块（整体封禁，与 from builtins import X 统一）──
        "import builtins\ndef execute(**kwargs):\n    return builtins.exec('x=1')\n",
        "import builtins\ndef execute(**kwargs):\n    return builtins.open('/etc/passwd').read()\n",
        "import builtins as b\ndef execute(**kwargs):\n    return b.eval('1')\n",
        "from builtins import exec\ndef execute(**kwargs):\n    return exec('x=1')\n",
        "from builtins import open\ndef execute(**kwargs):\n    return open('/etc/passwd').read()\n",
        # ── 孔2: 模块对象别名 ──
        "import os\nx = os\ndef execute(**kwargs):\n    return x.system('ls')\n",
        "import subprocess\ndef execute(**kwargs):\n    x = subprocess\n    return x.run(['ls'])\n",
        "import requests\ns = requests\ndef execute(**kwargs):\n    return s.get('http://x')\n",
        "def execute(**kwargs):\n    x = sys\n    return x\n",
        "import os\ndef execute(**kwargs):\n    y: object = os\n    return y.system('ls')\n",
        # ── 孔3: io.open / codecs.open / pathlib ──
        "import io\ndef execute(**kwargs):\n    return io.open('/etc/passwd').read()\n",
        "import io\ndef execute(**kwargs):\n    return io.open('C:/Windows/win.ini').read()\n",
        "from io import open\ndef execute(**kwargs):\n    return open('/etc/passwd').read()\n",
        "import codecs\ndef execute(**kwargs):\n    return codecs.open('/etc/passwd').read()\n",
        "from codecs import open\ndef execute(**kwargs):\n    return open('~/secret').read()\n",
        "from pathlib import Path\ndef execute(**kwargs):\n    return Path('/etc/passwd').read_text()\n",
        "from pathlib import Path\ndef execute(**kwargs):\n    Path('C:/Windows/win.ini').write_text('x')\n    return 'x'\n",
        "import pathlib\ndef execute(**kwargs):\n    return pathlib.Path('/etc/passwd').read_text()\n",
        "import pathlib as pl\ndef execute(**kwargs):\n    return pl.Path('~/secret').read_bytes()\n",
        "from pathlib import PosixPath\ndef execute(**kwargs):\n    return PosixPath('/etc/passwd').read_text()\n",
        "from pathlib import WindowsPath\ndef execute(**kwargs):\n    return WindowsPath('C:/x').read_text()\n",
        # io 非白名单成员同样按受限模块规则拒绝
        "import io\ndef execute(**kwargs):\n    return io.IOBase\n",
    ])
    def test_bypass_rejected(self, code):
        assert validate_tool_code(code) is False

    @pytest.mark.parametrize("code", [
        # io/codecs 白名单成员与相对路径——正常生成功能不得被误杀
        "import io\ndef execute(**kwargs):\n    return io.StringIO('x').getvalue()\n",
        "import io\ndef execute(**kwargs):\n    return io.BytesIO(b'x').read()\n",
        "import io\ndef execute(**kwargs):\n    io.open('out.txt', 'w').write('hi')\n    return 'ok'\n",
        "import codecs\ndef execute(**kwargs):\n    return codecs.encode('x', 'utf-8')\n",
        "import codecs\ndef execute(**kwargs):\n    codecs.open('out.txt', 'w', encoding='utf-8').write('hi')\n    return 'ok'\n",
        "from io import open\ndef execute(**kwargs):\n    open('out.txt', 'w').write('hi')\n    return 'ok'\n",
        # pathlib 相对路径读写放行
        "from pathlib import Path\ndef execute(**kwargs):\n    Path('out.txt').write_text('hi')\n    return Path('out.txt').read_text()\n",
        "import pathlib\ndef execute(**kwargs):\n    return pathlib.Path('data').joinpath('a.txt').name\n",
        # 非字面量路径（静态无法判定，记录为残余风险，放行）
        "from pathlib import Path\ndef execute(**kwargs):\n    return Path(kwargs['p']).write_text('x')\n",
        # 非模块的普通赋值不受别名检查影响
        "import os\ndef execute(**kwargs):\n    p = os.path\n    return p.join('a', 'b')\n",
        "def execute(**kwargs):\n    x = 5\n    y = 'str'\n    return str(x) + y\n",
        # 局部定义的 Path 函数（非 pathlib 导入）不误判
        "def Path(p):\n    return str(p)\ndef execute(**kwargs):\n    return Path('/x')\n",
    ])
    def test_still_accepted(self, code):
        assert validate_tool_code(code) is True


# ───────────────────────── Fix 5: MCP 超时 / 重连 ─────────────────────────

class TestMCPTimeout:
    def test_call_tool_timeout_resets_session(self):
        """future.result 带超时；超时后取消协程并重建 session。"""
        mgr = MCPClientManager()
        reset_calls = []
        mgr._reset_session = lambda name: reset_calls.append(name)

        async def hang(server, tool, args):
            await asyncio.sleep(30)
            return "never"

        mgr._async_call_tool = hang
        result = mgr.call_tool_sync("srv", "tool", {}, timeout=0.2)
        assert "timed out" in result
        assert "srv" in result
        assert reset_calls == ["srv"]

    def test_call_tool_normal_result_unaffected(self):
        mgr = MCPClientManager()
        mgr._reset_session = lambda name: pytest.fail("should not reset")

        async def quick(server, tool, args):
            return "fine"

        mgr._async_call_tool = quick
        assert mgr.call_tool_sync("srv", "tool", {}, timeout=5) == "fine"

    def test_dead_session_reconnects_on_load(self):
        """load_servers 对死会话先 teardown 再重连，而非 continue 跳过。"""
        mgr = MCPClientManager()
        mgr._sessions["dead"] = object()  # 占位旧会话
        torn_down = []
        connected = []

        async def fake_alive(name):
            return False

        async def fake_teardown(name):
            torn_down.append(name)
            mgr._sessions.pop(name, None)

        async def fake_connect(name, cfg):
            connected.append(name)

        mgr._session_alive = fake_alive
        mgr._teardown_session = fake_teardown
        mgr._connect_one = fake_connect
        asyncio.run(mgr._async_load_servers({"dead": {"command": "x"}}))
        assert torn_down == ["dead"]
        assert connected == ["dead"]
        assert mgr.servers["dead"] == {"command": "x"}  # 配置已留存供重建

    def test_alive_session_kept(self):
        """活会话不重建。"""
        mgr = MCPClientManager()
        mgr._sessions["ok"] = object()
        connected = []

        async def fake_alive(name):
            return True

        async def fake_connect(name, cfg):
            connected.append(name)

        mgr._session_alive = fake_alive
        mgr._connect_one = fake_connect
        asyncio.run(mgr._async_load_servers({"ok": {"command": "x"}}))
        assert connected == []

    def test_reset_session_rebuilds_from_saved_config(self):
        mgr = MCPClientManager()
        mgr.servers["s1"] = {"command": "run-s1"}
        mgr._sessions["s1"] = object()
        mgr._contexts["s1"] = None
        mgr._tools = {}
        events = []

        async def fake_teardown(name):
            events.append(("teardown", name))

        async def fake_connect(name, cfg):
            events.append(("connect", name, cfg))

        mgr._teardown_session = fake_teardown
        mgr._connect_one = fake_connect
        asyncio.run(mgr._async_reset_session("s1"))
        assert events == [("teardown", "s1"), ("connect", "s1", {"command": "run-s1"})]
