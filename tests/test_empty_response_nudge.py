# -*- coding: utf-8 -*-
"""空响应催促回归测试：推理型本地模型（llamacpp/Qwen）偶发「只输出
reasoning_content 就 EOS」（正文为空、无工具调用）。此前 run_turn 直接
停任务返回空响应错误——实测同一 prompt 非流式/重试即恢复，直接停太脆。
现在：有 reasoning_content 的空响应先催促重试（上限 2 次）。"""
import json
import queue
import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from agent.agent import OpenAGCAgent  # noqa: E402


class _StubMessage:
    def __init__(self, content=None, tool_calls=None, reasoning_content=None):
        self.role = "assistant"
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = reasoning_content


class _StubResponse:
    def __init__(self, message):
        self.choices = [types.SimpleNamespace(message=message)]
        self.usage = None


class StubLLM:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.default_model = "stub-model"

    def chat(self, messages=None, tools=None, interrupt_check=None):
        self.calls.append(1)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, "stub-model"


def _bare_agent():
    agent = OpenAGCAgent.__new__(OpenAGCAgent)
    agent.session_id = None
    agent.failed_attempts = []
    agent.messages = [{"role": "system", "content": "sys"}]
    agent.logger = None
    agent.pending_messages = []
    agent._processing_interjection = False
    agent._interjection_stuck_count = 0
    agent._rejected_interjection = None
    agent._in_self_review = False
    agent._max_correction_attempts = 0
    agent.tool_schemas = []
    agent.tool_display_names = {}
    agent.available_tools = {}
    agent.full_available_tools = {}
    agent._session_sandbox_whitelist = set()
    agent._session_network_whitelist = set()
    agent._session_permission_whitelist = set()
    agent._pending_sudo_password = ""
    agent.reflection_engine = None
    agent.knowledge_graph = types.SimpleNamespace(extract_from_messages=lambda msgs: None)
    agent._save_task_stats = lambda *a, **k: None
    agent.user_input_queue = queue.Queue()
    agent.progress_callback = None
    agent._build_system_prompt = lambda **kwargs: "sys"
    return agent


@pytest.fixture(autouse=True)
def _no_adaptive_writes(monkeypatch):
    monkeypatch.setattr("tools.adaptive.record_tool_call", lambda *a, **k: None)


class TestEmptyResponseNudge:
    def test_reasoning_only_empty_nudged_then_recovers(self):
        """空正文但有 reasoning_content：催促后继续，不停任务。"""
        agent = _bare_agent()
        agent.llm = StubLLM([
            _StubResponse(_StubMessage(content=None, reasoning_content="让我想想……")),
            _StubResponse(_StubMessage(content="正式回答")),
        ])
        result = agent.run_turn("任务", verbose=False, skip_rag=True)
        assert result == "正式回答"
        assert len(agent.llm.calls) == 2
        # 催促提示已注入上下文
        assert any(m.get("role") == "system" and "只进行了思考" in str(m.get("content", ""))
                   for m in agent.messages)

    def test_persistent_empty_stops_after_two_nudges(self):
        """连续空响应：催促 2 次后仍空才停止。"""
        agent = _bare_agent()
        agent.llm = StubLLM([
            _StubResponse(_StubMessage(content=None, reasoning_content="思考1")),
            _StubResponse(_StubMessage(content=None, reasoning_content="思考2")),
            _StubResponse(_StubMessage(content=None, reasoning_content="思考3")),
        ])
        result = agent.run_turn("任务", verbose=False, skip_rag=True)
        assert "空响应" in result
        assert len(agent.llm.calls) == 3  # 原始 + 2 次催促

    def test_truly_empty_no_reasoning_stops_immediately(self):
        """完全没有内容（连 reasoning 都没有）：不催促，直接停。"""
        agent = _bare_agent()
        agent.llm = StubLLM([
            _StubResponse(_StubMessage(content=None)),
        ])
        result = agent.run_turn("任务", verbose=False, skip_rag=True)
        assert "空响应" in result
        assert len(agent.llm.calls) == 1
