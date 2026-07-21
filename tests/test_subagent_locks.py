"""阶段 4 Task 2 回归测试：子代理锁竞态、SandboxBlocked 透传、沙箱等待唯一键与可中断等待。

- 同名有状态工具（browser_automation）在并行子代理间必须串行执行
- SubAgent.run 不得把 SandboxBlocked 吞成字符串，必须重新抛出
- _sandbox_waits 以唯一 request_id 为键：同会话两个并发等待互不覆盖
- 分段等待（wait(1) 循环）必须在 is_interrupted 置位后快速返回，而非睡满 120s
"""
import threading
import time
import types

import pytest

from agent.sub_agent import SubAgent
from tools.base import SandboxBlocked


# ── 测试替身 ──────────────────────────────────────────────────────────

class _FakeLLM:
    """第一次 chat() 返回一个工具调用，第二次返回最终文本。"""

    def __init__(self, tool_name):
        self.tool_name = tool_name
        self.calls = 0

    def chat(self, messages=None, tools=None):
        self.calls += 1
        if self.calls == 1:
            tc = types.SimpleNamespace(
                id="call_1",
                type="function",
                function=types.SimpleNamespace(
                    name=self.tool_name, arguments="{}"),
            )
            msg = types.SimpleNamespace(content="", tool_calls=[tc])
        else:
            msg = types.SimpleNamespace(content="done", tool_calls=None)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=msg)]), {}


class _SleepTool:
    """记录进入/退出顺序的有状态工具替身（用于验证互斥）。"""

    def __init__(self, name, log):
        self.name = name
        self.log = log

    def get_openai_schema(self):
        return {"type": "function",
                "function": {"name": self.name, "parameters": {}}}

    def execute(self, **kwargs):
        self.log.append("enter")
        time.sleep(0.15)
        self.log.append("exit")
        return "ok"


class _BlockedTool:
    """总是抛出 SandboxBlocked 的工具替身。"""

    def __init__(self, name):
        self.name = name

    def get_openai_schema(self):
        return {"type": "function",
                "function": {"name": self.name, "parameters": {}}}

    def execute(self, **kwargs):
        raise SandboxBlocked("/outside/path", "/sandbox", self.name)


def _bare_agent(session_id=1):
    """绕过重量级 __init__，仅装配 _handle_sandbox_blocked 所需属性。"""
    from agent.agent import OpenAGCAgent
    a = OpenAGCAgent.__new__(OpenAGCAgent)
    a.session_id = session_id
    a.is_interrupted = False
    a._session_sandbox_whitelist = set()
    a._session_network_whitelist = set()
    a._session_permission_whitelist = set()
    return a


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# ── Fix 1：锁创建竞态 ─────────────────────────────────────────────────

def test_tool_locks_preinitialized_for_all_stateful_tools():
    """类定义时即预建好全部有状态工具的锁，消除 check-then-act 窗口。"""
    for name in SubAgent.STATEFUL_TOOLS:
        assert name in SubAgent._tool_locks


def test_stateful_tool_serialized_across_parallel_subagents():
    """两个并行子代理调用同名有状态工具：执行区间不得交叠。"""
    log = []

    def _run_one():
        tool = _SleepTool("browser_automation", log)
        sub = SubAgent(
            task="t", tools=["browser_automation"],
            parent_tools={"browser_automation": tool},
            llm_client=_FakeLLM("browser_automation"),
        )
        sub.run()

    threads = [threading.Thread(target=_run_one) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive()

    assert len(log) == 4
    # 互斥下日志必须是 enter/exit 成对交替；并发进入会出现 enter,enter
    for i in range(0, len(log), 2):
        assert log[i] == "enter" and log[i + 1] == "exit", \
            f"stateful tool execution overlapped: {log}"


# ── Fix 2：SandboxBlocked 透传 ────────────────────────────────────────

def test_sandbox_blocked_reraised_not_swallowed():
    """子代理不得把 SandboxBlocked 吞成错误字符串，必须抛给主 agent。"""
    tool = _BlockedTool("read_file")
    sub = SubAgent(
        task="t", tools=["read_file"],
        parent_tools={"read_file": tool},
        llm_client=_FakeLLM("read_file"),
    )
    with pytest.raises(SandboxBlocked):
        sub.run()


def test_sandbox_blocked_reraise_releases_stateful_lock():
    """有状态工具内抛出 SandboxBlocked 时锁必须已释放（finally 生效）。"""
    tool = _BlockedTool("browser_automation")
    sub = SubAgent(
        task="t", tools=["browser_automation"],
        parent_tools={"browser_automation": tool},
        llm_client=_FakeLLM("browser_automation"),
    )
    with pytest.raises(SandboxBlocked):
        sub.run()
    lock = SubAgent._tool_locks["browser_automation"]
    assert lock.acquire(blocking=False), "stateful lock leaked after SandboxBlocked"
    lock.release()


# ── Fix 3：_sandbox_waits 唯一键 + 可中断分段等待 ─────────────────────

def test_concurrent_sandbox_waits_do_not_clobber():
    """同会话两个并发沙箱等待：互不覆盖，按 request_id 各自响应。"""
    from api.server import _sandbox_waits
    agent = _bare_agent(session_id=1)
    events = []
    results = {}

    def _cb(ev):
        events.append(ev)

    def _wait(path):
        sb = SandboxBlocked(path, "/sandbox", "read_file")
        results[path] = agent._handle_sandbox_blocked(
            sb, "read_file", {}, _cb)

    t1 = threading.Thread(target=_wait, args=("/p1",))
    t2 = threading.Thread(target=_wait, args=("/p2",))
    t1.start()
    t2.start()
    try:
        # 两个等待都注册完成（事件里带唯一 request_id）
        # 注意 SandboxBlocked 会对路径做 os.path.abspath（Windows 下 /p1 → D:\p1）
        assert _wait_for(lambda: len([e for e in events if e.get("request_id")]) == 2)
        import os as _os
        p1, p2 = _os.path.abspath("/p1"), _os.path.abspath("/p2")
        rid_by_path = {e["path"]: e["request_id"] for e in events
                       if e.get("request_id")}
        assert len(set(rid_by_path.values())) == 2, "request_id 必须唯一"
        # 两个条目同时存在于 _sandbox_waits（旧实现第二个会覆盖第一个）
        assert all(rid in _sandbox_waits for rid in rid_by_path.values())
        # 旧版 session 键仍保留（向后兼容回退）
        assert _sandbox_waits.get(1) is not None

        # 只响应 /p1 的等待
        rid1 = rid_by_path[p1]
        _sandbox_waits[rid1]["result"]["action"] = "deny_once"
        _sandbox_waits[rid1]["event"].set()
        t1.join(timeout=5)
        assert not t1.is_alive(), "被响应的等待应立即返回"
        assert "denied" in results["/p1"].lower()
        # /p2 的等待不受影响，仍在阻塞
        assert t2.is_alive(), "未被响应的等待不得被串扰"

        # 再响应 /p2
        rid2 = rid_by_path[p2]
        _sandbox_waits[rid2]["result"]["action"] = "deny_once"
        _sandbox_waits[rid2]["event"].set()
        t2.join(timeout=5)
        assert not t2.is_alive()
        assert "denied" in results["/p2"].lower()

        # 两个条目都被清理
        assert rid1 not in _sandbox_waits and rid2 not in _sandbox_waits
    finally:
        for t in (t1, t2):
            if t.is_alive():
                # 兜底：放行仍在等待的线程，避免悬挂
                for e in events:
                    rid = e.get("request_id")
                    if rid and rid in _sandbox_waits:
                        _sandbox_waits[rid]["result"]["action"] = "deny_once"
                        _sandbox_waits[rid]["event"].set()
                t.join(timeout=5)


def test_sandbox_wait_responds_to_interrupt_quickly():
    """is_interrupted 置位后分段等待应在 ~1s 量级返回，而非睡满 120s。"""
    agent = _bare_agent(session_id=2)
    events = []
    out = {}

    def _run():
        sb = SandboxBlocked("/p3", "/sandbox", "read_file")
        out["r"] = agent._handle_sandbox_blocked(
            sb, "read_file", {}, lambda e: events.append(e))

    t = threading.Thread(target=_run)
    t.start()
    assert _wait_for(lambda: len(events) == 1), "等待条目未注册"
    start = time.time()
    agent.is_interrupted = True
    t.join(timeout=10)
    elapsed = time.time() - start
    assert not t.is_alive(), "中断后等待仍未退出"
    assert elapsed < 10, f"中断响应太慢: {elapsed:.1f}s（应远小于 120s 超时）"
    assert "interrupted" in out["r"].lower()
