"""
Agent main-loop reliability tests (stage 4, task 1).

Covers:
- LLM call failure / empty choices -> cleanup path still runs (no escape from run_turn)
- Delegation with missing plan keys -> no KeyError; unexecuted subtasks reported
- Interjection accept/reject index -> user interjection message, not assistant tool_call
- Interjection stuck timeout -> message injected instead of silently dropped
- Post-process serial queue -> jobs run one at a time, in order, on snapshots
- wait_for_user_input -> interrupt + total timeout instead of blocking forever

All tests use a bare agent instance (``__new__``) plus stub LLM clients, so no
API keys, databases, or network access are required.
"""
import gc
import json
import os
import queue
import sys
import threading
import time
import types
import weakref

import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import agent.agent as agent_module  # noqa: E402
from agent.agent import OpenAGCAgent  # noqa: E402
from tools.interaction import UserInterjectionResponseTool  # noqa: E402


# ── Stub LLM helpers ──

class _StubMessage:
    def __init__(self, content=None, tool_calls=None):
        self.role = "assistant"
        self.content = content
        self.tool_calls = tool_calls


class _StubResponse:
    def __init__(self, message=None, choices=None):
        self.choices = choices if choices is not None else [types.SimpleNamespace(message=message)]
        self.usage = None


class StubLLM:
    """Scripted LLM client: pops one item per chat() call; Exception items are raised."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.default_model = "stub-model"

    def chat(self, messages=None, tools=None, interrupt_check=None):
        self.calls.append({"messages": messages, "tools": tools})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, "stub-model"


def _tool_call(name, arguments: dict):
    return types.SimpleNamespace(
        id="call_1",
        type="function",
        function=types.SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _text_response(text):
    return _StubResponse(_StubMessage(content=text))


def _tool_response(name, arguments):
    return _StubResponse(_StubMessage(tool_calls=[_tool_call(name, arguments)]))


def _bare_agent(**overrides):
    """Create an OpenAGCAgent without running heavy __init__ (DBs, MCP, LLM client)."""
    agent = OpenAGCAgent.__new__(OpenAGCAgent)
    agent.session_id = None
    agent.failed_attempts = []
    agent.messages = [{"role": "system", "content": "sys"}]
    agent.logger = None
    agent.llm = StubLLM([])
    agent.pending_messages = []
    agent._processing_interjection = False
    agent._interjection_stuck_count = 0
    agent._rejected_interjection = None
    agent._in_self_review = False
    agent._max_correction_attempts = 0
    agent.tool_schemas = []
    agent.tool_display_names = {}
    agent.available_tools = {"user_interjection_response": UserInterjectionResponseTool()}
    agent.full_available_tools = {}
    agent._session_sandbox_whitelist = set()
    agent._session_network_whitelist = set()
    agent._session_permission_whitelist = set()
    agent._pending_sudo_password = ""
    agent._session_sudo_password = ""
    agent.reflection_engine = None
    agent.knowledge_graph = types.SimpleNamespace(extract_from_messages=lambda msgs: None)
    agent._save_task_stats = lambda *a, **k: None
    agent.user_input_queue = queue.Queue()
    agent.progress_callback = None
    # System-prompt rebuild touches skills/memory stores — stub it out
    agent._build_system_prompt = lambda **kwargs: "sys"
    for key, value in overrides.items():
        setattr(agent, key, value)
    return agent


@pytest.fixture(autouse=True)
def _no_adaptive_writes(monkeypatch):
    """Keep adaptive tool-usage stats from writing to the real data dir."""
    monkeypatch.setattr("tools.adaptive.record_tool_call", lambda *a, **k: None)


# ── Fix 1: LLM call exception protection ──

class TestLLMCallProtection:
    def test_llm_exception_runs_cleanup_and_returns_error(self):
        agent = _bare_agent()
        agent.llm = StubLLM([ConnectionError("network down")])
        saved = []
        kg_calls = []
        agent._save_task_stats = lambda cat, iters, success: saved.append(success)
        agent.knowledge_graph = types.SimpleNamespace(
            extract_from_messages=lambda msgs: kg_calls.append(len(msgs)))

        result = agent.run_turn("随便一个任务", verbose=False, skip_rag=True)

        assert result.startswith("[LLM_ERROR]")
        # 用户态文案净化：原始异常全文（含请求 dump）不上屏，只留类型名
        assert "ConnectionError" in result
        assert "network down" not in result
        assert saved == [False], "task stats must be saved as failure"
        assert kg_calls, "KG extraction must run on LLM failure"

    def test_llm_empty_choices_treated_as_failure(self):
        agent = _bare_agent()
        agent.llm = StubLLM([_StubResponse(choices=[])])
        saved = []
        agent._save_task_stats = lambda cat, iters, success: saved.append(success)

        result = agent.run_turn("随便一个任务", verbose=False, skip_rag=True)

        assert result.startswith("[LLM_ERROR]")
        assert "ValueError" in result
        assert saved == [False]


# ── Fix 2: delegation robustness ──

class TestDelegation:
    def _make_agent(self, monkeypatch, plans, sub_success, sub_tool_calls=1):
        agent = _bare_agent()
        # 本组测试针对旧委派路径（dispatcher_mode 关闭语义）——强制模式关闭，
        # 隔离运行环境真实 config.json 可能开启 dispatcher_mode 的影响。
        agent._dispatcher_mode_enabled = lambda: False
        agent._should_delegate = lambda text: True
        agent._decompose_task = lambda text: [dict(p) for p in plans]
        seen_tasks = []

        class FakeSubAgent:
            def __init__(self, task=None, **kwargs):
                self.task = task
                seen_tasks.append(task)

            def run(self):
                return {"success": sub_success, "summary": f"done: {self.task}",
                        "duration": 0.1, "tool_calls": sub_tool_calls}

        monkeypatch.setattr("agent.agent.SubAgent", FakeSubAgent)
        # Reflection call: content=None -> run_turn falls back to the synthesis report
        agent.llm = StubLLM([_StubResponse(_StubMessage(content=None))])
        return agent, seen_tasks

    def test_missing_plan_keys_use_defaults_and_unresolvable_reported(self, monkeypatch):
        plans = [
            {"id": 1, "task": "任务A"},                     # normal
            {"depends_on": [1]},                             # missing id AND task
            {"id": 3, "task": "任务C", "depends_on": [99]},  # unresolvable dependency
        ]
        agent, seen_tasks = self._make_agent(monkeypatch, plans, sub_success=True)

        result = agent.run_turn("复杂任务", verbose=False, skip_rag=True)

        # No KeyError; plan missing keys ran with synthesized defaults
        assert "子任务 2" in seen_tasks
        # 新契约：委派不再以报告收尾，报告写入 messages，主代理继续执行
        assert agent._delegated_this_turn is True
        report = "\n".join(str(m.get("content", "")) for m in agent.messages)
        assert "未执行" in report
        assert "任务C" in report

    def test_failed_dependency_subtask_reported_as_unexecuted(self, monkeypatch):
        plans = [
            {"id": 1, "task": "失败的任务"},
            {"id": 2, "task": "依赖失败任务", "depends_on": [1]},
        ]
        agent, _ = self._make_agent(monkeypatch, plans, sub_success=False)

        result = agent.run_turn("复杂任务", verbose=False, skip_rag=True)

        report = "\n".join(str(m.get("content", "")) for m in agent.messages)
        assert "未执行" in report
        assert "依赖失败任务" in report

    def test_hallucinated_subtask_rejected_by_verification(self, monkeypatch):
        """子代理零工具调用假完成（幻觉编造内容仍报 success）→ 证据验收打回，
        不计入完成（生产实证 eval R7/R8：子代理编造小说句子/文件名）。"""
        plans = [{"id": 1, "task": "幻觉任务"}, {"id": 2, "task": "正常任务"}]
        agent, seen = self._make_agent(monkeypatch, plans, sub_success=True,
                                       sub_tool_calls=0)  # 零工具调用 = 空谈
        result = agent.run_turn("复杂任务", verbose=False, skip_rag=True)
        report = "\n".join(str(m.get("content", "")) for m in agent.messages)
        # 两个子任务都被验收打回（tool_calls=0），报告里带打回标记
        assert "验收打回" in report


# ── Fix 3: interjection index ──

class TestInterjectionIndex:
    def test_accept_marks_user_interjection_not_assistant_message(self, monkeypatch):
        agent = _bare_agent()
        # 动态段后置后，非空 goals 会以独立 system 消息插入——本测试断言
        # 消息绝对索引，stub 空 goals 使动态段不插入（与本测试主题无关）
        monkeypatch.setattr("tools.task_plan.load_goals", lambda: {"items": []})
        monkeypatch.setattr("tools.task_plan.format_goal_list_for_prompt", lambda g: "")
        agent.pending_messages = ["请把结果也发到群里"]
        agent.llm = StubLLM([
            _tool_response("user_interjection_response",
                           {"action": "accept", "response": "好的"}),
            _text_response("已完成，结果已发到群里。"),
        ])

        result = agent.run_turn("原始任务", verbose=False, skip_rag=True)

        assert result == "已完成，结果已发到群里。"
        # 动态段（含当前时间）永驻消息流——断言前过滤「系统补充上下文」用户消息
        core_msgs = [m for m in agent.messages
                     if not (m.get("role") == "user"
                             and "系统补充上下文" in str(m.get("content", "")))]
        # core: [system, user原始任务, user插话, assistant tool_call, tool result, assistant final]
        assert core_msgs[2]["role"] == "user"
        assert core_msgs[2]["content"].startswith("[用户插入已接受]")
        assert "好的" in core_msgs[2]["content"]
        # The assistant tool_call message must remain untouched
        assert core_msgs[3]["role"] == "assistant"
        assert core_msgs[3]["content"] == ""
        assert core_msgs[3]["tool_calls"]
        assert agent.pending_messages == []

    def test_reject_captures_user_interjection_content(self, monkeypatch):
        agent = _bare_agent()
        # 同上：stub 空 goals，动态段不插入，消息结构保持 system/user/assistant
        monkeypatch.setattr("tools.task_plan.load_goals", lambda: {"items": []})
        monkeypatch.setattr("tools.task_plan.format_goal_list_for_prompt", lambda g: "")
        agent.pending_messages = ["帮我订一张机票"]
        agent.llm = StubLLM([
            _tool_response("user_interjection_response",
                           {"action": "reject", "reason": "新话题"}),
            _text_response("主任务已完成"),
        ])

        result = agent.run_turn("原始任务", verbose=False, skip_rag=True)

        assert result.startswith("[INTERJECTION_REJECTED]")
        payload = json.loads(result.split("\n", 1)[0][len("[INTERJECTION_REJECTED] "):])
        assert "帮我订一张机票" in payload["message"]
        assert payload["reason"] == "新话题"
        assert "主任务已完成" in result
        # Interjection messages removed from context; only system/user/final remain
        # （过滤动态段：时间注入使动态 user 消息永驻消息流）
        core_roles = [m["role"] for m in agent.messages
                      if not (m.get("role") == "user"
                              and "系统补充上下文" in str(m.get("content", "")))]
        assert core_roles == ["system", "user", "assistant"]
        assert agent.pending_messages == []


# ── Fix 4: interjection stuck timeout ──

class TestInterjectionTimeout:
    def test_timeout_injects_message_instead_of_dropping(self):
        agent = _bare_agent()
        agent.pending_messages = ["超时未处理的消息"]
        agent._processing_interjection = True
        agent._interjection_stuck_count = 13

        injected = agent._check_pending_messages("当前任务")

        assert injected, "timed-out interjection must not be silently dropped"
        assert "超时未处理的消息" in injected
        assert agent.pending_messages == []
        assert agent._processing_interjection is False
        assert agent._interjection_stuck_count == 0


# ── Fix 6: post-process serial queue (module-level singleton worker) ──

class TestPostProcessQueue:
    def _drain(self, timeout=10.0):
        deadline = time.time() + timeout
        q = agent_module._post_process_queue
        while q.unfinished_tasks and time.time() < deadline:
            time.sleep(0.01)
        assert not q.unfinished_tasks, "post-process queue did not drain"

    def test_worker_runs_serially_in_order_on_snapshots(self):
        agent = _bare_agent()
        agent.reflection_engine = object()  # truthy: enqueue must not bail out
        agent.messages = [{"role": "user", "content": "m0"}]
        order = []
        active = []
        lock = threading.Lock()

        def fake_bg(task_input, duration, success, messages=None):
            with lock:
                active.append(task_input)
                assert len(active) == 1, "post-process jobs ran concurrently"
            time.sleep(0.01)
            with lock:
                order.append((task_input,
                              [m["content"] for m in messages if m["role"] == "user"]))
                active.pop()

        agent._background_post_process = fake_bg

        for i in range(5):
            agent._enqueue_post_process(f"task-{i}", 0.1, True)
            # Next turn starts mutating messages immediately after enqueue
            agent.messages.append({"role": "user", "content": f"m{i+1}"})

        self._drain()

        assert [t for t, _ in order] == [f"task-{i}" for i in range(5)]
        # Each job saw a snapshot taken at enqueue time (m0..mi), not later mutations
        for i, (_, contents) in enumerate(order):
            assert f"m{i}" in contents
            assert f"m{i+1}" not in contents

    def test_enqueue_skipped_without_reflection_engine(self):
        agent = _bare_agent()
        agent.reflection_engine = None
        before = agent_module._post_process_queue.qsize()
        agent._enqueue_post_process("t", 0.1, True)
        assert agent_module._post_process_queue.qsize() == before

    def test_singleton_worker_across_instances(self):
        """Many short-lived agents share exactly one post-process thread."""
        processed = []
        agents = []
        for i in range(3):
            a = _bare_agent()
            a.reflection_engine = object()
            a._background_post_process = (
                lambda task_input, duration, success, messages=None, _i=i:
                processed.append((_i, task_input)))
            agents.append(a)

        for i, a in enumerate(agents):
            a._enqueue_post_process(f"task-{i}", 0.1, True)
        self._drain()
        assert len(processed) == 3

        workers = [t for t in threading.enumerate() if t.name == "agent-post-process"]
        assert len(workers) == 1
        # Ensuring again must not spawn a second worker
        agent_module._ensure_post_process_worker()
        workers = [t for t in threading.enumerate() if t.name == "agent-post-process"]
        assert len(workers) == 1

    def test_agent_garbage_collected_after_queue_drains(self):
        """Once the job is processed, the worker drops its agent reference."""
        agent = _bare_agent()
        agent.reflection_engine = object()
        done = threading.Event()
        agent._background_post_process = lambda *a, **k: done.set()
        ref = weakref.ref(agent)

        agent._enqueue_post_process("t", 0.1, True)
        assert done.wait(5), "post-process job was not processed"
        self._drain()

        del agent  # the job tuple was the only other strong reference
        gc.collect()
        assert ref() is None, "agent still referenced after queue drained"


# ── Fix 5: wait_for_user_input ──

class TestWaitForUserInput:
    def test_returns_immediately_when_interrupted(self):
        agent = _bare_agent()
        agent.is_interrupted = True
        assert agent.wait_for_user_input("q?") == "[用户已中断任务]"

    def test_total_timeout_raises_task_paused(self):
        from tools.interaction import TaskPaused
        agent = _bare_agent()
        agent.is_interrupted = False
        agent._user_input_timeout = 0.2
        with pytest.raises(TaskPaused) as exc_info:
            agent.wait_for_user_input("q?")
        assert "等待用户回答超时" in str(exc_info.value)
        assert "q?" in str(exc_info.value)
        # Timeout must NOT kill the task — it pauses to background instead
        assert agent.is_interrupted is False

    def test_returns_user_answer(self):
        agent = _bare_agent()
        agent.is_interrupted = False

        def responder():
            time.sleep(0.2)
            agent.user_input_queue.put("用户答案")

        threading.Thread(target=responder, daemon=True).start()
        assert agent.wait_for_user_input("q?") == "用户答案"
