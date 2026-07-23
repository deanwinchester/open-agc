"""
阶段7 Task2（B2）：上下文防线真正生效

Covers:
1. 工具结果写入截断 — 按工具类型 cap（read_file/fetch_url 8000、
   execute_shell/execute_python 12000、其余 4000）；超出后写入 messages 的
   单条工具结果不超 cap（compress_tool_result + 硬截断兜底）
2. 后台 resume 快照全量 — messages[1:] 语义（全历史、去 system），
   save_task_context 防缩守卫保留
3. microcompact — 第一轮无条件写回 _timestamp，TTL 过后第二轮冷区工具
   结果被清
4. 预算按模型窗口解析 — litellm model_cost 的 max_input_tokens（llamacpp
   用 llamacpp_ctx_size），写入 agent 的 TokenBudget（config.context_budget
   优先）；ContextWindowExceeded 重试用解析值 × 0.9
5. plan 单次注入 — system prompt 中计划文本只出现一次
6. reasoning_content 剥离 — _build_model_kwargs 输出消息不含该键，
   且不改动调用方消息
7. failed_attempts 跨任务清空 — run_turn 开头随 _consecutive_failures 一起清

All tests run without API keys, real databases, or network access.
"""
import json
import os
import queue
import sqlite3
import sys
import time
import types

import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import agent.agent as agent_module  # noqa: E402
from agent.agent import OpenAGCAgent  # noqa: E402
from core.token_budget import TokenBudget  # noqa: E402
from tools.interaction import UserInterjectionResponseTool  # noqa: E402

_HAS_LITELLM = False
try:
    import litellm  # noqa: F401
    _HAS_LITELLM = True
except ImportError:
    pass

_litellm = pytest.mark.skipif(not _HAS_LITELLM, reason="requires litellm")


# ── Shared bare-agent helpers (same pattern as test_agent_reliability) ──

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
    """Scripted LLM client: pops one item per chat() call."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.default_model = "stub-model"

    def chat(self, messages=None, tools=None):
        self.calls.append({"messages": messages, "tools": tools})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, "stub-model"


def _text_response(text):
    return _StubResponse(_StubMessage(content=text))


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
    agent._build_system_prompt = lambda **kwargs: "sys"
    for key, value in overrides.items():
        setattr(agent, key, value)
    return agent


@pytest.fixture(autouse=True)
def _no_adaptive_writes(monkeypatch):
    """Keep adaptive tool-usage stats from writing to the real data dir."""
    monkeypatch.setattr("tools.adaptive.record_tool_call", lambda *a, **k: None)


# ── Item 1: 工具结果写入截断 ──

class TestToolResultWriteTruncation:
    """写入 messages 的单条工具结果不得超过按工具类型的 cap。"""

    def _agent(self):
        return OpenAGCAgent.__new__(OpenAGCAgent)

    def test_under_cap_returned_unchanged(self):
        agent = self._agent()
        short = "ok\n" * 100  # 300 chars, well under every cap
        for tool in ("read_file", "fetch_url", "execute_shell",
                     "execute_python", "write_file", "whatever"):
            assert agent._truncate_tool_result_for_context(short, tool) == short

    @pytest.mark.parametrize("tool,cap", [
        ("read_file", 8000),
        ("fetch_url", 8000),
        ("execute_shell", 12000),
        ("execute_python", 12000),
        ("write_file", 4000),   # 其余工具走默认 cap
        ("some_random_tool", 4000),
    ])
    def test_multi_line_result_capped(self, tool, cap):
        agent = self._agent()
        # 1000 行 × ~60 字符 ≈ 60k，远超所有 cap
        big = "\n".join(f"line {i} " + "x" * 50 for i in range(1000))
        out = agent._truncate_tool_result_for_context(big, tool)
        assert len(out) <= cap

    @pytest.mark.parametrize("tool,cap", [
        ("read_file", 8000),
        ("execute_shell", 12000),
        ("some_random_tool", 4000),
    ])
    def test_single_long_line_hard_capped(self, tool, cap):
        """单行超长结果：压缩器无从下手（甚至 counterproductive），
        硬截断兜底保证仍不超 cap。"""
        agent = self._agent()
        big = "y" * 50000  # 单行
        out = agent._truncate_tool_result_for_context(big, tool)
        assert len(out) <= cap

    def test_oversized_result_compressed_before_cap(self):
        """超出 cap 的多行 shell 输出先走压缩（保留头/尾信息），非简单砍头。"""
        agent = self._agent()
        big = "\n".join(f"line {i} " + "x" * 50 for i in range(1000))
        out = agent._truncate_tool_result_for_context(big, "execute_shell")
        assert len(out) <= 12000
        assert "line 0" in out  # 头部保留

    def test_write_caps_constants(self):
        assert OpenAGCAgent._TOOL_RESULT_WRITE_CAPS == {
            "read_file": 8000,
            "fetch_url": 8000,
            "execute_shell": 12000,
            "execute_python": 12000,
        }
        assert OpenAGCAgent._TOOL_RESULT_WRITE_CAP_DEFAULT == 4000


# ── Item 2: 后台 resume 快照全量 ──

class TestBackgroundSnapshotFull:
    """后台任务快照与 ws 一致取 messages[1:]：全历史、去 system；
    save_task_context 的防缩守卫保持有效。"""

    def _setup_db(self, monkeypatch, tmp_path, existing_snapshot=None):
        db_path = str(tmp_path / "chat_history.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, "
                     "context_snapshot TEXT, updated_at TEXT)")
        conn.execute("INSERT INTO tasks (id, context_snapshot) VALUES (?, ?)",
                     (1, json.dumps(existing_snapshot) if existing_snapshot else None))
        conn.commit()
        conn.close()
        monkeypatch.setattr("api.task_core.db_connect",
                            lambda: sqlite3.connect(db_path))
        return db_path

    def _read_snapshot(self, db_path):
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT context_snapshot FROM tasks WHERE id=1").fetchone()
        conn.close()
        return json.loads(row[0]) if row and row[0] else None

    def test_snapshot_is_full_history_minus_system(self, monkeypatch, tmp_path):
        """messages[1:] 语义：保留全部历史轮次（非仅本轮新增），去掉 system。"""
        db_path = self._setup_db(monkeypatch, tmp_path)
        # 模拟 resume 后的 agent.messages：system + 24 条历史 + 本轮 5 条
        messages = ([{"role": "system", "content": "sys"}]
                    + [{"role": "user", "content": f"历史{i}"} for i in range(24)]
                    + [{"role": "user", "content": f"本轮新消息{i}"} for i in range(5)])
        snapshot = messages[1:]  # background.py 现在使用的表达式
        from api.task_core import save_task_context
        save_task_context(1, snapshot)
        saved = self._read_snapshot(db_path)
        assert len(saved) == 29  # 24 历史 + 5 新增——不缩水
        assert all(m["role"] != "system" for m in saved)

    def test_anti_shrink_guard_still_blocks_tiny_overwrite(self, monkeypatch, tmp_path):
        """防缩守卫保留：新快照 <10 条且不足旧的一半时拒绝覆盖。"""
        big_old = [{"role": "user", "content": f"old{i}"} for i in range(30)]
        db_path = self._setup_db(monkeypatch, tmp_path, existing_snapshot=big_old)
        from api.task_core import save_task_context
        save_task_context(1, [{"role": "user", "content": "tiny"}])
        saved = self._read_snapshot(db_path)
        assert len(saved) == 30  # 旧快照未被小快照覆盖

    def test_full_snapshot_overwrites_normally(self, monkeypatch, tmp_path):
        """全量快照（>=10 条）正常覆盖旧快照。"""
        old = [{"role": "user", "content": f"old{i}"} for i in range(12)]
        db_path = self._setup_db(monkeypatch, tmp_path, existing_snapshot=old)
        new = [{"role": "user", "content": f"new{i}"} for i in range(15)]
        from api.task_core import save_task_context
        save_task_context(1, new)
        saved = self._read_snapshot(db_path)
        assert len(saved) == 15
        assert saved[0]["content"] == "new0"


# ── Item 3: microcompact 两轮后冷区清除 ──

class TestMicrocompactTwoRounds:
    """第一轮（无条件写回）补齐 _timestamp；TTL 过后第二轮据此清冷区。"""

    def _messages(self):
        return [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "任务"},
            {"role": "assistant", "content": "调用工具"},
            {"role": "tool", "tool_call_id": "t1", "name": "execute_shell",
             "content": "z" * 3000},  # >2000 才会被冷区清理
            {"role": "assistant", "content": "继续"},
        ]

    def test_round1_assigns_timestamps_without_clearing(self):
        tb = TokenBudget()
        msgs = self._messages()
        round1 = tb.time_based_microcompact(msgs, ttl=60)
        # 返回新列表（agent 无条件 self.messages = compacted 的前提）
        assert round1 is not msgs
        # 所有消息补齐 _timestamp
        assert all("_timestamp" in m for m in round1)
        # 新鲜消息不清（TTL 未过）
        tool_msg = next(m for m in round1 if m["role"] == "tool")
        assert tool_msg["content"] == "z" * 3000

    def test_round2_clears_cold_tool_results(self):
        """模拟 agent 无条件写回：round1 结果作为下一轮输入，
        TTL 过后冷区超大工具结果被替换为占位符。"""
        tb = TokenBudget()
        msgs = tb.time_based_microcompact(self._messages(), ttl=60)
        # 时间流逝：所有时间戳老化到 TTL 之前
        for m in msgs:
            m["_timestamp"] = time.time() - 3600
        round2 = tb.time_based_microcompact(msgs, ttl=60)
        tool_msg = next(m for m in round2 if m["role"] == "tool")
        assert tool_msg["content"].startswith("[Old tool result content cleared")
        # 非 tool 消息不受影响
        assert round2[1]["content"] == "任务"

    def test_small_tool_result_in_cold_region_kept(self):
        """冷区里 <=2000 字符的 tool 结果不属于'超大'，不清理。"""
        tb = TokenBudget()
        msgs = self._messages()
        msgs[3]["content"] = "short result"
        msgs = tb.time_based_microcompact(msgs, ttl=60)
        for m in msgs:
            m["_timestamp"] = time.time() - 3600
        round2 = tb.time_based_microcompact(msgs, ttl=60)
        tool_msg = next(m for m in round2 if m["role"] == "tool")
        assert tool_msg["content"] == "short result"


# ── Item 4: 预算按模型窗口解析 ──

@_litellm
class TestContextWindowResolution:
    """LLMClient 初始化按模型解析上下文窗口（mock litellm model_cost）。"""

    def _client(self, monkeypatch, model, config=None, model_cost=None):
        import core.llm_client as llm_mod
        monkeypatch.setattr(llm_mod, "load_config", lambda: config or {})
        if model_cost is not None:
            monkeypatch.setattr(llm_mod.litellm, "model_cost", model_cost)
        return llm_mod.LLMClient(default_model=model)

    def test_max_input_tokens_from_model_cost(self, monkeypatch):
        client = self._client(
            monkeypatch, "test-model-alpha",
            model_cost={"test-model-alpha": {"max_input_tokens": 262144}})
        assert client.model_context_window == 262144

    def test_provider_prefix_stripped_for_lookup(self, monkeypatch):
        client = self._client(
            monkeypatch, "openai/test-model-beta",
            model_cost={"test-model-beta": {"max_input_tokens": 200000}})
        assert client.model_context_window == 200000

    def test_falls_back_to_max_tokens(self, monkeypatch):
        client = self._client(
            monkeypatch, "test-model-gamma",
            model_cost={"test-model-gamma": {"max_tokens": 64000}})
        assert client.model_context_window == 64000

    def test_llamacpp_uses_ctx_size(self, monkeypatch):
        client = self._client(
            monkeypatch, "llamacpp/qwen-x",
            config={"llamacpp_ctx_size": 65536},
            model_cost={})  # model_cost 不应被查询
        assert client.model_context_window == 65536

    def test_unknown_model_returns_zero(self, monkeypatch):
        client = self._client(monkeypatch, "no/such-model-anywhere",
                              model_cost={})
        assert client.model_context_window == 0

    def test_context_exceeded_retry_uses_resolved_window(self, monkeypatch):
        """ContextWindowExceeded 重试：max_tokens = 解析窗口 × 0.9（替代硬编码 1000000）。"""
        import core.llm_client as llm_mod
        from litellm.exceptions import ContextWindowExceededError

        client = self._client(
            monkeypatch, "test-model-alpha",
            model_cost={"test-model-alpha": {"max_input_tokens": 262144}})
        monkeypatch.setattr(llm_mod, "_log_model_call", lambda *a, **k: None)

        captured = {}

        def fake_truncate(messages, max_tokens=4096):
            captured["max_tokens"] = max_tokens
            return [messages[0], messages[-1]]

        monkeypatch.setattr(client, "_truncate_for_context", fake_truncate)

        calls = []

        def fake_completion(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise ContextWindowExceededError(
                    "context window exceeded",
                    model="test-model-alpha", llm_provider="openai")
            usage = types.SimpleNamespace(
                prompt_tokens=10, completion_tokens=5,
                prompt_tokens_details=None, completion_tokens_details=None)
            message = types.SimpleNamespace(content="ok", tool_calls=None)
            return types.SimpleNamespace(
                usage=usage, choices=[types.SimpleNamespace(message=message)])

        monkeypatch.setattr(llm_mod.litellm, "completion", fake_completion)

        messages = [{"role": "system", "content": "S"},
                    {"role": "user", "content": "U1"},
                    {"role": "user", "content": "U2"}]
        client.chat(messages, model="test-model-alpha")

        assert captured["max_tokens"] == int(262144 * 0.9)

    def test_context_exceeded_retry_falls_back_when_unresolved(self, monkeypatch):
        """窗口解析失败（0）时重试回落 128k × 0.9。"""
        import core.llm_client as llm_mod
        from litellm.exceptions import ContextWindowExceededError

        client = self._client(monkeypatch, "unknown-model", model_cost={})
        monkeypatch.setattr(llm_mod, "_log_model_call", lambda *a, **k: None)

        captured = {}
        monkeypatch.setattr(
            client, "_truncate_for_context",
            lambda messages, max_tokens=4096: captured.setdefault(
                "max_tokens", max_tokens) or [messages[0], messages[-1]])

        calls = []

        def fake_completion(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise ContextWindowExceededError(
                    "x", model="unknown-model", llm_provider="openai")
            usage = types.SimpleNamespace(
                prompt_tokens=1, completion_tokens=1,
                prompt_tokens_details=None, completion_tokens_details=None)
            message = types.SimpleNamespace(content="ok", tool_calls=None)
            return types.SimpleNamespace(
                usage=usage, choices=[types.SimpleNamespace(message=message)])

        monkeypatch.setattr(llm_mod.litellm, "completion", fake_completion)
        client.chat([{"role": "user", "content": "hi"}], model="unknown-model")
        assert captured["max_tokens"] == int(128000 * 0.9)


class TestAgentTokenBudgetWiring:
    """agent 的 TokenBudget：config.context_budget 优先，其次模型窗口解析值，
    最后内置默认。"""

    def _make_agent(self, monkeypatch, tmp_path, config_dict, window):
        import core.llm_client as llm_mod
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps(config_dict), encoding="utf-8")
        monkeypatch.setattr(
            "agent.agent.get_data_path",
            lambda name="config.json": str(tmp_path / name))
        stub_llm = types.SimpleNamespace(
            default_model="stub", model_context_window=window)
        monkeypatch.setattr(
            "agent.agent.LLMClient", lambda default_model=None: stub_llm)
        return OpenAGCAgent(memory_db_path=str(tmp_path / "memory.db"))

    def test_model_window_used_when_no_config_budget(self, monkeypatch, tmp_path):
        agent = self._make_agent(monkeypatch, tmp_path, {}, window=262144)
        assert agent.token_budget.max_tokens == 262144

    def test_config_budget_takes_priority(self, monkeypatch, tmp_path):
        agent = self._make_agent(
            monkeypatch, tmp_path,
            {"context_budget": {"max_total_tokens": 111111}},
            window=262144)
        assert agent.token_budget.max_tokens == 111111

    def test_default_when_window_unresolved(self, monkeypatch, tmp_path):
        agent = self._make_agent(monkeypatch, tmp_path, {}, window=0)
        assert agent.token_budget.max_tokens == 128000  # TokenBudget 内置默认


# ── Item 5: plan 单次注入 ──

class TestPlanSingleInjection:
    """system prompt 中任务计划只出现一次（第一段无标题注入已删）。"""

    def test_plan_appears_exactly_once(self, monkeypatch, tmp_path):
        fake_plan = {
            "plan_id": "p-test",
            "goal": "TEST-PLAN-GOAL-XYZ",
            "status": "doing",
            "steps": [{"status": "todo", "desc": "步骤甲"}],
        }
        monkeypatch.setattr("tools.task_plan.load_plan",
                            lambda plan_id=None, task_id=None: fake_plan)
        monkeypatch.setattr("tools.task_plan.load_goals", lambda: [])
        monkeypatch.setattr("tools.task_plan.format_goal_list_for_prompt",
                            lambda goals: "")
        # plan_id / title 的 DB 查询指向临时空库（查不到即走 JSON fallback）
        monkeypatch.setattr("core.paths.get_data_path",
                            lambda name="config.json": str(tmp_path / name))

        agent = _bare_agent()
        agent.llm = StubLLM([_text_response("完成")])
        agent.run_turn("你好", verbose=False, task_id=123, skip_rag=True)

        system = agent.messages[0]["content"]
        assert system.count("## 当前计划进度") == 1
        assert system.count("TEST-PLAN-GOAL-XYZ") == 1
        assert system.count("📋 任务计划") == 1


# ── Item 6: reasoning_content 剥离 ──

@_litellm
class TestReasoningContentStripped:
    """_build_model_kwargs 对每条消息剥离 reasoning_content（单点覆盖
    resume 旧快照），且不改动调用方的消息列表。"""

    def test_stripped_from_sent_messages(self):
        from core.llm_client import LLMClient
        client = LLMClient.__new__(LLMClient)
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a",
             "reasoning_content": "链式思考……"},
            {"role": "user", "content": "q2",
             "reasoning_content": "不应出现的键"},
        ]
        kwargs = client._build_model_kwargs("gpt-4o", messages)
        sent = kwargs["messages"]
        assert all("reasoning_content" not in m for m in sent)
        # 内容其余部分保持
        assert sent[2]["content"] == "a"

    def test_caller_messages_not_mutated(self):
        from core.llm_client import LLMClient
        client = LLMClient.__new__(LLMClient)
        messages = [
            {"role": "user", "content": "q", "reasoning_content": "保留在调用方"},
            {"role": "assistant", "content": "a"},
        ]
        client._build_model_kwargs("gpt-4o", messages)
        assert messages[0].get("reasoning_content") == "保留在调用方"

    def test_no_reasoning_content_no_copy_overhead(self):
        """没有 reasoning_content 时消息列表原样传递（恒等行为不变）。"""
        from core.llm_client import LLMClient
        client = LLMClient.__new__(LLMClient)
        messages = [{"role": "user", "content": "q"},
                    {"role": "assistant", "content": "a"}]
        kwargs = client._build_model_kwargs("gpt-4o", messages)
        sent = kwargs["messages"]
        assert [m.get("content") for m in sent] == ["q", "a"]


# ── Item 7: failed_attempts 跨任务清空 ──

class TestFailedAttemptsClearedOnRunTurn:
    """run_turn 开头随 _consecutive_failures 一起清空 failed_attempts，
    避免上一任务的避坑清单泄漏进本任务 system prompt。"""

    def test_failed_attempts_cleared_and_not_injected(self, monkeypatch, tmp_path):
        monkeypatch.setattr("core.paths.get_data_path",
                            lambda name="config.json": str(tmp_path / name))
        agent = _bare_agent()
        agent.failed_attempts = ["`old_tool`(x) => Error OLD-ATTEMPT-MARKER"]
        agent.llm = StubLLM([_text_response("ok")])
        agent.run_turn("新任务", verbose=False, task_id=456, skip_rag=True)

        assert agent.failed_attempts == []
        assert "OLD-ATTEMPT-MARKER" not in agent.messages[0]["content"]
