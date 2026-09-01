# -*- coding: utf-8 -*-
"""interrupt 机制测试：_interrupt_session_agents 必须中断本会话所有前台/后台
agent（修复外圈 WS 循环缺失 interrupt 分支导致异步/后台时中断被丢弃）。"""
import types

import pytest

from api import ws as api_ws


@pytest.fixture(autouse=True)
def _clean_agent_stores():
    api_ws._active_agents.clear()
    api_ws._background_agents.clear()
    yield
    api_ws._active_agents.clear()
    api_ws._background_agents.clear()


def _agent(session_id):
    return types.SimpleNamespace(is_interrupted=False, session_id=session_id)


class TestInterruptSessionAgents:
    def test_interrupts_foreground_agents_of_session(self):
        a1 = _agent(1)
        a2 = _agent(1)
        api_ws._active_agents[1] = {5: a1, 6: a2}
        fg, bg = api_ws._interrupt_session_agents(1)
        assert a1.is_interrupted and a2.is_interrupted
        assert sorted(fg) == [5, 6]
        assert bg == []

    def test_interrupts_background_agents_of_session_only(self):
        mine = _agent(1)
        other = _agent(2)
        api_ws._background_agents[10] = mine
        api_ws._background_agents[11] = other
        fg, bg = api_ws._interrupt_session_agents(1)
        assert mine.is_interrupted
        assert not other.is_interrupted  # 不动其它会话的后台任务
        assert bg == [10]

    def test_mixed_foreground_and_background(self):
        a = _agent(1)
        b = _agent(1)
        api_ws._active_agents[1] = {5: a}
        api_ws._background_agents[10] = b
        fg, bg = api_ws._interrupt_session_agents(1)
        assert a.is_interrupted and b.is_interrupted
        assert fg == [5] and bg == [10]

    def test_empty_session_is_noop(self):
        fg, bg = api_ws._interrupt_session_agents(999)
        assert fg == [] and bg == []

    def test_zero_task_key_not_returned_as_task_id(self):
        a = _agent(1)
        api_ws._active_agents[1] = {0: a}  # task_id 未知时注册在 0 键
        fg, bg = api_ws._interrupt_session_agents(1)
        assert a.is_interrupted
        assert fg == []  # 0 不是有效 task_id，不参与进程清理
