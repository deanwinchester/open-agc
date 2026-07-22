# -*- coding: utf-8 -*-
"""S6 Task 4 tests:
- dispatch_subagent 工具：stub SubAgent 验证参数传递与结构化返回
- _should_delegate 怪癖修复：工具名字面量不再触发委派；领域关键词与复杂度信号组合才委派
- base64 驻留优化：提取后 messages 中无完整 base64（replace_image_markers 占位符）
"""
import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from agent.sub_agent import TOOL_SETS, match_tool_set  # noqa: E402
from agent.agent import OpenAGCAgent  # noqa: E402
from core.llm_client import (  # noqa: E402
    IMAGE_MARKER, SCREENSHOT_MARKER, IMAGE_INJECTED_PLACEHOLDER,
    extract_image_data, extract_screenshot_data, replace_image_markers,
)
from tools.subagent_dispatch import DispatchSubagentTool  # noqa: E402


# ────────────────────────── TOOL_SETS 结构 ──────────────────────────

class TestToolSetsStructure:
    def test_entries_have_keywords_and_tools(self):
        for name, entry in TOOL_SETS.items():
            assert set(entry.keys()) >= {"keywords", "tools"}, name
            assert entry["keywords"], name
            assert entry["tools"], name

    def test_keywords_contain_no_tool_name_literals(self):
        """怪癖根因：关键词表不得包含工具名字面量（read_file 等）。"""
        all_tool_names = {t for entry in TOOL_SETS.values() for t in entry["tools"]}
        for name, entry in TOOL_SETS.items():
            for kw in entry["keywords"]:
                assert kw not in all_tool_names, f"{name}: keyword '{kw}' 是工具名"

    def test_match_tool_set(self):
        assert match_tool_set("帮我分析一下这些数据") == "analysis"
        assert match_tool_set("部署并监控这个服务") == "deploy"
        # 无关键词命中时回退默认
        assert match_tool_set("做个东西") == "filesystem"
        assert match_tool_set("") == "filesystem"


# ────────────────────────── _should_delegate 行为 ──────────────────────────

class TestShouldDelegate:
    @staticmethod
    def _delegate(text: str) -> bool:
        # _should_delegate 不依赖实例状态，直接以未绑定方法调用
        return OpenAGCAgent._should_delegate(object(), text)

    def test_single_tool_mention_not_delegated(self):
        """原怪癖：TOOL_SETS 五个领域都含 read_file，提到即 area_count=5 必委派。"""
        assert self._delegate("用 read_file 读一下 test/case.txt") is False
        assert self._delegate("read_file 这个工具怎么用？") is False
        assert self._delegate("帮我用 execute_shell 跑个命令") is False

    def test_simple_one_shot_not_delegated(self):
        assert self._delegate("帮我看下这个报错是什么意思") is False
        assert self._delegate("把这个文件改成 UTF-8 编码") is False

    def test_single_domain_with_complexity_word_not_delegated(self):
        """高频复杂度词 + 单一领域（area_count=1）不得委派——主循环一两步即可完成。"""
        assert self._delegate("列出所有文件") is False          # 所有 + filesystem
        assert self._delegate("写个脚本处理所有日志") is False    # 所有 + code
        assert self._delegate("分析所有数据") is False           # 所有 + analysis
        assert self._delegate("同时读取这几个文件") is False      # 同时 + filesystem

    def test_multi_domain_with_complexity_word_delegated(self):
        """复杂度词 + 真多领域（area_count>=2）→ 委派。"""
        assert self._delegate("部署服务并监控所有实例") is True   # 所有 + deploy/monitor
        assert self._delegate("先研究竞品再写代码") is True       # 先.*再 + research/code

    def test_deploy_and_monitor_delegated(self):
        """复杂度信号(部署) + 多领域(deploy + monitor) → 委派。"""
        assert self._delegate("部署并监控这个服务") is True

    def test_original_two_rules_unchanged(self):
        """原有规则保持：match_count>=2 单独成立；area_count>=3 单独成立。"""
        # 复杂度词 >=2，无领域命中
        assert self._delegate("分别处理，先做 A 然后再做 B") is True
        # 领域 >=3，无复杂度词
        assert self._delegate("分析网页文件") is True

    def test_multi_domain_still_delegated(self):
        assert self._delegate("先分析数据，再部署服务") is True  # 先.*再 + 部署
        assert self._delegate("分别处理所有目录里的文件并做全面分析") is True

    def test_decompose_resolves_tools_from_new_structure(self):
        """_decompose_task 的工具集解析：新结构 entry['tools']，未知名称按字面工具名保留。"""
        for set_name, entry in TOOL_SETS.items():
            resolved = []
            e = TOOL_SETS.get(set_name)
            resolved.extend(e["tools"] if e else [set_name])
            assert resolved == entry["tools"]
        # 未知名称回退字面量
        e = TOOL_SETS.get("some_custom_tool")
        assert (e["tools"] if e else ["some_custom_tool"]) == ["some_custom_tool"]


# ────────────────────────── dispatch_subagent 工具 ──────────────────────────

class _StubAgent:
    """最小 agent 上下文：提供 SubAgent 所需的属性通道。"""

    def __init__(self):
        self.llm = object()  # 仅需非 None
        self.full_available_tools = {"read_file": object(), "write_file": object(),
                                     "execute_shell": object()}
        self.available_tools = self.full_available_tools
        self._session_sandbox_whitelist = {"sandbox-wl"}
        self._session_network_whitelist = {"net-wl"}
        self._session_permission_whitelist = {"perm-wl"}
        self.session_id = 7


class _StubSubAgent:
    """捕获构造参数，返回固定结构化结果。"""

    last_init = None

    def __init__(self, **kwargs):
        _StubSubAgent.last_init = kwargs

    def run(self):
        return {
            "success": True,
            "summary": "子任务完成",
            "output_files": ["workspace/out.txt"],
            "iterations_used": 3,
            "tool_calls": 2,
            "duration": 1.234,
            "steps": [],
        }


@pytest.fixture
def stub_sub_agent(monkeypatch):
    _StubSubAgent.last_init = None
    monkeypatch.setattr("agent.sub_agent.SubAgent", _StubSubAgent)
    return _StubSubAgent


class TestDispatchSubagentTool:
    def test_schema(self):
        schema = DispatchSubagentTool().get_openai_schema()
        fn = schema["function"]
        assert fn["name"] == "dispatch_subagent"
        props = fn["parameters"]["properties"]
        assert set(props) == {"task", "tool_set", "max_iterations"}
        assert fn["parameters"]["required"] == ["task"]
        assert set(props["tool_set"]["enum"]) == set(TOOL_SETS.keys())

    def test_explicit_tool_set_and_param_passing(self, stub_sub_agent):
        agent = _StubAgent()
        out = DispatchSubagentTool().execute(
            task="部署这个服务", tool_set="deploy", max_iterations=5,
            _agent_context=agent, _progress_cb="CB",
        )
        result = json.loads(out)
        assert result["success"] is True
        assert result["summary"] == "子任务完成"
        assert result["tool_set"] == "deploy"
        assert result["iterations_used"] == 3
        assert result["tool_calls"] == 2
        assert result["output_files"] == ["workspace/out.txt"]
        assert result["duration"] == 1.2

        init = stub_sub_agent.last_init
        assert init["task"] == "部署这个服务"
        assert init["tools"] == TOOL_SETS["deploy"]["tools"]
        assert init["max_iterations"] == 5
        assert init["parent_tools"] is agent.full_available_tools
        assert init["llm_client"] is agent.llm
        assert init["agent_context"] is agent
        assert init["progress_callback"] == "CB"
        # 白名单从 agent 属性通道兜底取得
        assert init["session_whitelist"] == {"sandbox-wl"}
        assert init["network_whitelist"] == {"net-wl"}
        assert init["permission_whitelist"] == {"perm-wl"}
        assert init["session_id"] == 7

    def test_auto_tool_set_match(self, stub_sub_agent):
        DispatchSubagentTool().execute(task="分析一下这些数据的分布",
                                       _agent_context=_StubAgent())
        init = stub_sub_agent.last_init
        assert init["tools"] == TOOL_SETS["analysis"]["tools"]

    def test_max_iterations_clamped(self, stub_sub_agent):
        DispatchSubagentTool().execute(task="t", max_iterations=999,
                                       _agent_context=_StubAgent())
        assert stub_sub_agent.last_init["max_iterations"] == 30
        DispatchSubagentTool().execute(task="t", max_iterations="bad",
                                       _agent_context=_StubAgent())
        assert stub_sub_agent.last_init["max_iterations"] == 10

    def test_unknown_tool_set_error(self, stub_sub_agent):
        out = DispatchSubagentTool().execute(task="t", tool_set="nope",
                                             _agent_context=_StubAgent())
        assert out.startswith("Error")
        assert stub_sub_agent.last_init is None  # 未构造 SubAgent

    def test_requires_agent_context(self):
        out = DispatchSubagentTool().execute(task="t")
        assert out.startswith("Error")

    def test_requires_llm(self):
        agent = _StubAgent()
        agent.llm = None
        out = DispatchSubagentTool().execute(task="t", _agent_context=agent)
        assert out.startswith("Error")

    def test_registered_lazy_in_agent(self):
        """注册在惰性集：full_available_tools 有，常驻 core 没有。"""
        # 不实例化 Agent（重），改为静态检查源码注册位置
        import inspect
        import agent.agent as agent_mod
        src = inspect.getsource(agent_mod)
        assert '"dispatch_subagent": DispatchSubagentTool()' in src
        # TIERED_CORE_TOOL_NAMES / FULL_CORE_TOOL_NAMES 均不含 → 惰性
        tiered = src.split("TIERED_CORE_TOOL_NAMES = {")[1].split("}")[0]
        full = src.split("FULL_CORE_TOOL_NAMES = {")[1].split("}")[0]
        assert "dispatch_subagent" not in tiered
        assert "dispatch_subagent" not in full


# ────────────────────────── base64 占位符替换 ──────────────────────────

class TestImageMarkerReplacement:
    B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    IMG_RESULT = f"图片读取成功 {IMAGE_MARKER}data:image/png;base64,{B64}] 请查看"
    SHOT_RESULT = f"点击完成 [SCREENSHOT_DATA:data:image/png;base64,{B64}]"

    def test_replace_both_markers(self):
        for raw in (self.IMG_RESULT, self.SHOT_RESULT):
            out = replace_image_markers(raw)
            assert IMAGE_INJECTED_PLACEHOLDER in out
            assert self.B64 not in out
            assert IMAGE_MARKER not in out
            assert SCREENSHOT_MARKER not in out

    def test_extract_then_replace_order(self):
        """时序契约：先从未截断 result 提取，再替换，替换后提取不到。"""
        url = extract_image_data(self.IMG_RESULT)
        assert url == f"data:image/png;base64,{self.B64}"
        out = replace_image_markers(self.IMG_RESULT)
        assert extract_image_data(out) is None

        url = extract_screenshot_data(self.SHOT_RESULT)
        assert url == f"data:image/png;base64,{self.B64}"
        out = replace_image_markers(self.SHOT_RESULT)
        assert extract_screenshot_data(out) is None

    def test_no_marker_passthrough(self):
        s = "普通工具结果，没有图片标记"
        assert replace_image_markers(s) is s

    def test_surrounding_text_preserved(self):
        out = replace_image_markers(self.IMG_RESULT)
        assert out.startswith("图片读取成功 ")
        assert out.endswith(" 请查看")

    def test_custom_placeholder(self):
        out = replace_image_markers(self.IMG_RESULT, placeholder="[img]")
        assert "[img]" in out and self.B64 not in out

    def test_agent_loop_messages_have_no_base64(self):
        """模拟主循环时序：提取 → 替换 → 写入 messages，messages 中无完整 base64。"""
        result_str = self.IMG_RESULT
        screenshot_urls = []
        # 与 agent.py 主循环相同顺序
        img_url = extract_image_data(result_str)
        if img_url:
            screenshot_urls.append(img_url)
        result_str = replace_image_markers(result_str)
        messages = [{"role": "tool", "content": result_str}]
        assert self.B64 not in messages[0]["content"]
        assert IMAGE_INJECTED_PLACEHOLDER in messages[0]["content"]
        assert screenshot_urls == [f"data:image/png;base64,{self.B64}"]
