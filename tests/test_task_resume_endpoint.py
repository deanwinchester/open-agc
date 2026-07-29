"""中断原因清理 + 手动恢复端点（POST /api/tasks/{id}/resume）回归测试。

Bug 1（中断原因误显）：任务被认领恢复后 interruption_reason 仍保留上一次
中断的历史原因（如 server_restart），前端「中断原因」区块对进行中/已完成
任务继续展示，严重误导。修复点：
- claim_task_for_resume CAS 认领恢复时一并清 interruption_reason=NULL；
- update_task_status 翻转 running/completed（未显式传 reason）时清 NULL；
- complete_task REST 端点的直连 SQL 同样清 NULL。

Bug 2（任务详情页「继续」按钮丢失）：新增 POST /api/tasks/{id}/resume
（body {extra_instruction?}，可空），与 WS {type:'resume'} 同一恢复链路：
状态校验（仅 interrupted/backgrounded/background_failed/failed/completed
可恢复，running 409、不存在 404、认领冲突 409）→ CAS 认领 → 附加指令注入
恢复上下文 → _run_background_task 后台恢复；活后台 agent 持有任务时指令
排队投递（不另开恢复线程）。
"""
import asyncio
import os
import sys
import types

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """把 api.db.DB_PATH 指到临时库；routes_tasks 持有 DB_PATH 值引用，同步指过去。"""
    import api.db as db_mod
    db_file = str(tmp_path / "test.db")
    monkeypatch.setattr(db_mod, "DB_PATH", db_file)
    db_mod.init_db()
    import api.routes.routes_tasks as rt
    monkeypatch.setattr(rt, "DB_PATH", db_file)
    return db_mod


@pytest.fixture()
def fake_bg_threads(monkeypatch):
    """记录 api.background 内 threading.Thread 的创建（不真正启动 worker）。"""
    import api.background as bg
    spawned = []

    class _FakeThread:
        def __init__(self, target=None, args=(), daemon=None, **kw):
            spawned.append({"target": target, "args": args})

        def start(self):
            pass

    monkeypatch.setattr(bg, "threading", types.SimpleNamespace(Thread=_FakeThread))
    return bg, spawned


def _insert_task(db_mod, status="interrupted", interruption_reason="server_restart",
                 user_query="原始查询"):
    conn = db_mod.db_connect()
    cur = conn.execute(
        "INSERT INTO tasks (title, user_query, status, interruption_reason, session_id) "
        "VALUES (?, ?, ?, ?, 1)",
        ("测试任务", user_query, status, interruption_reason))
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def _task_state(db_mod, tid):
    conn = db_mod.db_connect()
    row = conn.execute(
        "SELECT status, resume_count, interruption_reason FROM tasks WHERE id=?",
        (tid,)).fetchone()
    conn.close()
    return {"status": row[0], "resume_count": row[1], "interruption_reason": row[2]}


# ---------- 中断原因清理 ----------

class TestInterruptionReasonCleanup:
    def test_claim_clears_interruption_reason(self, tmp_db):
        """CAS 认领恢复：status→running、resume_count+1，同时清历史中断原因。"""
        from api.task_core import claim_task_for_resume
        tid = _insert_task(tmp_db, status="interrupted",
                           interruption_reason="server_restart")
        assert claim_task_for_resume(tid, ('interrupted',)) is True
        st = _task_state(tmp_db, tid)
        assert st["status"] == "running"
        assert st["resume_count"] == 1
        assert st["interruption_reason"] is None

    def test_claim_failure_keeps_reason(self, tmp_db):
        """认领失败（状态不在允许集合）：状态与原因原样保留。"""
        from api.task_core import claim_task_for_resume
        tid = _insert_task(tmp_db, status="interrupted",
                           interruption_reason="server_restart")
        assert claim_task_for_resume(tid, ('backgrounded',)) is False
        st = _task_state(tmp_db, tid)
        assert st["status"] == "interrupted"
        assert st["interruption_reason"] == "server_restart"

    def test_update_status_running_clears_reason(self, tmp_db):
        """update_task_status 翻转 running（未显式传 reason）：清历史原因。"""
        from api.task_core import update_task_status
        tid = _insert_task(tmp_db, status="interrupted",
                           interruption_reason="max_iterations")
        update_task_status(tid, "running")
        st = _task_state(tmp_db, tid)
        assert st["status"] == "running"
        assert st["interruption_reason"] is None

    def test_update_status_completed_clears_reason(self, tmp_db):
        """update_task_status 收官 completed：清历史原因。"""
        from api.task_core import update_task_status
        tid = _insert_task(tmp_db, status="running",
                           interruption_reason="server_restart")
        update_task_status(tid, "completed", "done")
        st = _task_state(tmp_db, tid)
        assert st["status"] == "completed"
        assert st["interruption_reason"] is None

    def test_update_status_interrupted_keeps_reason(self, tmp_db):
        """显式传 reason 的中断路径不受影响（防过度清理回归）。"""
        from api.task_core import update_task_status
        tid = _insert_task(tmp_db, status="running")
        update_task_status(tid, "interrupted", "s", interruption_reason="user")
        st = _task_state(tmp_db, tid)
        assert st["status"] == "interrupted"
        assert st["interruption_reason"] == "user"

    def test_complete_endpoint_clears_reason(self, tmp_db):
        """REST 标记为已完成：直连 SQL 同样清历史中断原因。"""
        from api.routes import routes_tasks
        tid = _insert_task(tmp_db, status="interrupted",
                           interruption_reason="user")
        resp = asyncio.run(routes_tasks.complete_task(tid))
        assert resp["status"] == "success"
        st = _task_state(tmp_db, tid)
        assert st["status"] == "completed"
        assert st["interruption_reason"] is None


# ---------- POST /api/tasks/{id}/resume ----------

class TestResumeEndpoint:
    def test_resume_interrupted_with_extra_instruction(self, tmp_db, fake_bg_threads):
        """interrupted + 附加指令：恢复成功；指令作为最后一条 user 消息注入
        恢复上下文并落库；走 _run_background_task(tid, user_query, ctx, True)。"""
        bg, spawned = fake_bg_threads
        from api.routes import routes_tasks
        from api.task_core import save_task_context, get_task_context
        tid = _insert_task(tmp_db, status="interrupted",
                           interruption_reason="server_restart")
        save_task_context(tid, [
            {"role": "user", "content": "原始查询"},
            {"role": "assistant", "content": "正在执行"},
        ])
        req = routes_tasks.ResumeTaskRequest(extra_instruction="优先处理剩余导出")
        resp = asyncio.run(routes_tasks.resume_task(tid, req))
        assert resp["status"] == "success" and resp["resumed"] is True
        st = _task_state(tmp_db, tid)
        assert st["status"] == "running"           # CAS 认领即 running
        assert st["resume_count"] == 1
        assert st["interruption_reason"] is None   # 历史原因已清
        # 起恢复线程，附加指令注入恢复上下文末尾
        assert len(spawned) == 1
        assert spawned[0]["target"] is bg._run_background_task
        args = spawned[0]["args"]
        assert args[0] == tid and args[1] == "原始查询" and args[3] is True
        assert args[2][-1]["role"] == "user"
        assert "优先处理剩余导出" in args[2][-1]["content"]
        # 指令同时落库到 context_snapshot
        ctx = get_task_context(tid)
        assert any("优先处理剩余导出" in m.get("content", "") for m in ctx)

    def test_resume_without_body(self, tmp_db, fake_bg_threads):
        """不带 body（req=None）：照常恢复，不注入附加指令。"""
        bg, spawned = fake_bg_threads
        from api.routes import routes_tasks
        tid = _insert_task(tmp_db, status="background_failed",
                           interruption_reason="max_resume_exceeded")
        resp = asyncio.run(routes_tasks.resume_task(tid, None))
        assert resp["status"] == "success" and resp["resumed"] is True
        assert len(spawned) == 1
        st = _task_state(tmp_db, tid)
        assert st["status"] == "running"
        assert st["interruption_reason"] is None

    def test_resume_running_task_rejected_409(self, tmp_db, fake_bg_threads):
        """running 状态不可恢复：409，不起恢复线程。"""
        from fastapi import HTTPException
        from api.routes import routes_tasks
        _, spawned = fake_bg_threads
        tid = _insert_task(tmp_db, status="running", interruption_reason=None)
        with pytest.raises(HTTPException) as ei:
            asyncio.run(routes_tasks.resume_task(tid, None))
        assert ei.value.status_code == 409
        assert spawned == []

    def test_resume_not_found_404(self, tmp_db, fake_bg_threads):
        """任务不存在：404。"""
        from fastapi import HTTPException
        from api.routes import routes_tasks
        with pytest.raises(HTTPException) as ei:
            asyncio.run(routes_tasks.resume_task(99999, None))
        assert ei.value.status_code == 404

    def test_resume_queued_to_live_background_agent(self, tmp_db, fake_bg_threads):
        """活后台 agent 持有任务：指令排队投递，不 CAS 认领、不起新线程。"""
        import api.state as state
        _, spawned = fake_bg_threads
        from api.routes import routes_tasks
        tid = _insert_task(tmp_db, status="interrupted",
                           interruption_reason="server_restart")
        queued = []
        fake_agent = types.SimpleNamespace(
            is_interrupted=False, queue_message=queued.append)
        state._background_agents[tid] = fake_agent
        try:
            req = routes_tasks.ResumeTaskRequest(extra_instruction="换个思路")
            resp = asyncio.run(routes_tasks.resume_task(tid, req))
            assert resp["status"] == "success" and resp["resumed"] is False
            assert queued == ["[用户继续指令] 换个思路"]
            assert spawned == []
            # 未 CAS 认领：状态与原因保持原样
            st = _task_state(tmp_db, tid)
            assert st["status"] == "interrupted"
            assert st["interruption_reason"] == "server_restart"
        finally:
            state._background_agents.pop(tid, None)

    def test_resume_claim_conflict_returns_409(self, tmp_db, fake_bg_threads, monkeypatch):
        """CAS 认领冲突（他路径抢先恢复）：409，不起恢复线程。"""
        from fastapi import HTTPException
        from api.routes import routes_tasks
        bg, spawned = fake_bg_threads
        monkeypatch.setattr(bg, "claim_task_for_resume", lambda tid, allowed: False)
        tid = _insert_task(tmp_db, status="interrupted",
                           interruption_reason="server_restart")
        with pytest.raises(HTTPException) as ei:
            asyncio.run(routes_tasks.resume_task(tid, None))
        assert ei.value.status_code == 409
        assert spawned == []
