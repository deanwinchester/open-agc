# -*- coding: utf-8 -*-
"""调度者模式 M1 测试（重构轮）：
- dispatch_worker 工具（brief 传递 / 验收失败重派 / 双失败返回失败信息）
- enrich_handoff 检索增强（零 LLM 调用；各来源 mock 后字段齐全 / 降级不炸）
- dispatcher_mode 开/关门控（开：工具预启用+提示词指引；关：零影响）
- verify_execution 证据验收（假成功拒绝 / 产出文件缺失·为空拒绝 / 真实产出通过）
- SubAgent 全量工具发现（worker 解锁初始集之外的工具并执行）
- dispatch_to_worker 闭环（验收失败重派一次 / 双失败结构化失败返回）
"""
import json
import os
import sys
from types import SimpleNamespace

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from agent import dispatcher  # noqa: E402
from agent.agent import OpenAGCAgent  # noqa: E402
from agent.sub_agent import SubAgent  # noqa: E402
from tools.dispatch_worker import DispatchWorkerTool, _parse_acceptance  # noqa: E402


# ────────────────────────── 测试替身 ──────────────────────────

def _resp(text=None, tool_calls=None):

    msg = SimpleNamespace(role="assistant", content=text or "", tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None), "mock-model"


def _tc(name, args=None, call_id="call_1"):
    fn = SimpleNamespace(name=name, arguments=json.dumps(args or {}, ensure_ascii=False))
    return SimpleNamespace(id=call_id, type="function", function=fn)


class _ScriptLLM:
    """按脚本返回的 LLM：每项是 (text, tool_calls) 元组或 Exception。"""
    def __init__(self, script):
        self.script = list(script)
        self.seen_tools = []
        self.seen_messages = []

    def chat(self, messages=None, tools=None):
        self.seen_tools.append(
            [t["function"]["name"] for t in (tools or []) if isinstance(t, dict)])
        self.seen_messages.append(list(messages or []))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        text, tool_calls = item
        return _resp(text, tool_calls)


class _FakeTool:
    """最小 duck-type 工具（SubAgent 只用 get_openai_schema/execute/description）。"""
    def __init__(self, name, description="fake tool", result="OK"):
        self.name = name
        self.description = description
        self._result = result
        self.calls = []

    def get_openai_schema(self):
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": {"type": "object", "properties": {}}}}

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return self._result


def _fake_agent(llm=None, **overrides):
    agent = SimpleNamespace(
        llm=llm,
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "请处理 D:/work/project 的数据"},
            {"role": "assistant", "content": "好的，先看 D:/work/project/data.csv"},
            {"role": "user", "content": "帮我分析所有数据并部署报表服务"},
        ],
        memory_store=SimpleNamespace(
            search_semantic=lambda q, top_k=5: [
                {"content": "上次用 pandas 处理过同类数据", "memory_type": "fact"},
                {"content": "报表服务端口固定 8080", "memory_type": "fact"},
            ][:top_k],
            search_memories=lambda q, top_k=5: [],
        ),
        sandbox_dir=None,
        task_id=None,
        session_id=None,
        full_available_tools={},
        _session_sandbox_whitelist=set(),
        _session_network_whitelist=set(),
        _session_permission_whitelist=set(),
        _build_context_brief=lambda: "当前任务目标：测试",
    )
    for k, v in overrides.items():
        setattr(agent, k, v)
    return agent


_HISTORY_STUB = [
    {"task_id": 5, "title": "上次报表任务", "result_summary": "完成",
     "key_steps": ["execute_shell", "write_file"]},
]


# ────────────────────────── 交接包增强（零 LLM）──────────────────────────

class TestEnrichHandoff:
    def test_enrichment_fields_complete_and_zero_llm(self, monkeypatch):
        """brief 原样保留 + 历史/记忆/文件追加；全程零 LLM 调用。"""
        llm = _ScriptLLM([])  # 若被调用会因脚本为空直接抛错
        monkeypatch.setattr(dispatcher, "_fetch_relevant_history",
                            lambda q, limit=2: _HISTORY_STUB)
        packet = dispatcher.enrich_handoff(
            _fake_agent(llm),
            "目标：分析销售数据并部署报表服务；背景：季度汇报；产出：report.html",
            acceptance=["产出文件 report.html 存在且非空", "  ", "服务可访问", "第四条不要"])

        assert packet["brief"].startswith("目标：分析销售数据")
        assert packet["relevant_history"][0]["task_id"] == 5
        assert packet["relevant_history"][0]["key_steps"] == ["execute_shell", "write_file"]
        assert packet["memories"] == ["上次用 pandas 处理过同类数据", "报表服务端口固定 8080"]
        assert "D:/work/project" in packet["files"]
        assert "D:/work/project/data.csv" in packet["files"]
        assert len(packet["files"]) <= 10
        # acceptance 归一化：去空白、≤3 条
        assert packet["acceptance"] == ["产出文件 report.html 存在且非空", "服务可访问", "第四条不要"]
        assert llm.seen_tools == []  # 零 LLM 调用

    def test_source_failures_degrade_independently(self, monkeypatch):
        monkeypatch.setattr(
            dispatcher, "_fetch_relevant_history",
            lambda q, limit=2: (_ for _ in ()).throw(RuntimeError("db down")))
        agent = _fake_agent(None, memory_store=SimpleNamespace(
            search_semantic=lambda q, top_k=5: (_ for _ in ()).throw(RuntimeError("chroma down")),
            search_memories=lambda q, top_k=5: (_ for _ in ()).throw(RuntimeError("fts down")),
        ), messages=None)
        packet = dispatcher.enrich_handoff(agent, "简报", acceptance=["标准1"])
        assert packet["brief"] == "简报"
        assert packet["relevant_history"] == []
        assert packet["memories"] == []
        assert packet["files"] == []
        assert packet["acceptance"] == ["标准1"]

    def test_empty_brief_skips_retrieval(self, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(dispatcher, "_fetch_relevant_history",
                            lambda q, limit=2: called.__setitem__("n", 1))
        packet = dispatcher.enrich_handoff(_fake_agent(None), "  ")
        assert packet["brief"] == ""
        assert called["n"] == 0  # 空简报不做检索

    def test_render_packet_task_contains_sections(self):
        text = dispatcher.render_packet_task({
            "brief": "目标X 背景Y",
            "relevant_history": _HISTORY_STUB,
            "memories": ["记忆1"], "files": ["D:/a/b.txt"],
            "acceptance": ["标准1"],
        })
        for marker in ("任务简报", "目标X 背景Y", "#5 上次报表任务",
                       "记忆1", "D:/a/b.txt", "1. 标准1", "search_available_tools"):
            assert marker in text


# ────────────────────────── 证据验收 ──────────────────────────

class TestVerifyExecution:
    def test_reject_fake_success_zero_tool_calls(self, tmp_path):
        v = dispatcher.verify_execution(
            {"acceptance": []},
            {"success": True, "summary": "完成了", "tool_calls": 0},
            sandbox_dir=str(tmp_path))
        assert v["passed"] is False
        assert any("工具调用" in f for f in v["failures"])

    def test_reject_unsuccess_and_empty_summary(self, tmp_path):
        v = dispatcher.verify_execution(
            {"acceptance": []},
            {"success": False, "summary": "", "tool_calls": 3},
            sandbox_dir=str(tmp_path))
        assert v["passed"] is False
        assert any("success" in f for f in v["failures"])
        assert any("摘要" in f for f in v["failures"])

    def test_reject_missing_output_file(self, tmp_path):
        v = dispatcher.verify_execution(
            {"acceptance": ["应产出文件 output/result.txt 供查看"]},
            {"success": True, "summary": "done", "tool_calls": 3, "output_files": []},
            sandbox_dir=str(tmp_path))
        assert v["passed"] is False
        assert any("不存在" in f and "output/result.txt" in f for f in v["failures"])

    def test_pass_with_real_output(self, tmp_path):
        f = tmp_path / "result.txt"
        f.write_text("data", encoding="utf-8")
        v = dispatcher.verify_execution(
            {"acceptance": ["产出文件 result.txt 存在且非空"]},
            {"success": True, "summary": "完成", "tool_calls": 2,
             "output_files": ["result.txt"]},
            sandbox_dir=str(tmp_path))
        assert v["passed"] is True, v["failures"]
        assert v["checked_files"]

    def test_cwd_relative_path_falls_back(self, tmp_path, monkeypatch):
        """生产实证回归：验收文本写 workspace/fib_report.md（项目根口径），
        沙箱根本身就是 workspace/ ——按项目根回退解析，不得误报不存在。"""
        sandbox = tmp_path / "workspace"
        sandbox.mkdir()
        (sandbox / "fib_report.md").write_text("data", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        v = dispatcher.verify_execution(
            {"acceptance": ["产出文件 workspace/fib_report.md 存在且非空"]},
            {"success": True, "summary": "完成", "tool_calls": 2,
             "output_files": ["workspace/fib_report.md"]},
            sandbox_dir=str(sandbox))
        assert v["passed"] is True, v["failures"]

    def test_url_and_domain_not_treated_as_files(self, tmp_path):
        f = tmp_path / "report.md"
        f.write_text("x", encoding="utf-8")
        v = dispatcher.verify_execution(
            {"acceptance": ["产出 report.md；参考 https://example.com/docs 与 python.org"]},
            {"success": True, "summary": "完成", "tool_calls": 1},
            sandbox_dir=str(tmp_path))
        assert v["passed"] is True, v["failures"]

    def test_non_dict_result_rejected(self):
        v = dispatcher.verify_execution({"acceptance": []}, "garbage")
        assert v["passed"] is False


# ────────────────────── dispatcher_mode 开/关门控 ──────────────────────

class TestDispatcherModeGating:
    """重构轮：调度入口从预防式分支改为工具调用。开=dispatch_worker 预启用+
    提示词指引；关=完全不注册，零影响。"""

    @staticmethod
    def _make_agent(monkeypatch, tmp_path, mode: bool):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"dispatcher_mode": mode}), encoding="utf-8")
        monkeypatch.setattr("agent.agent.get_data_path", lambda name: str(tmp_path / name))
        return OpenAGCAgent(memory_db_path=str(tmp_path / "memory.db"))

    def test_mode_flag_reads_config(self, monkeypatch, tmp_path):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"dispatcher_mode": True}), encoding="utf-8")
        monkeypatch.setattr("agent.agent.get_data_path", lambda name: str(tmp_path / name))
        assert OpenAGCAgent._dispatcher_mode_enabled(object()) is True
        cfg.write_text("{}", encoding="utf-8")
        assert OpenAGCAgent._dispatcher_mode_enabled(object()) is False

    def test_mode_on_registers_and_preenables(self, monkeypatch, tmp_path):
        agent = self._make_agent(monkeypatch, tmp_path, True)
        assert "dispatch_worker" in agent.full_available_tools
        # 预启用：无需 search_available_tools 即可调用
        assert "dispatch_worker" in agent.available_tools
        assert "dispatch_worker" in [
            s["function"]["name"] for s in agent.tool_schemas]
        # 系统提示含调度者角色段（base 组装阶段注入）
        prompt = agent.messages[0]["content"]
        assert "# 角色：调度者" in prompt
        assert "dispatch_worker" in prompt

    def test_mode_off_zero_footprint(self, monkeypatch, tmp_path):
        agent = self._make_agent(monkeypatch, tmp_path, False)
        assert "dispatch_worker" not in agent.full_available_tools
        assert "dispatch_worker" not in agent.available_tools
        prompt = agent.messages[0]["content"]
        assert "# 角色：调度者" not in prompt
        assert "dispatch_worker" not in prompt

    def test_mode_on_hides_dispatch_subagent(self, monkeypatch, tmp_path):
        """dispatcher_mode 下隐藏旧派发入口，避免双入口混淆。"""
        agent = self._make_agent(monkeypatch, tmp_path, True)
        assert "dispatch_subagent" not in agent.full_available_tools
        # 关闭时保留旧入口（零影响面）
        agent_off = self._make_agent(monkeypatch, tmp_path, False)
        assert "dispatch_subagent" in agent_off.full_available_tools

    def test_legacy_delegate_branch_gated(self):
        """dispatcher_mode 下 run_turn 的旧自动委派分支必须被守卫跳过
        （生产实证：_should_delegate 截获任务导致 dispatch_worker 无机会被调用）。"""
        import inspect
        import agent.agent as agent_mod
        run_src = inspect.getsource(agent_mod.OpenAGCAgent.run_turn)
        assert "not self._dispatcher_mode_enabled() and self._should_delegate" in run_src

    def test_preventive_branch_removed(self):
        """重构轮回归守卫：run_turn 开头的预防式调度分支与前缀逻辑已移除。"""
        import inspect
        import agent.agent as agent_mod
        cls_src = inspect.getsource(agent_mod.OpenAGCAgent)
        assert "_should_use_dispatcher" not in cls_src
        assert "_is_actionable_task" not in cls_src
        assert "_dispatcher_fallback_note" not in cls_src
        run_src = inspect.getsource(agent_mod.OpenAGCAgent.run_turn)
        assert "调度失败已接管" not in run_src


# ────────────────────────── SubAgent 全量工具发现 ──────────────────────────

class TestSubAgentFullToolDiscovery:
    def test_worker_unlocks_tool_via_discovery(self):
        extra = _FakeTool("weather_lookup", "查询天气 weather forecast lookup")
        shell = _FakeTool("execute_shell", "run shell commands")
        full_map = {"execute_shell": shell, "weather_lookup": extra}

        llm = _ScriptLLM([
            # 1) 直接调未解锁工具 → 报错并提示先发现
            (None, [_tc("weather_lookup", call_id="c1")]),
            # 2) 走发现机制
            (None, [_tc("search_available_tools", {"query": "weather"}, "c2")]),
            # 3) 再调已解锁工具 → 成功执行
            (None, [_tc("weather_lookup", call_id="c3")]),
            # 4) 文本总结收尾
            ("完成：天气晴", None),
        ])
        sub = SubAgent(
            task="查天气", tools=["execute_shell"], parent_tools=full_map,
            max_iterations=10, llm_client=llm, full_tools_map=full_map,
        )
        # 发现工具已注入，初始工具集不变
        assert "search_available_tools" in sub.available_tools
        assert "weather_lookup" not in sub.available_tools
        assert "weather_lookup" not in [s["function"]["name"] for s in sub.tool_schemas]

        result = sub.run()
        assert result["success"] is True
        assert extra.calls, "解锁后的工具应被真正执行"
        # 第一步的错误提示引导走发现路径
        assert "search_available_tools" in result["steps"][0]["result_preview"]
        assert result["steps"][0]["success"] is False
        # schema 在发现后注入：第 3 次 LLM 调用可见 weather_lookup
        assert "weather_lookup" not in llm.seen_tools[0]
        assert "weather_lookup" not in llm.seen_tools[1]
        assert "weather_lookup" in llm.seen_tools[2]

    def test_backward_compat_without_full_tools_map(self):
        """不传 full_tools_map：维持现状（无发现工具，未知工具报 not available）。"""
        shell = _FakeTool("execute_shell", "run shell commands")
        parent = {"execute_shell": shell}
        llm = _ScriptLLM([
            (None, [_tc("weather_lookup", call_id="c1")]),
            ("无法完成", None),
        ])
        sub = SubAgent(task="t", tools=["execute_shell"], parent_tools=parent,
                       max_iterations=5, llm_client=llm)
        assert "search_available_tools" not in sub.available_tools
        result = sub.run()
        assert "not available in this sub-agent" in result["steps"][0]["result_preview"]


# ────────────────────── dispatch_to_worker 闭环 ──────────────────────

class _StubSubAgent:
    """捕获构造 kwargs，按脚本返回结果。"""
    script = []
    inits = []

    def __init__(self, **kwargs):
        _StubSubAgent.inits.append(kwargs)

    def run(self):
        return _StubSubAgent.script.pop(0)


@pytest.fixture
def stub_worker(monkeypatch):
    _StubSubAgent.script = []
    _StubSubAgent.inits = []
    monkeypatch.setattr("agent.sub_agent.SubAgent", _StubSubAgent)
    return _StubSubAgent


_PKT = {"brief": "B", "relevant_history": [], "memories": [],
        "files": [], "acceptance": []}


class TestDispatchLoop:
    def test_pass_first_attempt(self, monkeypatch, stub_worker):
        monkeypatch.setattr(dispatcher, "enrich_handoff",
                            lambda agent, brief, acceptance=None: dict(_PKT))
        stub_worker.script = [
            {"success": True, "summary": "报表已生成", "tool_calls": 4, "output_files": []},
        ]
        out = dispatcher.dispatch_to_worker(_fake_agent(object()), "分析数据并部署报表")
        assert out["success"] is True
        assert out["summary"] == "报表已生成"   # worker 摘要交主 agent 呈现
        assert out["attempts"] == 1

    def test_retry_once_with_failure_info_then_pass(self, monkeypatch, stub_worker):
        monkeypatch.setattr(dispatcher, "enrich_handoff",
                            lambda agent, brief, acceptance=None: dict(_PKT))
        stub_worker.script = [
            {"success": True, "summary": "空谈", "tool_calls": 0, "output_files": []},
            {"success": True, "summary": "真正完成", "tool_calls": 2, "output_files": []},
        ]
        out = dispatcher.dispatch_to_worker(_fake_agent(object()), "task")
        assert out["success"] is True
        assert out["summary"] == "真正完成"
        assert out["attempts"] == 2
        # 重派任务文本携带上次验收失败信息
        assert "【上次执行未通过验收】" in stub_worker.inits[1]["task"]
        assert "工具调用" in stub_worker.inits[1]["task"]

    def test_double_failure_returns_structured_failure(self, monkeypatch, stub_worker):
        monkeypatch.setattr(dispatcher, "enrich_handoff",
                            lambda agent, brief, acceptance=None: dict(_PKT))
        stub_worker.script = [
            {"success": False, "summary": "err1", "tool_calls": 0, "output_files": []},
            {"success": False, "summary": "err2", "tool_calls": 1, "output_files": []},
        ]
        out = dispatcher.dispatch_to_worker(_fake_agent(object()), "task")
        # 双失败：结构化失败信息交主 agent 接管（不再有 fallback 分支）
        assert out["success"] is False
        assert out["attempts"] == 2
        assert out["verdict"]["failures"]
        assert out["summary"] == "err2"

    def test_worker_exception_converges_to_failure(self, monkeypatch, stub_worker):
        monkeypatch.setattr(dispatcher, "enrich_handoff",
                            lambda agent, brief, acceptance=None: dict(_PKT))

        class _Boom:
            def __init__(self, **kw): pass
            def run(self): raise RuntimeError("worker crashed")
        monkeypatch.setattr("agent.sub_agent.SubAgent", _Boom)
        out = dispatcher.dispatch_to_worker(_fake_agent(object()), "task")
        assert out["success"] is False
        assert "调度执行异常" in out["verdict"]["failures"][0]

    def test_max_iterations_passthrough(self, monkeypatch, stub_worker):
        monkeypatch.setattr(dispatcher, "enrich_handoff",
                            lambda agent, brief, acceptance=None: dict(_PKT))
        stub_worker.script = [
            {"success": True, "summary": "ok", "tool_calls": 1, "output_files": []},
        ]
        dispatcher.dispatch_to_worker(_fake_agent(object()), "task", max_iterations=5)
        assert stub_worker.inits[0]["max_iterations"] == 5

    def test_progress_events_labeled(self, monkeypatch):
        """进度事件 sub_task 标「调度执行」。"""
        events = []
        cb = dispatcher._label_progress(events.append)
        cb({"event": "tool_start", "tool": "execute_shell", "sub_task": "原任务名"})
        assert events[0]["sub_task"] == "调度执行"
        # 无 sub_task 字段的事件原样透传
        cb({"event": "thinking"})
        assert events[1] == {"event": "thinking"}


# ────────────────────── 评审修复轮（review-dispatcher-m1）──────────────────────

class TestFileRefExtraction:
    """I-3：验收文件引用提取——CJK 粘连 / Windows 盘符 / 库名误报。"""

    def test_cjk_glued_ascii_filename(self):
        assert dispatcher._extract_file_refs("产出文件report.html存在") == ["report.html"]
        assert dispatcher._extract_file_refs("应生成output/result.txt供查看") == ["output/result.txt"]

    def test_windows_drive_preserved(self):
        assert dispatcher._extract_file_refs("写入 D:/work/out.txt 后停止") == ["D:/work/out.txt"]
        assert dispatcher._extract_file_refs("输出到 D:\\logs\\a.log 即可") == ["D:\\logs\\a.log"]

    def test_runtime_and_lib_names_filtered(self):
        assert dispatcher._extract_file_refs("node.js 版本需 >= 18") == []
        assert dispatcher._extract_file_refs("使用 react.js 渲染页面") == []

    def test_pure_cjk_filename_kept(self):
        assert dispatcher._extract_file_refs("生成 报表.html 后停止") == ["报表.html"]

    def test_version_numbers_not_files(self):
        assert dispatcher._extract_file_refs("要求 Python 3.10 及以上") == []


class TestVerifyNonEmpty:
    """Minor-2：验收与 prompt 示例「存在且非空」对齐。"""

    def test_reject_empty_output_file(self, tmp_path):
        (tmp_path / "empty.txt").write_text("", encoding="utf-8")
        v = dispatcher.verify_execution(
            {"acceptance": ["产出文件 empty.txt 存在且非空"]},
            {"success": True, "summary": "done", "tool_calls": 2, "output_files": []},
            sandbox_dir=str(tmp_path))
        assert v["passed"] is False
        assert any("为空" in f for f in v["failures"])

    def test_nonempty_passes(self, tmp_path):
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        v = dispatcher.verify_execution(
            {"acceptance": ["产出文件 a.txt 存在且非空"]},
            {"success": True, "summary": "done", "tool_calls": 1, "output_files": []},
            sandbox_dir=str(tmp_path))
        assert v["passed"] is True, v["failures"]


class TestInterruptSemantics:
    """I-1：中断联动——worker 响应主 agent 中断；dispatch 不重派。
    重构轮后调用发生在主循环内，主循环每迭代自查 is_interrupted，语义结构性一致。"""

    def test_worker_stops_on_external_interrupt(self):
        flag = {"stop": False}

        class _StopTool(_FakeTool):
            def execute(self, **kwargs):
                flag["stop"] = True
                return "OK"

        shell = _StopTool("execute_shell", "run shell")
        llm = _ScriptLLM([
            (None, [_tc("execute_shell", call_id="c1")]),
            ("不应到达", None),  # 第二次迭代前中断生效，不会消费到这条
        ])
        sub = SubAgent(
            task="t", tools=["execute_shell"], parent_tools={"execute_shell": shell},
            max_iterations=10, llm_client=llm,
            external_interrupt_check=lambda: flag["stop"],
        )
        result = sub.run()
        assert result["success"] is False
        assert "Interrupted" in result["summary"]
        assert len(llm.seen_tools) == 1  # 中断后没有再调 LLM

    def test_dispatch_no_retry_on_interrupt(self, monkeypatch):
        monkeypatch.setattr(dispatcher, "enrich_handoff",
                            lambda a, brief, acceptance=None: dict(_PKT))
        agent = _fake_agent(object())

        class _StopWorker:
            def __init__(self, **kw): pass
            def run(self):
                agent.is_interrupted = True  # worker 执行期间用户点了中断
                return {"success": False, "summary": "Interrupted",
                        "tool_calls": 1, "output_files": []}
        monkeypatch.setattr("agent.sub_agent.SubAgent", _StopWorker)
        out = dispatcher.dispatch_to_worker(agent, "task")
        assert out["attempts"] == 1  # 中断不重派
        assert out["success"] is False  # 主循环下一迭代按中断语义返回


class TestPendingInjection:
    """M2：worker 插话只读专属队列（message_worker 注入的已分类追加指令），
    主 agent 的原始 pending_messages（含闲聊）不再转发——分类是主 agent 职责。"""

    def test_provider_reads_only_worker_inbox(self):
        agent = SimpleNamespace(pending_messages=["今天天气怎么样（闲聊）"],
                                session_id=7, task_id=77)
        provider = dispatcher._make_pending_provider(agent)
        assert provider() == ""  # 闲聊不进 worker
        dispatcher.push_worker_inbox(7, 77, "别忘了改端口")
        text = provider()
        assert "别忘了改端口" in text
        assert "调度者转发" in text
        assert provider() == ""  # 同一条不重复
        assert agent.pending_messages == ["今天天气怎么样（闲聊）"]  # 主队列不受影响

    def test_worker_receives_forwarded_message(self):
        shell = _FakeTool("execute_shell", "run shell")
        llm = _ScriptLLM([
            (None, [_tc("execute_shell", call_id="c1")]),
            ("完成", None),
        ])
        calls = {"n": 0}

        def provider():
            calls["n"] += 1
            return "【调度转发】用户插话：改成 UTF-8" if calls["n"] == 2 else ""

        sub = SubAgent(
            task="t", tools=["execute_shell"],
            parent_tools={"execute_shell": shell}, max_iterations=5, llm_client=llm,
            pending_message_provider=provider,
        )
        result = sub.run()
        assert result["success"] is True
        # 第二次 LLM 调用的上下文里能看到转发的插话
        assert any("用户插话：改成 UTF-8" in str(m.get("content", ""))
                   for m in llm.seen_messages[1])


class TestHistoryKeywords:
    """Minor-1：标识符下划线保留 + LIKE ESCAPE 转义。"""

    def test_keywords_keep_underscore(self):
        kws = dispatcher._history_keywords("修复 login_button 的样式问题")
        assert "login_button" in kws

    def test_like_escape(self):
        assert dispatcher._like_escape("login_button") == "login\\_button"
        assert dispatcher._like_escape("100%") == "100\\%"

    def test_history_search_hits_underscore_identifier(self, monkeypatch, tmp_path):
        import sqlite3
        db = tmp_path / "chat_history.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT, "
                     "user_query TEXT, result_summary TEXT, status TEXT)")
        conn.execute("CREATE TABLE task_steps (task_id INTEGER, step_number INTEGER, "
                     "tool_name TEXT, tool_label TEXT, success INTEGER)")
        conn.execute("INSERT INTO tasks VALUES (1, 'fix login_button css', "
                     "'修复登录按钮', '完成', 'completed')")
        conn.execute("INSERT INTO task_steps VALUES (1, 1, 'edit_file', NULL, 1)")
        conn.commit()
        conn.close()
        monkeypatch.setattr("core.paths.get_data_path", lambda name: str(tmp_path / name))
        hits = dispatcher._fetch_relevant_history("再修一下 login_button 的样式")
        assert hits and hits[0]["task_id"] == 1
        assert hits[0]["key_steps"] == ["edit_file"]


class TestDispatchWorkerTool:
    """dispatch_worker 工具：brief 传递、检索增强调用、验收结论返回。"""

    def test_schema(self):
        fn = DispatchWorkerTool().get_openai_schema()["function"]
        assert fn["name"] == "dispatch_worker"
        props = fn["parameters"]["properties"]
        assert set(props) == {"task_brief", "acceptance", "max_iterations"}
        assert fn["parameters"]["required"] == ["task_brief"]

    def test_requires_agent_context(self):
        out = DispatchWorkerTool().execute(task_brief="简报")
        assert out.startswith("Error")

    def test_requires_llm(self):
        out = DispatchWorkerTool().execute(
            task_brief="简报", _agent_context=SimpleNamespace(llm=None))
        assert out.startswith("Error")

    def test_empty_brief_rejected(self):
        out = DispatchWorkerTool().execute(
            task_brief="  ", _agent_context=SimpleNamespace(llm=object()))
        assert "task_brief" in out

    def test_acceptance_parsing(self):
        assert _parse_acceptance(None) == []
        assert _parse_acceptance(["a", " ", "b", "c", "d"]) == ["a", "b", "c"]
        assert _parse_acceptance('["x", "y"]') == ["x", "y"]
        assert _parse_acceptance("标准1\n标准2") == ["标准1", "标准2"]
        assert _parse_acceptance("单条文本") == ["单条文本"]

    def test_brief_passed_and_result_serialized(self, monkeypatch):
        """M2 异步化：brief/验收原样进 dispatch_async；工具立即返回已开工指引。"""
        seen = {}

        def _stub_async(agent, brief, acceptance=None, max_iterations=None,
                        progress_callback=None):
            seen.update(brief=brief, acceptance=acceptance,
                        max_iterations=max_iterations, progress_callback=progress_callback)
            return {"dispatched": True}

        monkeypatch.setattr(dispatcher, "dispatch_async", _stub_async)
        out = DispatchWorkerTool().execute(
            task_brief="目标：写报表；产出 report.html",
            acceptance=["产出 report.html 存在且非空"],
            max_iterations=7,
            _agent_context=SimpleNamespace(llm=object()),
            _progress_cb="CB",
        )
        result = json.loads(out)
        assert result["dispatched"] is True
        assert "不要空等" in result["note"]
        assert seen["brief"] == "目标：写报表；产出 report.html"
        assert seen["acceptance"] == ["产出 report.html 存在且非空"]
        assert seen["max_iterations"] == 7
        assert seen["progress_callback"] == "CB"

    def test_async_note_guides_interjection_classification(self):
        """工具返回指引包含插话分类职责（M2 三态）。"""
        from unittest.mock import patch
        with patch.object(dispatcher, "dispatch_async", return_value={"dispatched": True}):
            out = DispatchWorkerTool().execute(
                task_brief="简报", _agent_context=SimpleNamespace(llm=object()))
        note = json.loads(out)["note"]
        assert "message_worker" in note and "闲聊" in note


class TestRolePrompts:
    """角色提示词重写（用户要求）：调度者与执行者提示词按角色设计。"""

    def test_dispatcher_prompt_structure(self):
        src = open(os.path.join(PROJECT_ROOT, "agent", "agent.py"),
                   encoding="utf-8").read()
        for needle in ("角色：调度者", "分流", "亲自撰写", "task_brief",
                       "验收标准 acceptance", "验收未通过", "接管", "呈现"):
            assert needle in src, f"调度者提示词缺少: {needle}"
        # 只在模式开启时注入（组装分支）
        assert "if self._dispatcher_mode_enabled():" in src

    def test_worker_prompt_structure(self):
        from agent.sub_agent import SubAgent
        sub = SubAgent(task="测试任务简报", tools=[], parent_tools={},
                       llm_client=None)
        prompt = sub.messages[0]["content"]
        for needle in ("你是执行者", "任务简报", "工作纪律", "禁止只描述计划",
                       "交付汇报", "产出清单", "严禁假装完成"):
            assert needle in prompt, f"执行者提示词缺少: {needle}"


# ────────────────────────── 虚构交付拦截 ──────────────────────────

_FAB = ("✅ 编译完成！验收通过，Windows 桌面端 ASR 链路已跑通。"
        "whisper.cpp v1.8.4 编译完成，模型已下载，识别验证准确无误，"
        "性能实测 570ms。交付物：workspace/whisper_build/，文件位置：如上。"
        "性能（small 模型）6 秒音频识别耗时 570ms，约 10 倍实时速度。")


class TestFabricationGuard:
    """虚构交付拦截（生产实证：主 agent 零工具调用编造「编译完成/验收通过」，
    用户识破）。dispatcher_mode 下零工具 + 交付声明 → 注入纠错重跑一轮。"""

    def _make_agent(self, monkeypatch, tmp_path, script):
        from tests.test_agent_reliability import _bare_agent
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"dispatcher_mode": True}), encoding="utf-8")
        monkeypatch.setattr("agent.agent.get_data_path",
                            lambda name: str(tmp_path / name))
        agent = _bare_agent()
        agent.session_id = 1
        agent.llm = _ScriptLLM(script)
        return agent

    def test_zero_tool_fabricated_delivery_blocked_and_rerun(self, monkeypatch, tmp_path):
        agent = self._make_agent(monkeypatch, tmp_path, [
            (_FAB, None),                          # 第一轮：零工具虚构交付 → 拦截重跑
            ("派发：已派执行者去真实编译。", None),     # 第二轮：正常调度者回应
        ])
        out = agent.run_turn("开工", verbose=False, skip_rag=True)
        assert "派发" in out
        assert len(agent.llm.seen_messages) == 2
        second_call_msgs = agent.llm.seen_messages[1]
        assert any(m.get("role") == "system" and "虚构交付" in str(m.get("content", ""))
                   for m in second_call_msgs)

    def test_guard_fires_at_most_once(self, monkeypatch, tmp_path):
        agent = self._make_agent(monkeypatch, tmp_path, [
            (_FAB, None),
            (_FAB + "（第二版）", None),
        ])
        out = agent.run_turn("开工", verbose=False, skip_rag=True)
        # 第二轮仍命中 → 放行（防循环），仅拦截一次
        assert "第二版" in out
        assert len(agent.llm.seen_messages) == 2

    def test_pure_qa_not_blocked(self, monkeypatch, tmp_path):
        agent = self._make_agent(monkeypatch, tmp_path, [
            ("可以做到。方案是三端兼容：桌面用 whisper.cpp，移动端用 ……" + "分析" * 100, None),
        ])
        out = agent.run_turn("能做到吗", verbose=False, skip_rag=True)
        assert "可以做到" in out
        assert len(agent.llm.seen_messages) == 1  # 无重跑

    def test_with_tool_calls_not_blocked(self, monkeypatch, tmp_path):
        # 第一轮发起工具调用（工具不存在 → 产生 role=tool 错误消息，即
        # 本轮有真实工具动作），第二轮交付声明 —— 不拦
        agent = self._make_agent(monkeypatch, tmp_path, [
            ("", [_tc("nonexistent_tool_xyz", {})]),
            (_FAB, None),
        ])
        out = agent.run_turn("开工", verbose=False, skip_rag=True)
        assert "验收通过" in out
        assert len(agent.llm.seen_messages) == 2
        assert not any(m.get("role") == "system" and "虚构交付" in str(m.get("content", ""))
                       for m in agent.messages)


# ────────────────────────── M2：异步派发与插话分类 ──────────────────────────

class TestDispatchAsync:
    """dispatch_async：立即返回；后台闭环完成后注入【执行者返回】并唤醒。"""

    def _agent(self, **kw):
        base = dict(session_id=7, task_id=77, pending_messages=[],
                    llm=object(), sandbox_dir=None, is_interrupted=False)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_async_returns_immediately_and_notifies_live_agent(self, monkeypatch):
        monkeypatch.setattr(dispatcher, "dispatch_to_worker",
                            lambda *a, **kw: {
                                "success": True, "summary": "完成了编译",
                                "verdict": {"passed": True, "failures": []},
                                "result": {"output_files": ["out.exe"]}})
        agent = self._agent()
        # live 判定：把 agent 塞进 _active_agents
        import api.state as state
        state._active_agents.setdefault(7, {})[77] = agent
        try:
            out = dispatcher.dispatch_async(agent, "简报")
            assert out["dispatched"] is True
            with dispatcher._running_lock:
                d = dispatcher._running_dispatches.get((7, 77))
            assert d is not None and d["thread"] is not None
            d["thread"].join(timeout=10)
            assert agent.pending_messages, "完成后应注入【执行者返回】"
            note = agent.pending_messages[0]
            assert "执行者返回" in note and "验收通过" in note and "out.exe" in note
            # 完成后不再视为运行中
            assert dispatcher.get_running_dispatch(7, 77) is None
        finally:
            state._active_agents.get(7, {}).pop(77, None)

    def test_async_resume_when_turn_ended(self, monkeypatch):
        monkeypatch.setattr(dispatcher, "dispatch_to_worker",
                            lambda *a, **kw: {
                                "success": False, "summary": "失败摘要",
                                "verdict": {"passed": False,
                                            "failures": ["产出文件不存在: x"]},
                                "result": {}})
        agent = self._agent()  # 不在 _active_agents → turn 已结束
        calls = {}

        def _stub_resume(task_id, extra_instruction=""):
            calls.update(task_id=task_id, extra=extra_instruction)
            return {"ok": True, "status": "resumed"}

        monkeypatch.setattr("api.background.resume_task_manual", _stub_resume)
        out = dispatcher.dispatch_async(agent, "简报")
        key = (7, 77)
        import time as _t
        for _ in range(50):  # 最多等 5s
            with dispatcher._running_lock:
                if dispatcher._running_dispatches.get(key, {}).get("done"):
                    break
            _t.sleep(0.1)
        for _ in range(50):
            if calls:
                break
            _t.sleep(0.1)
        assert calls.get("task_id") == 77
        assert "执行者返回" in calls["extra"]
        assert "验收未通过" in calls["extra"]
        assert "产出文件不存在" in calls["extra"]


class TestWorkerInbox:
    def test_inbox_isolated_per_task(self):
        dispatcher.push_worker_inbox(1, 10, "任务A的追加")
        dispatcher.push_worker_inbox(1, 20, "任务B的追加")
        a = dispatcher._pop_worker_inbox(1, 10)
        b = dispatcher._pop_worker_inbox(1, 20)
        assert a == ["任务A的追加"] and b == ["任务B的追加"]
        assert dispatcher._pop_worker_inbox(1, 10) == []


class TestMessageWorkerTool:
    def _tool(self):
        from tools.message_worker import MessageWorkerTool
        return MessageWorkerTool()

    def test_no_running_dispatch_rejected(self):
        out = self._tool().execute(
            message="补一句", _agent_context=SimpleNamespace(session_id=9, task_id=99))
        r = json.loads(out)
        assert r["delivered"] is False
        assert "没有运行中" in r["note"]

    def test_delivers_to_running_dispatch(self):
        key = (9, 99)
        with dispatcher._running_lock:
            dispatcher._running_dispatches[key] = {"done": False, "result": None}
        try:
            out = self._tool().execute(
                message="端口改成 8080",
                _agent_context=SimpleNamespace(session_id=9, task_id=99))
            r = json.loads(out)
            assert r["delivered"] is True
            assert dispatcher._pop_worker_inbox(9, 99) == ["端口改成 8080"]
        finally:
            with dispatcher._running_lock:
                dispatcher._running_dispatches.pop(key, None)

    def test_empty_message_rejected(self):
        out = self._tool().execute(
            message="  ", _agent_context=SimpleNamespace(session_id=9, task_id=99))
        assert "Error" in out

    def test_requires_agent_context(self):
        assert "Error" in self._tool().execute(message="x")


class TestDispatcherPromptM2:
    def test_prompt_has_interjection_classification(self):
        src = open(os.path.join(PROJECT_ROOT, "agent", "agent.py"),
                   encoding="utf-8").read()
        for needle in ("插话分类", "message_worker", "异步", "不要空等",
                       "另派一个执行者"):
            assert needle in src, f"M2 提示词缺少: {needle}"

    def test_message_worker_registered_in_dispatcher_mode(self, monkeypatch, tmp_path):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"dispatcher_mode": True}), encoding="utf-8")
        monkeypatch.setattr("agent.agent.get_data_path", lambda name: str(tmp_path / name))
        agent = OpenAGCAgent(memory_db_path=str(tmp_path / "memory.db"))
        assert "message_worker" in agent.full_available_tools
        assert "message_worker" in agent.available_tools
        # 关闭模式零痕迹
        cfg.write_text(json.dumps({"dispatcher_mode": False}), encoding="utf-8")
        agent2 = OpenAGCAgent(memory_db_path=str(tmp_path / "memory2.db"))
        assert "message_worker" not in agent2.full_available_tools
