"""
ask_user timeout-pause tests.

Covers:
- wait_for_user_input timeout -> raises TaskPaused (background pause) instead
  of setting is_interrupted / killing the task; default timeout is 1800s
- resume_task_with_late_answer -> injects the late answer (with
  "do not re-ask" guidance) into a backgrounded task's context and resumes
  via claim_task_for_resume CAS + _run_background_task; terminal states
  return explicit statuses instead of 404
- REST POST /api/tasks/{id}/reply -> falls back to inject+resume when no
  live agent holds the task's queue

All tests stub the DB layer, so no database, API keys, or network needed.
"""
import asyncio
import os
import queue
import sys
import types

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import agent.agent as agent_module  # noqa: E402
import api.background as bg  # noqa: E402
from agent.agent import OpenAGCAgent  # noqa: E402
from tools.interaction import TaskPaused  # noqa: E402


def _bare_agent():
    """Bare OpenAGCAgent with just the attributes wait_for_user_input needs."""
    agent = OpenAGCAgent.__new__(OpenAGCAgent)
    agent.is_interrupted = False
    agent.user_input_queue = queue.Queue()
    agent.progress_callback = None
    return agent


# ── wait_for_user_input: timeout pauses instead of killing ──

class TestWaitTimeoutPause:
    def test_timeout_raises_task_paused_not_interrupt(self):
        agent = _bare_agent()
        agent._user_input_timeout = 0.2
        with pytest.raises(TaskPaused) as exc_info:
            agent.wait_for_user_input("要删除哪个文件？")
        msg = str(exc_info.value)
        assert "等待用户回答超时" in msg
        assert "要删除哪个文件？" in msg  # question embedded for later matching
        assert agent.is_interrupted is False

    def test_default_timeout_is_1800s(self, monkeypatch):
        """With no _user_input_timeout override the deadline is 1800s:
        a check at t+1799.9 must NOT trip, a check at t+1800.1 must."""
        agent = _bare_agent()
        times = iter([1000.0, 1000.0 + 1799.9, 1000.0 + 1800.1])
        calls = []

        def fake_time():
            v = next(times)
            calls.append(v)
            return v

        monkeypatch.setattr(agent_module, "_time",
                            types.SimpleNamespace(time=fake_time))
        with pytest.raises(TaskPaused):
            agent.wait_for_user_input("q?")
        # 3 time() calls: deadline, first check (no trip), second check (trip)
        assert len(calls) == 3

    def test_answer_still_returned_before_timeout(self):
        import threading
        agent = _bare_agent()
        agent._user_input_timeout = 30.0

        def responder():
            import time as _t
            _t.sleep(0.2)
            agent.user_input_queue.put("用户答案")

        threading.Thread(target=responder, daemon=True).start()
        assert agent.wait_for_user_input("q?") == "用户答案"


# ── resume_task_with_late_answer ──

class _FakeConn:
    def __init__(self, row):
        self._row = row

    def execute(self, sql, params=()):
        return self

    def fetchone(self):
        return self._row

    def close(self):
        pass


@pytest.fixture
def patched_bg(monkeypatch):
    """Stub out every DB/thread touchpoint of resume_task_with_late_answer."""
    state = {
        "row": ("backgrounded", "原始任务",
                "Task paused: 等待用户回答超时，任务转入后台挂起。"
                "请回答此前的问题后任务自动恢复。问题: 要删除哪个文件？",
                "backgrounded"),
        "ctx": [{"role": "user", "content": "原始任务"}],
        "saved": None,
        "claimed": None,
        "status_updates": [],
        "ran": None,
    }
    monkeypatch.setattr(bg, "db_connect", lambda: _FakeConn(state["row"]))
    monkeypatch.setattr(
        bg, "claim_task_for_resume",
        lambda tid, allowed: state.update(claimed=(tid, tuple(allowed))) is None or True)
    monkeypatch.setattr(bg, "get_task_context",
                        lambda tid: list(state["ctx"]) if state["ctx"] is not None else None)
    monkeypatch.setattr(bg, "save_task_context",
                        lambda tid, ctx: state.update(saved=ctx))
    monkeypatch.setattr(
        bg, "update_task_status",
        lambda tid, s, summary=None, interruption_reason=None:
        state["status_updates"].append((tid, s, interruption_reason)))

    class _FakeThread:
        def __init__(self, target=None, args=(), daemon=None):
            state["ran"] = (target, args)

        def start(self):
            pass

    monkeypatch.setattr(bg, "threading", types.SimpleNamespace(Thread=_FakeThread))
    return state


class TestResumeWithLateAnswer:
    def test_backgrounded_task_injected_and_resumed(self, patched_bg):
        result = bg.resume_task_with_late_answer(7, "删 a.txt")
        assert result["ok"] is True
        assert result["status"] == "resumed"
        # CAS claim happened before the resume thread was spawned
        assert patched_bg["claimed"] == (7, ("backgrounded", "interrupted"))
        # Answer injected into context with anti-re-ask guidance + question
        saved = patched_bg["saved"]
        assert saved is not None
        injected = saved[-1]
        assert injected["role"] == "user"
        assert "用户已回答" in injected["content"]
        assert "删 a.txt" in injected["content"]
        assert "要删除哪个文件？" in injected["content"]  # recovered question
        assert "不要重复提问" in injected["content"]
        # Resume thread runs _run_background_task as a resume (is_resume=True)
        target, args = patched_bg["ran"]
        assert target is bg._run_background_task
        assert args[0] == 7 and args[1] == "原始任务" and args[3] is True
        # Status flipped to interrupted/background_complete for the resume path
        assert (7, "interrupted", "background_complete") in patched_bg["status_updates"]

    def test_completed_task_returns_terminal_status(self, patched_bg):
        patched_bg["row"] = ("completed", "q", "", None)
        result = bg.resume_task_with_late_answer(7, "答案")
        assert result["ok"] is False
        assert result["error"] == "terminal"
        assert result["status"] == "completed"
        assert patched_bg["claimed"] is None
        assert patched_bg["saved"] is None

    def test_failed_task_returns_terminal_status(self, patched_bg):
        patched_bg["row"] = ("failed", "q", "", None)
        result = bg.resume_task_with_late_answer(7, "答案")
        assert result["ok"] is False
        assert result["error"] == "terminal"
        assert result["status"] == "failed"

    def test_not_found(self, patched_bg):
        patched_bg["row"] = None
        result = bg.resume_task_with_late_answer(999, "答案")
        assert result["ok"] is False
        assert result["error"] == "not_found"

    def test_running_task_not_resumed(self, patched_bg):
        patched_bg["row"] = ("running", "q", "", None)
        result = bg.resume_task_with_late_answer(7, "答案")
        assert result["ok"] is False
        assert result["error"] == "running"
        assert patched_bg["claimed"] is None

    def test_user_interrupted_task_not_resumed(self, patched_bg):
        patched_bg["row"] = ("interrupted", "q", "", "user")
        result = bg.resume_task_with_late_answer(7, "答案")
        assert result["ok"] is False
        assert result["error"] == "user_interrupted"
        assert patched_bg["claimed"] is None

    def test_claim_failure_does_not_inject(self, patched_bg, monkeypatch):
        monkeypatch.setattr(bg, "claim_task_for_resume", lambda tid, allowed: False)
        result = bg.resume_task_with_late_answer(7, "答案")
        assert result["ok"] is False
        assert result["error"] == "claim_failed"
        assert patched_bg["saved"] is None
        assert patched_bg["ran"] is None


# ── REST POST /api/tasks/{id}/reply fallback ──

class TestRestReplyFallback:
    def test_no_live_agent_triggers_inject_and_resume(self, monkeypatch):
        import api.state as state
        from api.routes import routes_tasks
        state._background_agents.pop(42, None)  # ensure no live agent
        called = {}
        monkeypatch.setattr(
            "api.background.resume_task_with_late_answer",
            lambda tid, ans: called.update(tid=tid, ans=ans)
            or {"ok": True, "status": "resumed", "message": "ok"})
        result = asyncio.run(
            routes_tasks.reply_to_background_task(42, {"answer": "删 a.txt"}))
        assert result["status"] == "success"
        assert result["resumed"] is True
        assert called == {"tid": 42, "ans": "删 a.txt"}

    def test_live_agent_gets_queue_delivery(self, monkeypatch):
        import api.state as state
        from api.routes import routes_tasks
        delivered = []
        fake_agent = types.SimpleNamespace(
            is_interrupted=False,
            user_input_queue=types.SimpleNamespace(
                put_nowait=lambda a: delivered.append(a)))
        state._background_agents[43] = fake_agent
        try:
            def _boom(*a, **k):
                raise AssertionError("resume helper must not run for live agent")
            monkeypatch.setattr("api.background.resume_task_with_late_answer", _boom)
            result = asyncio.run(
                routes_tasks.reply_to_background_task(43, {"answer": "x"}))
            assert result["status"] == "success"
            assert delivered == ["x"]
        finally:
            state._background_agents.pop(43, None)

    def test_terminal_task_returns_status_not_404(self, monkeypatch):
        import api.state as state
        from api.routes import routes_tasks
        state._background_agents.pop(44, None)
        monkeypatch.setattr(
            "api.background.resume_task_with_late_answer",
            lambda tid, ans: {"ok": False, "error": "terminal",
                              "status": "completed", "message": "任务已完成"})
        result = asyncio.run(
            routes_tasks.reply_to_background_task(44, {"answer": "x"}))
        assert result["status"] == "terminal"
        assert result["task_status"] == "completed"

    def test_unknown_task_still_404(self, monkeypatch):
        import api.state as state
        from api.routes import routes_tasks
        from fastapi import HTTPException
        state._background_agents.pop(999, None)
        monkeypatch.setattr(
            "api.background.resume_task_with_late_answer",
            lambda tid, ans: {"ok": False, "error": "not_found",
                              "message": "任务不存在"})
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(routes_tasks.reply_to_background_task(999, {"answer": "x"}))
        assert exc_info.value.status_code == 404
