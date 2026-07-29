"""中断双向同步 + 重启统一恢复 回归测试。

Bug 1（中断双向同步缺失）：
- 任务中断（REST /api/tasks/{id}/interrupt；WS interrupt 走同一 helper）时，
  注册在 _background_process_info 的后台进程必须被 kill_tree 终止并清出
  跟踪表（先杀后 pop，顺序不能反），任务上下文注入系统通知；
- /api/tasks/{id}/kill 端点真正杀进程（而非只 pop 跟踪表），并按任务状态
  同步 interrupted（backgrounded/running → interrupted/user；终结态不动）。

Bug 2（重启后无锚点 backgrounded 任务不恢复）：
- api.background.reconcile_backgrounded_after_restart 统一接管所有
  backgrounded 任务：置 interrupted + 注入「服务器重启，请继续执行」通知，
  随后走 claim_task_for_resume CAS + _run_background_task 恢复链路
  （resume_count 自然计数）；
- 保留下载锚点语义（完成/失败文案、background_resumed 原子标志）；
- 沿用 _is_backoff_elapsed 退避与 max_resume_count 上限防恢复风暴。
"""
import asyncio
import json
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


@pytest.fixture(autouse=True)
def _isolated_bg_store(tmp_path, monkeypatch):
    """注册表写-through 持久化重定向到临时目录，不碰真实 data/。"""
    import tools.shell as sh
    monkeypatch.setattr(sh, "_BG_STORE_PATH", str(tmp_path / "background_processes.json"))


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


def _insert_task(db_mod, status="backgrounded", resume_count=0, max_resume_count=10,
                 updated_at=None, session_id=1):
    conn = db_mod.db_connect()
    cur = conn.execute(
        "INSERT INTO tasks (title, user_query, status, resume_count, "
        "max_resume_count, session_id) VALUES (?, ?, ?, ?, ?, ?)",
        ("测试任务", "原始查询", status, resume_count, max_resume_count, session_id))
    tid = cur.lastrowid
    if updated_at is not None:
        conn.execute("UPDATE tasks SET updated_at=? WHERE id=?", (updated_at, tid))
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


def _insert_download(db_mod, tid, status="completed", background_resumed=0,
                     error_message=None):
    conn = db_mod.db_connect()
    cur = conn.execute(
        "INSERT INTO downloads (type, label, status, task_id, background_resumed, "
        "error_message) VALUES ('file', '测试文件', ?, ?, ?, ?)",
        (status, tid, background_resumed, error_message))
    dl_id = cur.lastrowid
    conn.commit()
    conn.close()
    return dl_id


def _seed_tracked_process(task_id, pid, output_file=""):
    """一任务多进程结构：{task_id: {pid: info}}。"""
    import tools.shell as sh
    with sh._background_process_lock:
        sh._background_process_info[str(task_id)] = {
            str(pid): {
                "pid": pid, "output_file": output_file,
                "command": "sleep 999", "started_at": 0.0,
                "timeout": 0, "alive": True,
            }
        }


def _tracked_entry(task_id):
    import tools.shell as sh
    with sh._background_process_lock:
        return sh._background_process_info.get(str(task_id))


# ---------- tools.shell.kill_background_process_for_task（单元级） ----------

class TestShellKillHelper:
    def test_kills_pid_before_popping_tracking_entry(self, monkeypatch):
        """先取 pid 杀进程树、再 pop 跟踪表——顺序不能反。"""
        import tools.shell as sh
        _seed_tracked_process(9001, 4321)
        seen = {}

        def _fake_kill(pid):
            seen["pid"] = pid
            # 杀进程时跟踪表条目必须还在（先杀后 pop）
            seen["entry_present_at_kill"] = _tracked_entry(9001) is not None

        monkeypatch.setattr(sh, "kill_tree", _fake_kill)
        try:
            pids = sh.kill_background_process_for_task(9001)
            assert pids == [4321]
            assert seen == {"pid": 4321, "entry_present_at_kill": True}
            assert _tracked_entry(9001) is None
        finally:
            sh.cleanup_background_process(9001)

    def test_kill_failure_still_pops_then_raises(self, monkeypatch):
        """kill_tree 抛错：跟踪表仍被清理（finally pop），异常传播给调用方
        （由 task_core helper 兜底，不阻断中断流程）。"""
        import tools.shell as sh
        _seed_tracked_process(9002, 4322)

        def _boom(pid):
            raise RuntimeError("taskkill missing")

        monkeypatch.setattr(sh, "kill_tree", _boom)
        try:
            with pytest.raises(RuntimeError):
                sh.kill_background_process_for_task(9002)
            assert _tracked_entry(9002) is None
        finally:
            sh.cleanup_background_process(9002)

    def test_no_tracked_process_returns_empty(self):
        import tools.shell as sh
        assert sh.kill_background_process_for_task(9003) == []

    def test_entry_without_pid_popped_returns_empty(self):
        import tools.shell as sh
        _seed_tracked_process(9004, None)
        try:
            assert sh.kill_background_process_for_task(9004) == []
            assert _tracked_entry(9004) is None
        finally:
            sh.cleanup_background_process(9004)


# ---------- REST interrupt：任务中断联动杀后台进程 ----------

class TestRestInterruptKillsProcess:
    def test_interrupt_kills_tracked_process_and_notifies(self, tmp_db, monkeypatch):
        import tools.shell as sh
        from api.routes import routes_tasks
        from api.task_core import save_task_context, get_task_context

        tid = _insert_task(tmp_db, status="running")
        save_task_context(tid, [
            {"role": "user", "content": "原始查询"},
            {"role": "assistant", "content": "正在执行"},
        ])
        _seed_tracked_process(tid, 5555)
        killed = []
        monkeypatch.setattr(sh, "kill_tree", lambda pid: killed.append(pid))
        try:
            result = asyncio.run(routes_tasks.interrupt_task(tid))
            assert result["status"] == "success"
            # kill_tree 以正确 pid 调用，跟踪表已清理
            assert killed == [5555]
            assert _tracked_entry(tid) is None
            # 任务状态置 interrupted/user
            st = _task_state(tmp_db, tid)
            assert st["status"] == "interrupted"
            assert st["interruption_reason"] == "user"
            # 上下文注入系统通知
            ctx = get_task_context(tid)
            notices = [m for m in ctx if "已随任务中断一并终止" in m.get("content", "")]
            assert len(notices) == 1
            assert "5555" in notices[0]["content"]
        finally:
            sh.cleanup_background_process(tid)

    def test_interrupt_without_tracked_process_still_interrupts(self, tmp_db, monkeypatch):
        import tools.shell as sh
        from api.routes import routes_tasks

        tid = _insert_task(tmp_db, status="running")
        killed = []
        monkeypatch.setattr(sh, "kill_tree", lambda pid: killed.append(pid))
        result = asyncio.run(routes_tasks.interrupt_task(tid))
        assert result["status"] == "success"
        assert killed == []
        assert _task_state(tmp_db, tid)["status"] == "interrupted"

    def test_ws_interrupt_uses_shared_kill_helper(self):
        """WS interrupt 分支接入同一 kill helper（源码级回归）。"""
        src = open(os.path.join(PROJECT_ROOT, "api", "ws.py"),
                   encoding="utf-8").read()
        marker = 'if user_msg.get("type") == "interrupt":'
        idx = src.index(marker)
        # interrupt 分支内（下一个 elif 之前）必须调用 kill helper
        branch = src[idx:src.index('elif user_msg.get("type")', idx)]
        assert "kill_tracked_background_process" in branch

    def test_kill_failure_returns_empty_and_honest_notice(self, tmp_db, monkeypatch):
        """kill_tree 抛异常：helper 返回空列表、不阻断中断流程；注入的通知
        如实说明"终止失败/可能仍在运行"。"""
        import tools.shell as sh
        from api.task_core import (kill_tracked_background_process,
                                   save_task_context, get_task_context)

        tid = _insert_task(tmp_db, status="running")
        save_task_context(tid, [
            {"role": "user", "content": "原始查询"},
            {"role": "assistant", "content": "正在执行"},
        ])
        _seed_tracked_process(tid, 8888)

        def _boom(pid):
            raise RuntimeError("taskkill missing")

        monkeypatch.setattr(sh, "kill_tree", _boom)
        try:
            pids = kill_tracked_background_process(tid)
            assert pids == []
            assert _tracked_entry(tid) is None
            ctx = get_task_context(tid)
            notices = [m for m in ctx if "终止失败" in m.get("content", "")]
            assert len(notices) == 1
            assert "可能仍在运行" in notices[0]["content"]
        finally:
            sh.cleanup_background_process(tid)


# ---------- /api/tasks/{id}/kill：杀进程 + 状态同步 ----------

class TestKillEndpoint:
    def test_kills_process_and_marks_backgrounded_task_interrupted(
            self, tmp_db, monkeypatch, tmp_path):
        import tools.shell as sh
        from api.routes import routes_tasks
        from api.task_core import get_task_context

        tid = _insert_task(tmp_db, status="backgrounded", resume_count=3)
        out_file = tmp_path / "shell.log"
        out_file.write_text("some output\n", encoding="utf-8")
        _seed_tracked_process(tid, 6666, output_file=str(out_file))
        killed = []
        monkeypatch.setattr(sh, "kill_tree", lambda pid: killed.append(pid))
        try:
            resp = asyncio.run(routes_tasks.kill_task_process(tid))
            assert resp["status"] == "success"
            assert resp["killed_pid"] == 6666
            assert resp["task_interrupted"] is True
            # 进程树已杀、跟踪表已清
            assert killed == [6666]
            assert _tracked_entry(tid) is None
            # 任务状态同步 interrupted/user，resume_count 复位
            st = _task_state(tmp_db, tid)
            assert st["status"] == "interrupted"
            assert st["interruption_reason"] == "user"
            assert st["resume_count"] == 0
            # 输出注入任务上下文
            ctx = get_task_context(tid)
            assert any("Process killed by user" in m.get("content", "") for m in ctx)
        finally:
            sh.cleanup_background_process(tid)

    def test_terminal_task_status_preserved_but_process_killed(
            self, tmp_db, monkeypatch, tmp_path):
        """任务本来不是 backgrounded/running：进程照杀，任务状态保持原样；
        守卫 UPDATE 不命中——resume_count / result_summary 连带不动。"""
        import tools.shell as sh
        from api.routes import routes_tasks

        tid = _insert_task(tmp_db, status="completed", resume_count=5)
        conn = tmp_db.db_connect()
        conn.execute("UPDATE tasks SET result_summary='已完成的结果' WHERE id=?", (tid,))
        conn.commit()
        conn.close()
        out_file = tmp_path / "shell.log"
        out_file.write_text("done\n", encoding="utf-8")
        _seed_tracked_process(tid, 7777, output_file=str(out_file))
        killed = []
        monkeypatch.setattr(sh, "kill_tree", lambda pid: killed.append(pid))
        try:
            resp = asyncio.run(routes_tasks.kill_task_process(tid))
            assert resp["killed_pid"] == 7777
            assert resp["task_interrupted"] is False
            assert killed == [7777]
            assert _tracked_entry(tid) is None
            st = _task_state(tmp_db, tid)
            assert st["status"] == "completed"
            assert st["resume_count"] == 5  # 守卫 UPDATE 不命中：不复位
            conn = tmp_db.db_connect()
            rs = conn.execute(
                "SELECT result_summary FROM tasks WHERE id=?", (tid,)).fetchone()[0]
            conn.close()
            assert rs == "已完成的结果"  # 不被覆写
        finally:
            sh.cleanup_background_process(tid)

    def test_task_completing_mid_kill_not_overwritten(
            self, tmp_db, monkeypatch, tmp_path):
        """I-1：杀进程窗口内任务刚被另一路径置 completed——守卫 UPDATE 不
        命中，completed 不被覆写回 interrupted（summary/resume_count 不动）。"""
        import tools.shell as sh
        from api.routes import routes_tasks

        tid = _insert_task(tmp_db, status="running", resume_count=2)
        out_file = tmp_path / "shell.log"
        out_file.write_text("tail\n", encoding="utf-8")
        _seed_tracked_process(tid, 9999, output_file=str(out_file))
        monkeypatch.setattr(sh, "kill_tree", lambda pid: None)
        # 窗口注入：输出注入读取上下文时，任务被另一路径标记为 completed
        orig_get = routes_tasks.get_task_context

        def _racing_get(t):
            conn = tmp_db.db_connect()
            conn.execute(
                "UPDATE tasks SET status='completed', result_summary='搞定' WHERE id=?",
                (t,))
            conn.commit()
            conn.close()
            return orig_get(t)

        monkeypatch.setattr(routes_tasks, "get_task_context", _racing_get)
        try:
            resp = asyncio.run(routes_tasks.kill_task_process(tid))
            assert resp["killed_pid"] == 9999
            assert resp["task_interrupted"] is False
            st = _task_state(tmp_db, tid)
            assert st["status"] == "completed"
            assert st["resume_count"] == 2
        finally:
            sh.cleanup_background_process(tid)

    def test_running_task_kill_sets_agent_interrupt_flag(self, tmp_db, monkeypatch):
        """I-2：running 任务被 kill——翻转状态的同时置活 agent 的
        is_interrupted（机制同 WS/REST interrupt），旧 agent 自行停止，
        之后的 resume CAS 认领不会造成双 agent 并行。"""
        import api.state as state
        import tools.shell as sh
        from api.routes import routes_tasks
        from api.task_core import claim_task_for_resume

        tid = _insert_task(tmp_db, status="running")
        _seed_tracked_process(tid, 1234)
        monkeypatch.setattr(sh, "kill_tree", lambda pid: None)
        fake_bg_agent = types.SimpleNamespace(is_interrupted=False)
        fake_fg_agent = types.SimpleNamespace(is_interrupted=False)
        state._background_agents[tid] = fake_bg_agent
        state._active_agents.setdefault(1, {})[tid] = fake_fg_agent
        try:
            resp = asyncio.run(routes_tasks.kill_task_process(tid))
            assert resp["task_interrupted"] is True
            # 两处注册表（前台 _active_agents / 后台 _background_agents）的
            # 活 agent 都被置中断标志
            assert fake_bg_agent.is_interrupted is True
            assert fake_fg_agent.is_interrupted is True
            assert fake_bg_agent._completed_by_user is True
            assert fake_fg_agent._completed_by_user is True
            # 任务已翻转 interrupted：resume CAS 可认领，但被置标志的旧
            # agent 不会继续执行（无双跑）
            assert claim_task_for_resume(tid, ('interrupted',)) is True
        finally:
            state._background_agents.pop(tid, None)
            state._active_agents.get(1, {}).pop(tid, None)
            sh.cleanup_background_process(tid)

    def test_no_tracked_process_running_task_still_interrupted(self, tmp_db, monkeypatch):
        """无跟踪进程但任务存活：不同步杀进程，仅把任务置 interrupted。"""
        import tools.shell as sh
        from api.routes import routes_tasks

        tid = _insert_task(tmp_db, status="running")
        killed = []
        monkeypatch.setattr(sh, "kill_tree", lambda pid: killed.append(pid))
        resp = asyncio.run(routes_tasks.kill_task_process(tid))
        assert resp["killed_pid"] is None
        assert resp["task_interrupted"] is True
        assert killed == []
        st = _task_state(tmp_db, tid)
        assert st["status"] == "interrupted"
        assert st["interruption_reason"] == "user"


# ---------- reconcile_backgrounded_after_restart：重启统一恢复 ----------

class TestReconcileAfterRestart:
    def test_no_anchor_task_interrupted_and_resumed(self, tmp_db, fake_bg_threads):
        """无锚点（无 wake_at/无进程/无下载）的 backgrounded 任务：重启后
        置 interrupted 并触发恢复认领（CAS 后 status=running，resume_count+1）。"""
        bg, spawned = fake_bg_threads
        from api.task_core import save_task_context, get_task_context

        tid = _insert_task(tmp_db, status="backgrounded", resume_count=0)
        save_task_context(tid, [
            {"role": "user", "content": "原始查询"},
            {"role": "assistant", "content": "正在执行"},
        ])
        bg.reconcile_backgrounded_after_restart()

        st = _task_state(tmp_db, tid)
        assert st["status"] == "running"          # 认领即 running
        assert st["resume_count"] == 1            # resume_count 自然计数
        # 认领即清历史中断原因（server_restart 属于上一次中断，任务已恢复执行）
        assert st["interruption_reason"] is None
        # 注入了「服务器重启，请继续执行」通知
        ctx = get_task_context(tid)
        assert any("服务器重启" in m.get("content", "") for m in ctx)
        # 走 _run_background_task 恢复链路
        assert len(spawned) == 1
        assert spawned[0]["target"] is bg._run_background_task
        args = spawned[0]["args"]
        assert args[0] == tid and args[1] == "原始查询" and args[3] is True
        assert any("服务器重启" in m.get("content", "") for m in args[2])

    def test_backoff_not_elapsed_defers_resume(self, tmp_db, fake_bg_threads):
        """resume_count=1（退避 30s）且刚更新：留在 interrupted，本次不恢复。"""
        bg, spawned = fake_bg_threads
        tid = _insert_task(tmp_db, status="backgrounded", resume_count=1)
        bg.reconcile_backgrounded_after_restart()
        st = _task_state(tmp_db, tid)
        assert st["status"] == "interrupted"
        assert st["resume_count"] == 1            # 未认领，不计数
        assert spawned == []

    def test_max_resume_exceeded_marks_background_failed(self, tmp_db, fake_bg_threads):
        """resume_count 达上限：置 background_failed，不再恢复。"""
        bg, spawned = fake_bg_threads
        tid = _insert_task(tmp_db, status="backgrounded",
                           resume_count=10, max_resume_count=10)
        bg.reconcile_backgrounded_after_restart()
        st = _task_state(tmp_db, tid)
        assert st["status"] == "background_failed"
        assert st["interruption_reason"] == "max_resume_exceeded"
        assert spawned == []

    def test_claim_failure_leaves_interrupted(self, tmp_db, fake_bg_threads, monkeypatch):
        """CAS 认领失败（另一路径已接管）：保持 interrupted，不起恢复线程。"""
        bg, spawned = fake_bg_threads
        monkeypatch.setattr(bg, "claim_task_for_resume",
                            lambda tid, allowed: False)
        tid = _insert_task(tmp_db, status="backgrounded", resume_count=0)
        bg.reconcile_backgrounded_after_restart()
        st = _task_state(tmp_db, tid)
        assert st["status"] == "interrupted"
        assert st["resume_count"] == 0
        assert spawned == []

    def test_download_anchor_semantics_preserved_and_resumed(
            self, tmp_db, fake_bg_threads):
        """下载完成锚点：保留原语义（完成文案 + background_resumed 标志 +
        background_complete reason），并且也被恢复（不再只有它们被标记）。"""
        bg, spawned = fake_bg_threads
        from api.task_core import get_task_context

        tid = _insert_task(tmp_db, status="backgrounded", resume_count=0)
        dl_id = _insert_download(tmp_db, tid, status="completed")
        bg.reconcile_backgrounded_after_restart()

        st = _task_state(tmp_db, tid)
        assert st["status"] == "running"
        assert st["resume_count"] == 1
        # background_complete 原因在置 interrupted 时写入、CAS 认领恢复时清 NULL
        assert st["interruption_reason"] is None
        conn = tmp_db.db_connect()
        flag = conn.execute(
            "SELECT background_resumed FROM downloads WHERE id=?", (dl_id,)).fetchone()[0]
        conn.close()
        assert flag == 1
        ctx = get_task_context(tid)
        assert any("文件已就绪" in m.get("content", "") for m in ctx)
        assert len(spawned) == 1

    def test_failed_download_only_notifies_session(self, tmp_db, fake_bg_threads):
        """下载失败锚点（任务已终结）：只通知会话 + 消费标志，不触发恢复。"""
        bg, spawned = fake_bg_threads
        tid = _insert_task(tmp_db, status="completed")
        dl_id = _insert_download(tmp_db, tid, status="failed",
                                 error_message="网络错误")
        bg.reconcile_backgrounded_after_restart()

        conn = tmp_db.db_connect()
        flag = conn.execute(
            "SELECT background_resumed FROM downloads WHERE id=?", (dl_id,)).fetchone()[0]
        msgs = conn.execute(
            "SELECT content FROM messages WHERE role='system' AND session_id=1").fetchall()
        conn.close()
        assert flag == 1
        assert any("下载失败" in m[0] and "网络错误" in m[0] for m in msgs)
        assert _task_state(tmp_db, tid)["status"] == "completed"
        assert spawned == []

    def test_non_backgrounded_tasks_untouched(self, tmp_db, fake_bg_threads):
        """running / interrupted / completed 任务不属于本次 reconcile 范围。"""
        bg, spawned = fake_bg_threads
        t_running = _insert_task(tmp_db, status="running")
        t_interrupted = _insert_task(tmp_db, status="interrupted")
        t_completed = _insert_task(tmp_db, status="completed")
        bg.reconcile_backgrounded_after_restart()
        assert _task_state(tmp_db, t_running)["status"] == "running"
        assert _task_state(tmp_db, t_interrupted)["status"] == "interrupted"
        assert _task_state(tmp_db, t_completed)["status"] == "completed"
        assert spawned == []

    def test_flip_guard_does_not_clobber_running_task(self, tmp_db, fake_bg_threads):
        """守卫翻转：快照与翻转之间被他路径 CAS 认领（running）的任务，
        翻转失败且状态不被覆写（防与 BgMonitor wake 点火双跑）。"""
        bg, _ = fake_bg_threads
        tid = _insert_task(tmp_db, status="running")
        conn = tmp_db.db_connect()
        flipped = bg._flip_bg_to_interrupted(conn, tid, "s", "server_restart")
        conn.close()
        assert flipped is False
        assert _task_state(tmp_db, tid)["status"] == "running"

    def test_flip_guard_flips_backgrounded_task(self, tmp_db, fake_bg_threads):
        bg, _ = fake_bg_threads
        tid = _insert_task(tmp_db, status="backgrounded")
        conn = tmp_db.db_connect()
        flipped = bg._flip_bg_to_interrupted(conn, tid, "s", "server_restart")
        conn.close()
        assert flipped is True
        st = _task_state(tmp_db, tid)
        assert st["status"] == "interrupted"
        assert st["interruption_reason"] == "server_restart"
