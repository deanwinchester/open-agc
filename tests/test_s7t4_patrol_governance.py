"""阶段 7 Task 4（B4）：巡检可用性与误判治理测试。

覆盖：
- Item 1：陈腐 running 复位移出 heartbeat 门控——heartbeat_enabled=False 时
  stale_running_rescue_once 仍复位孤尸；Guardian 循环不再携带该块（源码级）
- Item 2：复位前查活句柄——_background_agents / _active_agents 有该 task
  活句柄时跳过（线程活着就不是孤尸）
- Item 3：停滞判定保守化——静默进程未满 _STALL_FREEZE_ROUNDS（90 轮 ≈ 15min）
  不被判完成、不删输出文件；满阈值解除追踪但如实告知"仍在运行、无输出 N 分钟"
- Item 4：解除追踪写兜底 wake_at（+30min）；BgMonitor 无 pinfo 分支对超 6h
  无寄托（无进程/无 wake/无下载）任务置 background_failed
- Item 5：邮件 mark_seen 移到落库成功后（顺序源码级断言 + store 行为级）；
  回信文案按真实终态区分（completed/failed/interrupted/backgrounded）
"""
import inspect
import os
import sqlite3
import sys
import threading
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """把 api.db.DB_PATH 指到临时库。"""
    import api.db as db_mod
    db_file = str(tmp_path / "test.db")
    monkeypatch.setattr(db_mod, "DB_PATH", db_file)
    db_mod.init_db()
    return db_mod


def _insert_task(db_mod, status="running", updated_at=None, wake_at=None,
                 task_type="oneshot", session_id=1):
    conn = db_mod.db_connect()
    cur = conn.execute(
        "INSERT INTO tasks (title, user_query, status, task_type, session_id) "
        "VALUES (?, ?, ?, ?, ?)",
        ("测试任务", "原始查询", status, task_type, session_id))
    tid = cur.lastrowid
    if updated_at is not None:
        conn.execute("UPDATE tasks SET updated_at=? WHERE id=?", (updated_at, tid))
    if wake_at is not None:
        conn.execute("UPDATE tasks SET wake_at=? WHERE id=?", (wake_at, tid))
    conn.commit()
    conn.close()
    return tid


def _task_row(db_mod, tid):
    conn = db_mod.db_connect()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status, resume_count, interruption_reason, wake_at FROM tasks WHERE id=?",
        (tid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _utc_ts(**ago):
    return (datetime.now(timezone.utc) - timedelta(**ago)).strftime('%Y-%m-%d %H:%M:%S')


# ---------- Item 1：stale rescue 移出 heartbeat 门控 ----------

class TestStaleRescueUngated:
    def test_stale_reset_works_with_heartbeat_disabled(self, tmp_db, monkeypatch):
        """heartbeat_enabled=False（默认）时陈腐 running 复位仍然工作。"""
        import api.background as bg
        monkeypatch.setattr(bg, "load_config", lambda: {"heartbeat_enabled": False})
        tid = _insert_task(tmp_db, status="running", updated_at=_utc_ts(minutes=40))
        actions = bg.stale_running_rescue_once()
        row = _task_row(tmp_db, tid)
        assert row["status"] == "interrupted"
        assert row["interruption_reason"] == "stale_running"
        assert any(str(tid) in a for a in actions)

    def test_fresh_running_task_not_reset(self, tmp_db):
        import api.background as bg
        tid = _insert_task(tmp_db, status="running")  # updated_at = 当前时间
        assert bg.stale_running_rescue_once() == []
        assert _task_row(tmp_db, tid)["status"] == "running"

    def test_heartbeat_task_type_excluded(self, tmp_db):
        """heartbeat/goal_resume 类型任务不参与孤尸复位（与原 Guardian 行为一致）。"""
        import api.background as bg
        tid = _insert_task(tmp_db, status="running",
                           updated_at=_utc_ts(minutes=40), task_type="heartbeat")
        assert bg.stale_running_rescue_once() == []
        assert _task_row(tmp_db, tid)["status"] == "running"

    def test_guardian_loop_no_longer_carries_stale_block(self):
        """源码级：Guardian 循环不再携带陈腐复位块；独立小循环存在且不受门控。"""
        import api.background as bg
        guardian_src = inspect.getsource(bg.start_guardian_loop)
        assert "_stale_running" not in guardian_src
        assert "Stale running check error" not in guardian_src
        # 独立循环存在，且其函数体不读配置门控（无 load_config / cfg.get）
        rescue_src = inspect.getsource(bg.start_stale_rescue_loop)
        assert "load_config" not in rescue_src
        assert "cfg.get" not in rescue_src
        assert "stale_running_rescue_once" in rescue_src

    def test_server_starts_stale_rescue_loop(self):
        src = (Path(__file__).resolve().parent.parent / "api" / "server.py").read_text(encoding="utf-8")
        assert "start_stale_rescue_loop()" in src


# ---------- Item 2：陈腐复位先查活 ----------

class TestLiveHandleSkipsRescue:
    def test_background_agent_handle_skips_reset(self, tmp_db, monkeypatch):
        import api.background as bg
        from api.state import _background_agents
        tid = _insert_task(tmp_db, status="running", updated_at=_utc_ts(minutes=40))
        monkeypatch.setitem(_background_agents, tid, object())
        actions = bg.stale_running_rescue_once()
        assert _task_row(tmp_db, tid)["status"] == "running"
        assert any("skipped" in a for a in actions)

    def test_active_agent_handle_skips_reset(self, tmp_db, monkeypatch):
        import api.background as bg
        from api.state import _active_agents
        tid = _insert_task(tmp_db, status="running", updated_at=_utc_ts(minutes=40))
        monkeypatch.setitem(_active_agents, 7, {tid: object()})
        bg.stale_running_rescue_once()
        assert _task_row(tmp_db, tid)["status"] == "running"

    def test_unrelated_handle_does_not_block_reset(self, tmp_db, monkeypatch):
        """其他 task 的句柄不影响本 task 的孤尸判定。"""
        import api.background as bg
        from api.state import _background_agents
        tid = _insert_task(tmp_db, status="running", updated_at=_utc_ts(minutes=40))
        monkeypatch.setitem(_background_agents, tid + 9999, object())
        bg.stale_running_rescue_once()
        assert _task_row(tmp_db, tid)["status"] == "interrupted"


# ---------- BgMonitor 测试 harness ----------

def _install_monitor_harness(monkeypatch, bg, run_rounds=8):
    """monitor_loop 跑真线程、恢复 worker 走假线程；sleep 改为计数闸门，
    跑满 run_rounds 轮后放行主线程断言，随后把循环线程永久泊住（daemon）。"""
    spawned = []
    rounds_done = threading.Event()
    release_loop = threading.Event()
    rounds = {"n": 0}

    def _thread_factory(target=None, args=(), daemon=None, **kw):
        if getattr(target, "__name__", "") == "monitor_loop":
            return threading.Thread(target=target, daemon=True)
        spawned.append({"target": target, "args": args})
        return types.SimpleNamespace(start=lambda: None)

    monkeypatch.setattr(bg, "threading", types.SimpleNamespace(Thread=_thread_factory))

    def _fake_sleep(seconds):
        rounds["n"] += 1
        if rounds["n"] >= run_rounds:
            rounds_done.set()
            release_loop.wait(timeout=30)
            threading.Event().wait()  # 泊住（daemon，随进程退出）

    monkeypatch.setattr(bg._time, "sleep", _fake_sleep)
    return spawned, rounds_done, release_loop


def _patch_shell_idle(monkeypatch, tid, out_file):
    """进程活着、输出文件恒定不变的 shell 环境。返回进程表（供 mock cleanup 同步删除）。"""
    procs = {str(tid): {"pid": 424242, "output_file": str(out_file),
                        "command": "sleep 9999", "started_at": 1.0}}
    monkeypatch.setattr("tools.shell.get_background_processes", lambda: dict(procs))
    monkeypatch.setattr("tools.shell.get_orphan_processes", lambda: {})
    monkeypatch.setattr("tools.shell.adopt_orphan_processes", lambda *a, **k: 0)
    return procs


# ---------- Item 3 + 4a：停滞判定保守化 + 兜底 wake_at ----------

class TestStallConservative:
    def test_stall_threshold_is_15min(self):
        import api.background as bg
        # 90 轮 × 10s/轮 ≈ 15 分钟（原 30s 快路径已删除）
        assert bg._STALL_FREEZE_ROUNDS == 90

    def test_silent_process_under_threshold_not_judged_done(self, tmp_db, monkeypatch, tmp_path):
        """静默进程未满阈值：不恢复、不删文件、不谎称完成，任务保持 backgrounded。"""
        import api.background as bg
        from api.task_core import save_task_context
        monkeypatch.setattr(bg, "_STALL_FREEZE_ROUNDS", 1000)  # 阈值拉高，8 轮内不触发
        tid = _insert_task(tmp_db, status="backgrounded")
        save_task_context(tid, [{"role": "user", "content": "原始任务"}])
        out_file = tmp_path / "out.log"
        out_file.write_text("partial output", encoding="utf-8")
        _patch_shell_idle(monkeypatch, tid, out_file)
        monkeypatch.setattr(bg, "pid_alive", lambda pid: True)
        cleaned = []
        monkeypatch.setattr("tools.shell.cleanup_background_process",
                            lambda key: cleaned.append(key))
        spawned, rounds_done, release_loop = _install_monitor_harness(
            monkeypatch, bg, run_rounds=8)

        bg.start_background_monitor()
        try:
            assert rounds_done.wait(timeout=20), "monitor loop did not run 8 rounds"
        finally:
            release_loop.set()
        assert spawned == []                                   # 未起恢复线程
        assert cleaned == []                                   # 未解除追踪
        assert out_file.exists()                               # 输出文件未删
        row = _task_row(tmp_db, tid)
        assert row["status"] == "backgrounded"
        assert row["wake_at"] is None

    def test_frozen_output_untracks_honestly_and_sets_fallback_wake(self, tmp_db, monkeypatch, tmp_path):
        """满阈值：解除追踪但如实告知仍在运行、不删输出文件、写兜底 wake_at(+30min)。"""
        import api.background as bg
        from api.task_core import save_task_context, get_task_context
        monkeypatch.setattr(bg, "_STALL_FREEZE_ROUNDS", 3)     # 阈值拉低，快速触发
        tid = _insert_task(tmp_db, status="backgrounded")
        save_task_context(tid, [{"role": "user", "content": "原始任务"}])
        out_file = tmp_path / "out.log"
        out_file.write_text("partial output", encoding="utf-8")
        procs = _patch_shell_idle(monkeypatch, tid, out_file)
        monkeypatch.setattr(bg, "pid_alive", lambda pid: True)
        cleaned = []

        def _fake_cleanup(key):
            cleaned.append(key)
            procs.pop(key, None)  # 与真实 cleanup 一致：解除后 pinfo 消失

        monkeypatch.setattr("tools.shell.cleanup_background_process", _fake_cleanup)
        spawned, rounds_done, release_loop = _install_monitor_harness(
            monkeypatch, bg, run_rounds=8)

        before = datetime.now(timezone.utc)
        bg.start_background_monitor()
        try:
            assert rounds_done.wait(timeout=20), "monitor loop did not run 8 rounds"
        finally:
            release_loop.set()
        assert cleaned == [str(tid)]                           # 已解除追踪
        assert spawned == []                                   # 进程活着 → 不恢复
        assert out_file.exists()                               # 不删输出文件
        row = _task_row(tmp_db, tid)
        assert row["status"] == "backgrounded"                 # 任务不判完成
        # 兜底 wake_at ≈ +30min
        assert row["wake_at"] is not None
        wake_dt = datetime.strptime(row["wake_at"], '%Y-%m-%d %H:%M:%S').replace(
            tzinfo=timezone.utc)
        assert before + timedelta(minutes=29) <= wake_dt <= datetime.now(
            timezone.utc) + timedelta(minutes=31)
        # 如实告知：仍在运行、无输出 N 分钟（不再谎称"执行完毕"）
        ctx = get_task_context(tid)
        last = ctx[-1]["content"]
        assert "仍在运行" in last and "无输出" in last
        assert "执行完毕" not in last


# ---------- Item 4b：6h 无寄托置 background_failed ----------

class TestNoAnchorTimeout:
    def _bg_7h_task(self, tmp_db, monkeypatch, wake_at=None):
        """启动后背景的、7h 无更新的 backgrounded 任务（无进程信息）。"""
        import api.background as bg
        tid = _insert_task(tmp_db, status="backgrounded",
                           updated_at=_utc_ts(hours=7), wake_at=wake_at)
        # 服务启动时间拨到 8h 前 → 该任务属"启动后背景"分支（走 6h 规则而非 2h 重启规则）
        monkeypatch.setattr(bg, "_SERVER_START_TIME",
                            datetime.now(timezone.utc) - timedelta(hours=8))
        monkeypatch.setattr("tools.shell.get_background_processes", lambda: {})
        monkeypatch.setattr("tools.shell.get_orphan_processes", lambda: {})
        monkeypatch.setattr("tools.shell.adopt_orphan_processes", lambda *a, **k: 0)
        return tid

    def test_no_anchor_6h_marks_background_failed(self, tmp_db, monkeypatch):
        import api.background as bg
        tid = self._bg_7h_task(tmp_db, monkeypatch)
        spawned, rounds_done, release_loop = _install_monitor_harness(
            monkeypatch, bg, run_rounds=3)
        bg.start_background_monitor()
        try:
            assert rounds_done.wait(timeout=20)
        finally:
            release_loop.set()
        row = _task_row(tmp_db, tid)
        assert row["status"] == "background_failed"
        assert row["interruption_reason"] == "no_anchor_timeout"
        assert spawned == []

    def test_future_wake_anchor_survives_6h(self, tmp_db, monkeypatch):
        import api.background as bg
        future_wake = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
            '%Y-%m-%d %H:%M:%S')
        tid = self._bg_7h_task(tmp_db, monkeypatch, wake_at=future_wake)
        spawned, rounds_done, release_loop = _install_monitor_harness(
            monkeypatch, bg, run_rounds=3)
        bg.start_background_monitor()
        try:
            assert rounds_done.wait(timeout=20)
        finally:
            release_loop.set()
        assert _task_row(tmp_db, tid)["status"] == "backgrounded"

    def test_live_download_anchor_survives_6h(self, tmp_db, monkeypatch):
        import api.background as bg
        tid = self._bg_7h_task(tmp_db, monkeypatch)
        conn = tmp_db.db_connect()
        conn.execute(
            "INSERT INTO downloads (type, label, status, task_id) "
            "VALUES ('file', '进行中下载', 'downloading', ?)", (tid,))
        conn.commit()
        conn.close()
        spawned, rounds_done, release_loop = _install_monitor_harness(
            monkeypatch, bg, run_rounds=3)
        bg.start_background_monitor()
        try:
            assert rounds_done.wait(timeout=20)
        finally:
            release_loop.set()
        assert _task_row(tmp_db, tid)["status"] == "backgrounded"

    def test_young_task_without_anchor_not_failed(self, tmp_db, monkeypatch):
        """未满 6h 的无寄托任务不判死（保守）。"""
        import api.background as bg
        tid = _insert_task(tmp_db, status="backgrounded", updated_at=_utc_ts(hours=3))
        monkeypatch.setattr(bg, "_SERVER_START_TIME",
                            datetime.now(timezone.utc) - timedelta(hours=8))
        monkeypatch.setattr("tools.shell.get_background_processes", lambda: {})
        monkeypatch.setattr("tools.shell.get_orphan_processes", lambda: {})
        monkeypatch.setattr("tools.shell.adopt_orphan_processes", lambda *a, **k: 0)
        spawned, rounds_done, release_loop = _install_monitor_harness(
            monkeypatch, bg, run_rounds=3)
        bg.start_background_monitor()
        try:
            assert rounds_done.wait(timeout=20)
        finally:
            release_loop.set()
        assert _task_row(tmp_db, tid)["status"] == "backgrounded"


# ---------- Item 5：邮件监听 ----------

class TestEmailGovernance:
    def test_mark_email_seen_store_flags(self, monkeypatch):
        import core.email_service as es
        calls = {}

        class _FakeIMAP:
            def __init__(self, server):
                calls["server"] = server

            def login(self, u, p):
                calls["login"] = (u, p)

            def select(self, mbox):
                calls["select"] = mbox

            def store(self, mail_id, flags, value):
                calls["store"] = (mail_id, flags, value)

            def logout(self):
                calls["logout"] = True

        monkeypatch.setattr(es.imaplib, "IMAP4_SSL", _FakeIMAP)
        assert es.mark_email_seen("imap.example.com", "u@x.com", "pw", "42") is True
        assert calls["store"] == ("42", "+FLAGS", "\\Seen")
        assert calls["logout"] is True

    def test_mark_email_seen_empty_id_noop(self, monkeypatch):
        import core.email_service as es
        monkeypatch.setattr(es.imaplib, "IMAP4_SSL",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not connect")))
        assert es.mark_email_seen("srv", "u", "p", None) is False
        assert es.mark_email_seen("srv", "u", "p", "") is False

    def test_mark_seen_after_task_created_and_peek_fetch(self):
        """源码级顺序断言：取信用 PEEK（不标记），create_task 成功后才 mark_seen。"""
        import api.background as bg
        src = inspect.getsource(bg.start_email_listener)
        assert "mark_seen=False" in src
        assert src.index("create_task(") < src.index("mark_email_seen(")

    def test_reply_lines_distinct_per_terminal_status(self):
        import api.background as bg
        completed = bg._email_reply_lines(5, "completed", "总结A")
        failed = bg._email_reply_lines(5, "failed", "错误B")
        interrupted = bg._email_reply_lines(5, "interrupted", "半途C")
        backgrounded = bg._email_reply_lines(5, "backgrounded", "转入后台D")
        timeout = bg._email_reply_lines(5, "", "")
        # 四终态 + 超时各自措辞，互不雷同
        bodies = {completed[1], failed[1], interrupted[1], backgrounded[1], timeout[1]}
        assert len(bodies) == 5
        assert "completed." in completed[1]
        assert "FAILED" in failed[1]
        assert "interrupted" in interrupted[1]
        assert "background" in backgrounded[1]
        assert "still running" in timeout[1]
        # 失败/中断/后台/超时不再谎称 completed
        for word, body in (failed, interrupted, backgrounded, timeout):
            assert "completed." not in body[1]
        assert completed[0] == "completed" and timeout[0] == "running"

    def test_email_poll_recognizes_backgrounded_terminal(self):
        """源码级：邮件轮询终态集合包含 backgrounded（不再干等 10 分钟后谎称完成）。"""
        import api.background as bg
        src = inspect.getsource(bg.start_email_listener)
        assert '"completed", "failed", "interrupted", "backgrounded"' in src
