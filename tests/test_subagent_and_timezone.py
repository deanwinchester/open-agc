# -*- coding: utf-8 -*-
"""子代理假完成修复回归（生产实证：子代理回一段计划或把工具调用写成
JSON 文本就报 success，主 agent 只能兜底重做）：
1) 文本形态的 JSON 工具调用 → 营救为真实执行；
2) 零工具调用的空谈回复 → 催促继续（最多 2 次），不算完成。

时区：系统提示带时区偏移与 UTC 换算提醒；前端 DB 时间转本地。"""
import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from agent.sub_agent import SubAgent  # noqa: E402


class _FakeTool:
    def __init__(self):
        self.calls = []

    def get_openai_schema(self):
        return {"type": "function", "function": {"name": "fake_tool",
                "parameters": {"type": "object", "properties": {}}}}

    def execute(self, **kw):
        self.calls.append(kw)
        return "REAL_WORK_DONE"


class _Msg:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, m):
        self.message = m


class _Resp:
    def __init__(self, m):
        self.choices = [_Choice(m)]


class _ScriptedLLM:
    """按脚本逐轮返回内容。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def chat(self, messages=None, tools=None):
        self.calls += 1
        return _Resp(_Msg(self.script.pop(0))), "fake-model"


def _make_sub(llm, tool):
    return SubAgent(
        task="执行一个写入任务",
        tools=["fake_tool"],
        parent_tools={"fake_tool": tool},
        max_iterations=6,
        llm_client=llm,
    )


class TestSubAgentRescue:
    def test_text_json_tool_call_rescued(self):
        tool = _FakeTool()
        llm = _ScriptedLLM([
            '```json\n{"name": "fake_tool", "arguments": {"x": 1}}\n```',
            "任务完成，已写入。",
        ])
        sub = _make_sub(llm, tool)
        result = sub.run()
        assert tool.calls, "文本 JSON 工具调用必须被营救为真实执行"
        assert result["success"] is True
        assert result["tool_calls"] >= 1

    def test_empty_talk_nudged(self):
        tool = _FakeTool()
        llm = _ScriptedLLM([
            "我打算先读取文件再写入。",   # 空谈 → 催促
            "我计划分三步……",            # 还是空谈 → 再催
            "我打算先读取文件再写入。",    # 第 3 次空谈 → 接受（防死循环）
        ])
        sub = _make_sub(llm, tool)
        result = sub.run()
        assert not tool.calls
        # 两次催促后才放行 → LLM 被调用了 3 次
        assert llm.calls == 3
        assert result["success"] is True  # 放行保底，不卡死


class TestTimezone:
    def test_prompt_has_timezone(self):
        src = open(os.path.join(PROJECT_ROOT, "agent", "agent.py"),
                   encoding="utf-8").read()
        assert "astimezone()" in src
        assert "当前本地时间：{current_time}" in src
        assert "时区" in src and "UTC" in src

    def test_frontend_db_time_formatter(self):
        src = open(os.path.join(PROJECT_ROOT, "vue-app", "src", "utils",
                                "time.js"), encoding="utf-8").read()
        assert "formatDbTime" in src and "+ 'Z'" in src.replace("'", "'")
