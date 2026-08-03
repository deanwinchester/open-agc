"""Tests for the download-visibility / source-probing fixes.

Covers:
1. tools/download.py preflight — 404/410/conn-fail rejected before any
   downloads record is created; 200/206/302/416 pass.
2. api/ws.py history loading — role='system' rows reach the agent as
   user-role 【系统通知】 messages (never as role='system').
3. routes_settings live injection — a running foreground/background agent
   receives the download notice via queue_message.
"""
import os
import sqlite3
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.download import DownloadTool, _preflight_download_url
from api.ws import _map_history_message, _load_session_history
from api.routes import routes_settings


class _FakeResp:
    """Minimal requests.Response stand-in for preflight tests."""

    def __init__(self, status_code=200):
        self.status_code = status_code

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _mock_get(monkeypatch, status_code=None, exc=None):
    import requests

    def fake_get(url, **kw):
        if exc is not None:
            raise exc
        return _FakeResp(status_code=status_code)

    monkeypatch.setattr(requests, "get", fake_get)


# ───────────────────────── 1. 下载前源探测（preflight） ─────────────────────────

class TestPreflight:
    def test_404_rejected(self, monkeypatch):
        _mock_get(monkeypatch, status_code=404)
        err = _preflight_download_url("https://example.com/missing.bin")
        assert err is not None
        assert "文件不存在或源不可用，换源前请先验证文件是否存在" in err

    def test_410_rejected(self, monkeypatch):
        _mock_get(monkeypatch, status_code=410)
        assert _preflight_download_url("https://example.com/gone.bin") is not None

    def test_302_allowed(self, monkeypatch):
        _mock_get(monkeypatch, status_code=302)
        assert _preflight_download_url("https://example.com/redir.bin") is None

    def test_416_means_file_exists(self, monkeypatch):
        # Servers that reject Range requests still prove the file exists.
        _mock_get(monkeypatch, status_code=416)
        assert _preflight_download_url("https://example.com/no-range.bin") is None

    @pytest.mark.parametrize("code", [200, 206, 301, 307, 308])
    def test_success_codes_allowed(self, monkeypatch, code):
        _mock_get(monkeypatch, status_code=code)
        assert _preflight_download_url("https://example.com/ok.bin") is None

    def test_connection_error_rejected(self, monkeypatch):
        import requests
        _mock_get(monkeypatch, exc=requests.ConnectionError("refused"))
        err = _preflight_download_url("https://unreachable.example.com/x.bin")
        assert err is not None
        assert "文件不存在或源不可用，换源前请先验证文件是否存在" in err

    def test_timeout_rejected(self, monkeypatch):
        import requests
        _mock_get(monkeypatch, exc=requests.Timeout("timed out"))
        assert _preflight_download_url("https://slow.example.com/x.bin") is not None

    def test_execute_404_queues_nothing(self, monkeypatch):
        """Preflight failure must not create any downloads record."""
        _mock_get(monkeypatch, status_code=404)
        tripwire = MagicMock(
            side_effect=AssertionError("create_download_record must not be called"))
        monkeypatch.setattr(routes_settings, "create_download_record", tripwire)
        tool = DownloadTool()
        result = tool.execute(
            url="https://example.com/definitely-missing-preflight-test.bin",
            filename="definitely-missing-preflight-test.bin",
            source="direct",
        )
        assert "文件不存在或源不可用，换源前请先验证文件是否存在" in result
        assert tripwire.call_count == 0


# ───────────────────────── 2. 历史加载 system→user 映射 ─────────────────────────

class TestHistoryMapping:
    def test_system_mapped_to_user_notice(self):
        msg = _map_history_message("system", "✅ 下载完成: model.gguf")
        assert msg["role"] == "user"
        assert msg["content"].startswith("【系统通知】")
        assert "下载完成" in msg["content"]

    def test_system_not_double_prefixed(self):
        msg = _map_history_message("system", "【系统通知】后台下载任务已完成")
        assert msg["role"] == "user"
        assert msg["content"].count("【系统通知】") == 1

    def test_agent_maps_to_assistant(self):
        assert _map_history_message("agent", "hi") == {"role": "assistant", "content": "hi"}

    def test_tool_step_skipped(self):
        assert _map_history_message("tool_step", "internal") is None

    def test_user_passthrough(self):
        assert _map_history_message("user", "q") == {"role": "user", "content": "q"}

    def test_load_session_history_includes_system_notices(self, tmp_path, monkeypatch):
        db = str(tmp_path / "history.db")
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT, content TEXT, session_id INTEGER, "
            "attachments TEXT)")
        conn.executemany(
            "INSERT INTO messages (role, content, session_id) VALUES (?, ?, 1)",
            [
                ("user", "帮我下载模型"),
                ("agent", "好的，已排队"),
                ("tool_step", "queue_download(...)"),
                ("system", "❌ 下载失败: m.gguf\n错误信息: HTTP 404"),
            ])
        conn.commit()
        conn.close()

        monkeypatch.setattr("api.ws.db_connect", lambda: sqlite3.connect(db))
        history = _load_session_history(1)

        roles = [m["role"] for m in history]
        # No raw system role may reach the LLM context
        assert "system" not in roles
        # The system notice shows up exactly once, as a 【系统通知】 user message
        notices = [m for m in history
                   if m["role"] == "user" and m["content"].startswith("【系统通知】")]
        assert len(notices) == 1
        assert "下载失败" in notices[0]["content"]
        assert "HTTP 404" in notices[0]["content"]
        # tool_step rows stay excluded; user/agent pass through
        assert {"role": "user", "content": "帮我下载模型"} in history
        assert {"role": "assistant", "content": "好的，已排队"} in history
        assert all("queue_download" not in m["content"] for m in history)


# ───────────────────────── 3. 运行中 agent 实时注入 ─────────────────────────

class TestLiveInjection:
    def test_foreground_agent_receives_notice(self):
        from api.state import _active_agents
        agent = MagicMock()
        agent.is_interrupted = False
        _active_agents.setdefault(701, {})[42] = agent
        try:
            ok = routes_settings._inject_notice_to_running_agent(42, 701, "【系统通知】下载失败: x")
            assert ok is True
            agent.queue_message.assert_called_once_with("【系统通知】下载失败: x")
        finally:
            _active_agents.pop(701, None)

    def test_foreground_agent_registered_under_zero_key(self):
        # Agent registered before its task id was known (key 0) — the session
        # fallback must still reach it.
        from api.state import _active_agents
        agent = MagicMock()
        agent.is_interrupted = False
        _active_agents.setdefault(702, {})[0] = agent
        try:
            ok = routes_settings._inject_notice_to_running_agent(55, 702, "【系统通知】下载完成: y")
            assert ok is True
            agent.queue_message.assert_called_once_with("【系统通知】下载完成: y")
        finally:
            _active_agents.pop(702, None)

    def test_background_agent_receives_notice(self):
        from api.state import _background_agents
        agent = MagicMock()
        agent.is_interrupted = False
        _background_agents[43] = agent
        try:
            ok = routes_settings._inject_notice_to_running_agent(43, 703, "n")
            assert ok is True
            agent.queue_message.assert_called_once_with("n")
        finally:
            _background_agents.pop(43, None)

    def test_interrupted_agent_skipped(self):
        from api.state import _active_agents
        agent = MagicMock()
        agent.is_interrupted = True
        _active_agents.setdefault(704, {})[44] = agent
        try:
            ok = routes_settings._inject_notice_to_running_agent(44, 704, "n")
            assert ok is False
            agent.queue_message.assert_not_called()
        finally:
            _active_agents.pop(704, None)

    def test_no_running_agent_returns_false(self):
        assert routes_settings._inject_notice_to_running_agent(999999, 999999, "n") is False

    def test_interrupted_task_match_falls_through_to_live_session_agent(self):
        # task_id key hits an interrupted agent — the scan must continue to
        # the session's other live agents (e.g. registered under key 0).
        from api.state import _active_agents
        interrupted = MagicMock()
        interrupted.is_interrupted = True
        live = MagicMock()
        live.is_interrupted = False
        _active_agents[705] = {42: interrupted, 0: live}
        try:
            ok = routes_settings._inject_notice_to_running_agent(42, 705, "【系统通知】下载完成: z")
            assert ok is True
            interrupted.queue_message.assert_not_called()
            live.queue_message.assert_called_once_with("【系统通知】下载完成: z")
        finally:
            _active_agents.pop(705, None)


# ───────────── 4. 【系统通知】不被插入判定协议丢弃 ─────────────

def _bare_agent():
    """OpenAGCAgent instance without heavy __init__ (DBs, MCP, LLM client)."""
    from agent.agent import OpenAGCAgent
    agent = OpenAGCAgent.__new__(OpenAGCAgent)
    agent.pending_messages = []
    agent._processing_interjection = False
    agent._interjection_stuck_count = 0
    return agent


class TestSystemNoticeBypass:
    def test_system_notice_injected_directly_never_rejected(self):
        agent = _bare_agent()
        agent.pending_messages = ["【系统通知】下载失败: m.gguf\n错误信息: HTTP 404"]
        injected = agent._check_pending_messages("当前任务")
        # Notice content is injected for the current run...
        assert "【系统通知】下载失败: m.gguf" in injected
        assert "HTTP 404" in injected
        # ...deterministically: popped at once, no accept/reject/ask protocol
        assert agent.pending_messages == []
        assert agent._processing_interjection is False
        assert "user_interjection_response" not in injected

    def test_user_interjection_still_uses_judgment_protocol(self):
        # Non-notice messages keep the original peek + judgment behavior.
        agent = _bare_agent()
        agent.pending_messages = ["帮我订一张机票"]
        injected = agent._check_pending_messages("当前任务")
        assert "[用户插入: 帮我订一张机票]" in injected
        assert "user_interjection_response" in injected
        assert agent._processing_interjection is True
        assert agent.pending_messages == ["帮我订一张机票"]  # peek, not popped

    def test_notice_queued_behind_interjection_not_disturbed(self):
        # While an interjection is under judgment, nothing new is injected.
        agent = _bare_agent()
        agent.pending_messages = ["普通插入", "【系统通知】下载完成: a.gguf"]
        agent._processing_interjection = True
        agent._interjection_stuck_count = 0
        assert agent._check_pending_messages("当前任务") == ""
        assert agent.pending_messages == ["普通插入", "【系统通知】下载完成: a.gguf"]
