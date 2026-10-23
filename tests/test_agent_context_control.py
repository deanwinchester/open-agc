# -*- coding: utf-8 -*-
"""Agent 主循环上下文主动管理（截断可控 + 主动丢弃）

Covers:
1. 截断提示语含 read_file offset/limit 分页指引与溢出文件路径/总字符数
2. read_file offset/limit 分段读取（含边界：offset=0 钳制、越界、非法值）
3. result_limit 透传参数生效（替代默认 cap）与 200000 封顶、非法值回落
4. drop_context：替换 content 且保留消息骨架（role/tool_call_id）、
   最近 2 轮拒绝、占位符含 reason、丢弃处 system 备注

替身风格同 test_s7t2_context_defenses.py（裸 agent，不跑 __init__）。
All tests run without API keys, real databases, or network access.
"""
import os
import sys
import types

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from agent.agent import OpenAGCAgent  # noqa: E402
from tools.drop_context import DropContextTool  # noqa: E402
from tools.filesystem import ReadFileTool  # noqa: E402


@pytest.fixture
def no_sandbox(monkeypatch, tmp_path):
    """get_data_path 指向不存在的 config → 跳过沙箱校验（同 test_s5t3）。"""
    monkeypatch.setattr("core.paths.get_data_path",
                        lambda name: str(tmp_path / "no_config" / name))


def _trunc_agent(tmp_path):
    """裸 agent：截断溢出落盘到 tmp_path，避免向仓库工作目录写文件。"""
    agent = OpenAGCAgent.__new__(OpenAGCAgent)
    agent.sandbox_dir = str(tmp_path)
    return agent


def _big_multiline(lines=1000):
    # 1000 行 × ~60 字符 ≈ 60k，远超所有默认 cap
    return "\n".join(f"line {i} " + "x" * 50 for i in range(lines))


# ── 1. 截断提示语含分页指引 ──

class TestSpillNotePagingGuidance:
    def test_note_has_read_file_paging_guidance(self, tmp_path):
        agent = _trunc_agent(tmp_path)
        big = _big_multiline()
        out = agent._truncate_tool_result_for_context(big, "read_file")
        assert len(out) <= 8000
        assert "read_file" in out and "offset" in out and "limit" in out
        assert "tool_results/" in out
        assert f"共 {len(big)} 字符" in out

    def test_spilled_file_is_complete(self, tmp_path):
        agent = _trunc_agent(tmp_path)
        big = _big_multiline()
        agent._truncate_tool_result_for_context(big, "fetch_url")
        files = list((tmp_path / "tool_results").glob("fetch_url_*.txt"))
        assert len(files) == 1
        assert files[0].read_text(encoding="utf-8") == big


# ── 2. read_file offset/limit 分段（含边界） ──

class TestReadFilePagingForSpilledResults:
    @pytest.fixture
    def spilled_file(self, tmp_path):
        f = tmp_path / "spill.txt"
        f.write_text("\n".join(f"row{i}" for i in range(1, 11)), encoding="utf-8")
        return f

    def test_segment_read(self, no_sandbox, spilled_file):
        out = ReadFileTool().execute(path=str(spilled_file), offset=4, limit=3)
        assert "(Lines 4 to 6 of 10)" in out
        assert "4 | row4" in out and "6 | row6" in out
        assert "3 | row3" not in out
        # 分页 hint 指向下一段
        assert "offset=7" in out

    def test_offset_zero_clamped_to_one(self, no_sandbox, spilled_file):
        out = ReadFileTool().execute(path=str(spilled_file), offset=0, limit=2)
        assert "(Lines 1 to 2 of 10)" in out

    def test_offset_beyond_eof(self, no_sandbox, spilled_file):
        out = ReadFileTool().execute(path=str(spilled_file), offset=99)
        assert "超出文件末尾" in out and "10" in out

    def test_last_segment_no_hint(self, no_sandbox, spilled_file):
        out = ReadFileTool().execute(path=str(spilled_file), offset=9, limit=50)
        assert "(Lines 9 to 10 of 10)" in out
        assert "继续读取" not in out

    def test_invalid_limit_rejected(self, no_sandbox, spilled_file):
        out = ReadFileTool().execute(path=str(spilled_file), limit="abc")
        assert out.startswith("Error")
        out = ReadFileTool().execute(path=str(spilled_file), limit=0)
        assert "limit must be >= 1" in out


# ── 3. result_limit 透传参数 ──

class TestResultLimit:
    def test_result_limit_overrides_default_cap(self, tmp_path):
        agent = _trunc_agent(tmp_path)
        # 单行超长：压缩器无从下手走硬截断，输出正好吃满预算，
        # 可严格区分「默认 8000 cap」与「result_limit=20000」
        big = "y" * 60000
        out = agent._truncate_tool_result_for_context(
            big, "read_file", result_limit=20000)
        assert 8000 < len(out) <= 20000  # 不再被默认 8000 截断

    def test_result_limit_smaller_than_default(self, tmp_path):
        agent = _trunc_agent(tmp_path)
        out = agent._truncate_tool_result_for_context(
            _big_multiline(), "execute_shell", result_limit=2000)
        assert len(out) <= 2000

    def test_result_limit_capped_at_max(self, tmp_path):
        agent = _trunc_agent(tmp_path)
        big = "y" * 250000  # 单行超长，走硬截断兜底
        out = agent._truncate_tool_result_for_context(
            big, "read_file", result_limit=10 ** 9)
        assert len(out) <= OpenAGCAgent._TOOL_RESULT_LIMIT_MAX == 200000
        assert len(out) > 12000  # 确实绕过了默认 cap

    def test_invalid_result_limit_falls_back(self, tmp_path):
        agent = _trunc_agent(tmp_path)
        out = agent._truncate_tool_result_for_context(
            _big_multiline(), "read_file", result_limit="abc")
        assert len(out) <= 8000  # 非法值回落默认 cap

    def test_no_result_limit_unchanged(self, tmp_path):
        agent = _trunc_agent(tmp_path)
        big = _big_multiline()
        out = agent._truncate_tool_result_for_context(big, "read_file")
        assert len(out) <= 8000


# ── 4. drop_context 主动丢弃 ──

def _drop_agent():
    """裸 agent + 三轮工具调用的消息流（第三轮为当前工作现场）。"""
    agent = OpenAGCAgent.__new__(OpenAGCAgent)
    agent.messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "任务"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "t1"}, {"id": "t2"}]},          # 第 1 轮（最远）
        {"role": "tool", "tool_call_id": "t1", "name": "search_web",
         "content": "BIG-1 " + "a" * 3000},
        {"role": "tool", "tool_call_id": "t2", "name": "fetch_url",
         "content": "BIG-2 " + "b" * 3000},
        {"role": "assistant", "content": "继续"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "t3"}]},                          # 第 2 轮（受保护）
        {"role": "tool", "tool_call_id": "t3", "name": "execute_shell",
         "content": "BIG-3 " + "c" * 3000},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "t4"}]},                          # 第 3 轮（受保护）
        {"role": "tool", "tool_call_id": "t4", "name": "read_file",
         "content": "BIG-4 " + "d" * 3000},
        {"role": "assistant", "content": "当前工作中"},
    ]
    return agent


class TestDropContext:
    def _execute(self, agent, targets, reason="无关检索结果"):
        return DropContextTool().execute(
            targets=targets, reason=reason, _agent_context=agent)

    def _msg(self, agent, call_id):
        return next(m for m in agent.messages
                    if m.get("role") == "tool" and m.get("tool_call_id") == call_id)

    def test_replace_content_keep_skeleton(self):
        agent = _drop_agent()
        n_before = len(agent.messages)
        out = self._execute(agent, ["t1"])
        assert "已丢弃 1 段" in out
        msg = self._msg(agent, "t1")
        # content 替换为占位符且含 reason
        assert msg["content"].startswith("[该内容已被你主动丢弃以节省上下文")
        assert "无关检索结果" in msg["content"]
        assert "tool_results/" in msg["content"]
        # 消息骨架保留：role/tool_call_id/name 不动，assistant tool_calls 不动
        assert msg["role"] == "tool" and msg["tool_call_id"] == "t1"
        assert msg["name"] == "search_web"
        assert agent.messages[2]["tool_calls"] == [{"id": "t1"}, {"id": "t2"}]
        # 只插入了一条 system 备注，未删任何消息
        assert len(agent.messages) == n_before + 1
        notes = [m for m in agent.messages if m["role"] == "system"]
        assert any("主动丢弃" in m["content"] and "无关检索结果" in m["content"]
                   for m in notes[1:])  # notes[0] 是原 system prompt

    def test_system_note_not_inside_tool_chain(self):
        agent = _drop_agent()
        self._execute(agent, ["t1"])
        # 备注不得插进 assistant.tool_calls 与其 tool 结果链之间（协议约束）
        for i, m in enumerate(agent.messages):
            if m.get("role") == "assistant" and m.get("tool_calls"):
                assert agent.messages[i + 1]["role"] == "tool"

    def test_recent_two_rounds_refused(self):
        agent = _drop_agent()
        out = self._execute(agent, ["t4", "t3", "最近第1轮", "最近第2轮"])
        assert "受保护" in out
        assert self._msg(agent, "t3")["content"].startswith("BIG-3")
        assert self._msg(agent, "t4")["content"].startswith("BIG-4")
        # 全部拒绝时不插备注、不动消息数
        assert len(agent.messages) == 11

    def test_round_ordinal_drops_oldest_round(self):
        agent = _drop_agent()
        out = self._execute(agent, ["最近第3轮"], reason="早期检索全部无用")
        assert "已丢弃 2 段" in out
        assert self._msg(agent, "t1")["content"].startswith("[该内容已被你主动丢弃")
        assert self._msg(agent, "t2")["content"].startswith("[该内容已被你主动丢弃")

    def test_plain_number_treated_as_round_ordinal(self):
        agent = _drop_agent()
        out = self._execute(agent, ["3"])
        assert "已丢弃 2 段" in out

    def test_unknown_target_reported(self):
        agent = _drop_agent()
        out = self._execute(agent, ["no-such-id"])
        assert "未找到目标" in out and "no-such-id" in out
        assert len(agent.messages) == 11

    def test_missing_agent_context(self):
        out = DropContextTool().execute(targets=["t1"], reason="x")
        assert out.startswith("Error")

    def test_empty_targets_rejected(self):
        agent = _drop_agent()
        out = self._execute(agent, [])
        assert out.startswith("Error")
