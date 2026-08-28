# -*- coding: utf-8 -*-
"""approve_once（「授权本次」）一次性授权测试。

根因：此前 approve_once 与 approve_session 混用同一条会话白名单路径，
导致首次 sudo 授权后，后续同类命令被会话白名单自动放行、不再弹窗。
修复：approve_once 只授权当前这一条命令（one-shot，shell 消费掉），
下一条同类命令重新弹窗。

All tests stub subprocess.Popen — no real sudo, no network, no DB.
"""
import os
import sys
import threading
import time

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import tools.shell as shell_mod  # noqa: E402
from tools.base import SandboxBlocked  # noqa: E402
from tools.shell import ShellTool  # noqa: E402

TEST_SID = 987002


@pytest.fixture(autouse=True)
def _clean_shared_store():
    from api.state import (_session_sudo_passwords, _session_permission_whitelists,
                           _session_permission_once)
    for d in (_session_sudo_passwords, _session_permission_whitelists, _session_permission_once):
        d.clear()
    yield
    for d in (_session_sudo_passwords, _session_permission_whitelists, _session_permission_once):
        d.clear()


class _FakeStdin:
    def __init__(self):
        self.written = b""

    def write(self, data):
        self.written += data

    def flush(self):
        pass

    def close(self):
        pass


def _make_fake_popen(captured: dict, output: bytes = b"", returncode: int = 0):
    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["command"] = cmd
            captured["popen_kwargs"] = kwargs
            self.pid = 987654
            self.returncode = returncode
            self.stdin = _FakeStdin() if kwargs.get("stdin") is not None else None
            if self.stdin is not None:
                captured["stdin"] = self.stdin
            out = kwargs.get("stdout")
            if out is not None and output:
                out.write(output)
                out.flush()

        def poll(self):
            return self.returncode

        def kill(self):
            pass

    return _FakePopen


def _stub_shell(monkeypatch, tmp_path, captured, output=b"", returncode=0):
    monkeypatch.setattr(shell_mod, "_get_shell_output_dir", lambda: str(tmp_path))
    monkeypatch.setattr(shell_mod.subprocess, "Popen",
                        _make_fake_popen(captured, output=output, returncode=returncode))


class TestOnceGrantConsumedByShell:
    def test_once_grant_allows_command_and_is_consumed(self, monkeypatch, tmp_path):
        from api.state import _session_permission_once
        _session_permission_once[TEST_SID] = {"sudo apt-get update"}

        captured = {}
        _stub_shell(monkeypatch, tmp_path, captured, output=b"ok\n", returncode=0)
        tool = ShellTool()
        result = tool.execute(command="sudo apt-get update", timeout=5,
                              _sudo_password="pw", _session_id=TEST_SID)
        assert "Exit Code: 0" in result
        # 一次性授权已被消费——集合为空
        assert "sudo apt-get update" not in _session_permission_once.get(TEST_SID, set())

    def test_next_same_command_reblocks_after_consumption(self, monkeypatch, tmp_path):
        from api.state import _session_permission_once
        _session_permission_once[TEST_SID] = {"sudo apt-get update"}

        captured = {}
        _stub_shell(monkeypatch, tmp_path, captured,
                    output=b"sudo: a password is required\n", returncode=1)
        tool = ShellTool()
        # 第一次：一次性授权放行
        tool.execute(command="sudo apt-get update", timeout=5,
                     _sudo_password="pw", _session_id=TEST_SID)
        # 第二次：授权已消费，且无会话白名单 -> 重新弹窗（SandboxBlocked）
        with pytest.raises(SandboxBlocked) as exc_info:
            tool.execute(command="sudo apt-get update", timeout=5, _session_id=TEST_SID)
        assert exc_info.value.category == "sudo"

    def test_other_command_still_blocked_while_once_present(self, monkeypatch, tmp_path):
        from api.state import _session_permission_once
        _session_permission_once[TEST_SID] = {"sudo apt-get update"}

        captured = {}
        _stub_shell(monkeypatch, tmp_path, captured,
                    output=b"sudo: a password is required\n", returncode=1)
        tool = ShellTool()
        # 一次性授权只针对确切命令字符串；其它 sudo 命令仍弹窗
        with pytest.raises(SandboxBlocked) as exc_info:
            tool.execute(command="sudo systemctl restart x", timeout=5, _session_id=TEST_SID)
        assert exc_info.value.category == "sudo"
        # 未消费
        assert "sudo apt-get update" in _session_permission_once[TEST_SID]


class TestAgentApproveOnceWritesOnceStore:
    """agent._handle_sandbox_blocked 的 approve_once 路径：写一次性授权库，
    不写会话白名单。"""

    def _run_handle(self, agent, sb, progress_callback):
        out = {}
        def _call():
            out["result"] = agent._handle_sandbox_blocked(sb, "execute_shell", {}, progress_callback)
        t = threading.Thread(target=_call, daemon=True)
        t.start()
        return out, t

    def test_approve_once_sudo_writes_once_not_whitelist(self, monkeypatch):
        from api.state import _sandbox_waits, _session_permission_once, _session_permission_whitelists
        from agent.agent import OpenAGCAgent

        agent = OpenAGCAgent.__new__(OpenAGCAgent)
        agent.session_id = TEST_SID
        agent._session_permission_whitelist = set()
        agent._session_sandbox_whitelist = set()
        agent._session_network_whitelist = set()
        agent._session_sudo_password = ""
        agent._pending_sudo_password = ""
        agent.is_interrupted = False

        class _SB:
            sandbox_dir = "permission"
            category = "sudo"
            description = "需要 sudo 密码"
            path = "sudo apt-get update"
            tool_name = "execute_shell"

        captured_event = {}
        def _pcb(ev):
            captured_event.update(ev)

        out, t = self._run_handle(agent, _SB(), _pcb)
        # 等待 wait 注册，然后模拟用户点「授权本次」并输入密码
        deadline = time.time() + 5
        entry = None
        while time.time() < deadline:
            rid = captured_event.get("request_id")
            if rid and rid in _sandbox_waits:
                entry = _sandbox_waits[rid]
                break
            time.sleep(0.02)
        assert entry is not None, "sandbox wait not registered"
        entry["result"]["action"] = "approve_once"
        entry["result"]["password"] = "pw"
        entry["event"].set()
        t.join(timeout=5)

        assert out["result"] is None  # 返回 None 表示重试
        # 一次性授权库写入确切命令
        assert "sudo apt-get update" in _session_permission_once.get(TEST_SID, set())
        # 会话白名单不包含 sudo（否则后续不再弹窗）
        assert "sudo" not in agent._session_permission_whitelist
        assert "sudo" not in _session_permission_whitelists.get(TEST_SID, set())
        # 密码只进一次性 _pending_sudo_password（供本次重试），不进会话持久缓存
        assert agent._pending_sudo_password == "pw"
        assert agent._session_sudo_password == ""

    def test_approve_session_sudo_uses_whitelist(self, monkeypatch):
        from api.state import _sandbox_waits, _session_permission_whitelists
        from agent.agent import OpenAGCAgent

        agent = OpenAGCAgent.__new__(OpenAGCAgent)
        agent.session_id = TEST_SID
        agent._session_permission_whitelist = set()
        agent._session_sandbox_whitelist = set()
        agent._session_network_whitelist = set()
        agent._session_sudo_password = ""
        agent._pending_sudo_password = ""
        agent.is_interrupted = False

        class _SB:
            sandbox_dir = "permission"
            category = "sudo"
            description = "需要 sudo 密码"
            path = "sudo apt-get update"
            tool_name = "execute_shell"

        captured_event = {}
        def _pcb(ev):
            captured_event.update(ev)

        out, t = self._run_handle(agent, _SB(), _pcb)
        deadline = time.time() + 5
        entry = None
        while time.time() < deadline:
            rid = captured_event.get("request_id")
            if rid and rid in _sandbox_waits:
                entry = _sandbox_waits[rid]
                break
            time.sleep(0.02)
        assert entry is not None
        entry["result"]["action"] = "approve_session"
        entry["result"]["password"] = "pw"
        entry["event"].set()
        t.join(timeout=5)

        assert out["result"] is None
        # approve_session 才会写入会话白名单
        assert "sudo" in agent._session_permission_whitelist
        assert "sudo" in _session_permission_whitelists.get(TEST_SID, set())
