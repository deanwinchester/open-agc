# -*- coding: utf-8 -*-
"""继续执行与后台承诺的硬约束回归（生产实证：Strata 任务反复给选项不执行）。"""
import json

from tests.test_agent_reliability import _bare_agent
from tests.test_dispatcher_m1 import _ScriptLLM


def _agent(monkeypatch, tmp_path, script):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"dispatcher_mode": False}), encoding="utf-8")
    monkeypatch.setattr("agent.agent.get_data_path", lambda name: str(tmp_path / name))
    agent = _bare_agent()
    agent.session_id = 1
    agent.llm = _ScriptLLM(script)
    return agent


def test_continue_with_zero_tools_is_sent_back_to_execute(monkeypatch, tmp_path):
    answer = "两条路，你选：A. 装插件继续；B. 不用 Strata。你确认后我就继续。"
    agent = _agent(monkeypatch, tmp_path, [
        (answer, None),
        ("我先检查进程、模型文件和下载状态，再继续执行。", None),
    ])
    out = agent.run_turn("继续", skip_rag=True)
    assert len(agent.llm.seen_messages) == 2
    assert "我先检查" in out
    assert any(m.get("role") == "system" and "不得重复询问" in str(m.get("content"))
               for m in agent.llm.seen_messages[1])


def test_continue_guard_runs_at_most_once(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path, [
        ("你确认后我就继续。", None),
        ("仍需你确认。", None),
    ])
    assert agent.run_turn("继续", skip_rag=True) == "仍需你确认。"
    assert len(agent.llm.seen_messages) == 2


def test_download_completion_promise_requires_background_handoff(monkeypatch, tmp_path):
    answer = ("MLX 已装好，Strata 正在跑 Flash-Next，模型已开下。"
              "下载完 Strata 会自动调优并启动服务，当前已写入 0.4GB。"
              "镜像已经恢复，支持断点续传，服务默认使用 OpenAI 兼容接口。")
    agent = _agent(monkeypatch, tmp_path, [
        (answer, None),
        ("下载尚未完成，当前任务需要手动发送「继续」恢复。", None),
    ])
    out = agent.run_turn("strata 跑 flash next，顺便安装 mlx", skip_rag=True)
    assert len(agent.llm.seen_messages) == 2
    assert "手动" in out
    assert any(m.get("role") == "system" and "pause_and_wait" in str(m.get("content"))
               for m in agent.llm.seen_messages[1])
