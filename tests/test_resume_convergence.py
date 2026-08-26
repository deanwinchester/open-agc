"""阶段 7 Task 1（B1）：恢复链路收敛测试。

统一语义「认领即 running，不再降级」——消除 claim_task_for_resume 成功
（状态已翻 running）后又写回 interrupted 的窗口；窗口内 Guardian 可能
再次认领同一任务 → 双 agent 烧 token。

覆盖：
- claim_task_for_resume CAS：并发双认领仅一个成功；认领同时 resume_count+1；
  状态不匹配认领失败且不计数；跨路径互斥
- 各恢复路径状态集语义：wake/shell/下载直启 认 ('backgrounded',)；
  Scheduler 认 ('completed','failed') —— interrupted/backgrounded 不点火，
  completed/failed 点火且 CAS 失败不重复点火
- 下载直启 _direct_resume_background_task（真实临时库行为级）：
  原子 background_resumed 标志 rowcount 判赢 + CAS 认领，重复/并发调用仅一次起线程
- BgMonitor wake / shell 路径（真实临时库行为级）：认领后状态保持 running
  （无降级），resume_count 递增
- 退避 _is_backoff_elapsed：resume_count=0 立即可恢复；未到期不恢复；
  到期恢复；超档按最后一档（300s）封顶
- 源码级回归：三处 CAS 后降级已删、Guardian 旧 +1 已删并接入退避/超限、
  Scheduler 点火收紧且线程前 CAS、shell 路径补 CAS、下载标志原子化
"""
import os
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """把 api.db.DB_PATH 指到临时库；routes_settings 持有 DB_PATH 值引用，同步指过去。"""
    import api.db as db_mod
    db_file = str(tmp_path / "test.db")
    monkeypatch.setattr(db_mod, "DB_PATH", db_file)
    db_mod.init_db()
    import api.routes.routes_settings as rs
    monkeypatch.setattr(rs, "DB_PATH", db_file)
    return db_mod


def _insert_task(db_mod, status="backgrounded", resume_count=0, max_resume_count=10,
                 updated_at=None, wake_at=None, task_type="oneshot"):
    conn = db_mod.db_connect()
    cur = conn.execute(
        "INSERT INTO tasks (title, user_query, status, task_type, resume_count, "
        "max_resume_count, session_id) VALUES (?, ?, ?, ?, ?, ?, 1)",
        ("测试任务", "原始查询", status, task_type, resume_count, max_resume_count))
    tid = cur.lastrowid
    if updated_at is not None:
        conn.execute("UPDATE tasks SET updated_at=? WHERE id=?", (updated_at, tid))
    if wake_at is not None:
        conn.execute("UPDATE tasks SET wake_at=? WHERE id=?", (wake_at, tid))
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


def _insert_download(db_mod, tid, status="completed", background_resumed=0):
    conn = db_mod.db_connect()
    cur = conn.execute(
        "INSERT INTO downloads (type, label, status, task_id, background_resumed) "
        "VALUES ('file', '测试文件', ?, ?, ?)",
        (status, tid, background_resumed))
    dl_id = cur.lastrowid
    conn.commit()
    conn.close()
    return dl_id


def _utc_ts(**ago):
    return (datetime.now(timezone.utc) - timedelta(**ago)).strftime('%Y-%m-%d %H:%M:%S')


# ---------- claim_task_for_resume CAS ----------

class TestClaimCAS:
    def test_concurrent_double_claim_single_winner(self, tmp_db):
        """并发双认领（同一任务、同一路径状态集）：恰好一个成功，resume_count 只 +1。"""
        from api.task_core import claim_task_for_resume
        tid = _insert_task(tmp_db, status="backgrounded")
        barrier = threading.Barrier(8)
        results = []

        def _worker():
            barrier.wait(timeout=10)
            results.append(claim_task_for_resume(tid, ('backgrounded',)))

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert results.count(True) == 1
        assert results.count(False) == 7
        st = _task_state(tmp_db, tid)
        assert st["status"] == "running"
        assert st["resume_count"] == 1

    def test_claim_increments_resume_count(self, tmp_db):
        from api.task_core import claim_task_for_resume
        tid = _insert_task(tmp_db, status="interrupted", resume_count=2)
        assert claim_task_for_resume(tid, ('interrupted',)) is True
        st = _task_state(tmp_db, tid)
        assert st["status"] == "running" and st["resume_count"] == 3

    def test_claim_wrong_status_fails_without_counting(self, tmp_db):
        from api.task_core import claim_task_for_resume
        tid = _insert_task(tmp_db, status="running", resume_count=4)
        assert claim_task_for_resume(tid, ('interrupted', 'backgrounded')) is False
        st = _task_state(tmp_db, tid)
        assert st["status"] == "running" and st["resume_count"] == 4

    def test_cross_path_exclusion(self, tmp_db):
        """一条路径认领成功后，其他路径（各自状态集）再认领必失败——双跑被堵。"""
        from api.task_core import claim_task_for_resume
        tid = _insert_task(tmp_db, status="backgrounded")
        assert claim_task_for_resume(tid, ('backgrounded',)) is True         # wake/shell/下载 胜
        assert claim_task_for_resume(tid, ('interrupted',)) is False         # Guardian
        assert claim_task_for_resume(tid, ('backgrounded',)) is False        # 另一 monitor 路径
        assert claim_task_for_resume(tid, ('completed', 'failed')) is False  # Scheduler
        assert _task_state(tmp_db, tid)["resume_count"] == 1


# ---------- Scheduler 点火语义 ----------

class TestSchedulerFireSemantics:
    """Scheduler 点火：status IN ('completed','failed') + 起线程前 CAS。
    interrupted 让位 Guardian、backgrounded 让位 BgMonitor。"""

    @pytest.mark.parametrize("status", ["interrupted", "backgrounded", "running"])
    def test_non_terminal_statuses_cannot_be_claimed_for_fire(self, tmp_db, status):
        from api.task_core import claim_task_for_resume
        tid = _insert_task(tmp_db, status=status, task_type="scheduled")
        assert claim_task_for_resume(tid, ('completed', 'failed')) is False
        assert _task_state(tmp_db, tid)["status"] == status

    def test_completed_task_fire_claim_succeeds_once(self, tmp_db):
        from api.task_core import claim_task_for_resume
        tid = _insert_task(tmp_db, status="completed", task_type="scheduled")
        assert claim_task_for_resume(tid, ('completed', 'failed')) is True   # 点火
        assert claim_task_for_resume(tid, ('completed', 'failed')) is False  # CAS 失败不重复点火
        st = _task_state(tmp_db, tid)
        assert st["status"] == "running" and st["resume_count"] == 1

    def test_failed_task_fire_claim_succeeds_once(self, tmp_db):
        from api.task_core import claim_task_for_resume
        tid = _insert_task(tmp_db, status="failed", task_type="scheduled")
        assert claim_task_for_resume(tid, ('completed', 'failed')) is True
        assert claim_task_for_resume(tid, ('completed', 'failed')) is False
        assert _task_state(tmp_db, tid)["status"] == "running"

    def test_scheduler_source_tightened_and_claims_before_spawn(self):
        src = _BG_SRC.read_text(encoding="utf-8")
        assert "next_run_at <= ? AND status IN ('completed','failed')" in src
        assert "next_run_at <= ? AND status != 'running'" not in src
        assert "claim_task_for_resume(task_id, ('completed', 'failed'))" in src


# ---------- 退避 ----------

class TestBackoff:
    def test_resume_count_zero_immediately_resumable(self):
        """resume_count=0（首次恢复）无退避：刚更新的任务也立即可恢复。"""
        from api.background import _is_backoff_elapsed
        assert _is_backoff_elapsed(_utc_ts(seconds=0), 0) is True

    def test_not_elapsed_blocks_resume(self):
        from api.background import _is_backoff_elapsed
        # resume_count=1 → 退避 30s；10s 前更新的任务未到期
        assert _is_backoff_elapsed(_utc_ts(seconds=10), 1) is False

    def test_elapsed_allows_resume(self):
        from api.background import _is_backoff_elapsed
        assert _is_backoff_elapsed(_utc_ts(seconds=31), 1) is True
        # resume_count=2 → 退避 120s
        assert _is_backoff_elapsed(_utc_ts(seconds=100), 2) is False
        assert _is_backoff_elapsed(_utc_ts(seconds=121), 2) is True

    def test_beyond_schedule_capped_at_last_delay(self):
        """超出日程表档位数按最后一档（300s）封顶，任务不滞留 interrupted。"""
        from api.background import _is_backoff_elapsed, _BACKOFF_SCHEDULE
        cap = _BACKOFF_SCHEDULE[-1]
        rc = len(_BACKOFF_SCHEDULE) + 3
        assert _is_backoff_elapsed(_utc_ts(seconds=cap - 1), rc) is False
        assert _is_backoff_elapsed(_utc_ts(seconds=cap + 1), rc) is True


# ---------- 下载直启（行为级，真实临时库） ----------

@pytest.fixture()
def fake_threads(monkeypatch):
    """记录 routes_settings 内 threading.Thread 的创建（不真正启动 worker）。"""
    import api.routes.routes_settings as rs
    spawned = []

    class _FakeThread:
        def __init__(self, target=None, args=(), daemon=None, **kw):
            spawned.append({"target": target, "args": args})

        def start(self):
            pass

    monkeypatch.setattr(rs, "threading", types.SimpleNamespace(Thread=_FakeThread))
    return rs, spawned


class TestDirectResumeBackgroundTask:
    def test_claims_and_spawns_with_resume_count(self, tmp_db, fake_threads):
        rs, spawned = fake_threads
        tid = _insert_task(tmp_db, status="backgrounded")
        dl_id = _insert_download(tmp_db, tid)
        ctx = [{"role": "user", "content": "原始任务"}]
        rs._direct_resume_background_task(tid, "原始查询", ctx, download_id=dl_id)
        assert len(spawned) == 1
        assert spawned[0]["target"] is rs._run_background_task
        assert spawned[0]["args"] == (tid, "原始查询", ctx, True)
        st = _task_state(tmp_db, tid)
        assert st["status"] == "running"   # 认领即 running，不再降级 interrupted
        assert st["resume_count"] == 1     # 下载路径也计数
        conn = tmp_db.db_connect()
        flag = conn.execute(
            "SELECT background_resumed FROM downloads WHERE id=?", (dl_id,)).fetchone()[0]
        conn.close()
        assert flag == 1

    def test_double_call_only_one_resume(self, tmp_db, fake_threads):
        """重复触发（下载完成事件 + BgMonitor 轮询）：仅一次起线程。"""
        rs, spawned = fake_threads
        tid = _insert_task(tmp_db, status="backgrounded")
        dl_id = _insert_download(tmp_db, tid)
        ctx = [{"role": "user", "content": "x"}]
        rs._direct_resume_background_task(tid, "q", ctx, download_id=dl_id)
        rs._direct_resume_background_task(tid, "q", ctx, download_id=dl_id)
        assert len(spawned) == 1
        assert _task_state(tmp_db, tid)["resume_count"] == 1

    def test_concurrent_direct_resume_single_winner(self, tmp_db, fake_threads):
        rs, spawned = fake_threads
        tid = _insert_task(tmp_db, status="backgrounded")
        dl_id = _insert_download(tmp_db, tid)
        ctx = [{"role": "user", "content": "x"}]
        barrier = threading.Barrier(6)

        def _worker():
            barrier.wait(timeout=10)
            rs._direct_resume_background_task(tid, "q", ctx, download_id=dl_id)

        threads = [threading.Thread(target=_worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert len(spawned) == 1
        st = _task_state(tmp_db, tid)
        assert st["status"] == "running" and st["resume_count"] == 1

    def test_flag_already_consumed_skips(self, tmp_db, fake_threads):
        """background_resumed 已被另一路径消费（rowcount=0）→ 直接放弃，不认领。"""
        rs, spawned = fake_threads
        tid = _insert_task(tmp_db, status="backgrounded")
        dl_id = _insert_download(tmp_db, tid, background_resumed=1)
        rs._direct_resume_background_task(tid, "q", [], download_id=dl_id)
        assert spawned == []
        st = _task_state(tmp_db, tid)
        assert st["status"] == "backgrounded" and st["resume_count"] == 0

    def test_non_backgrounded_task_claim_fails(self, tmp_db, fake_threads):
        """任务非 backgrounded（如已 completed）→ CAS 失败不起线程。"""
        rs, spawned = fake_threads
        tid = _insert_task(tmp_db, status="completed")
        dl_id = _insert_download(tmp_db, tid)
        rs._direct_resume_background_task(tid, "q", [], download_id=dl_id)
        assert spawned == []
        assert _task_state(tmp_db, tid)["status"] == "completed"


# ---------- BgMonitor wake / shell 路径（行为级，真实临时库） ----------

def _install_monitor_harness(monkeypatch, bg):
    """把 BgMonitor 的 monitor_loop 跑在真线程里、恢复 worker 走假线程，
    sleep 改为事件闸门（首轮迭代结束后放行主线程断言）。"""
    spawned = []
    loop_started = threading.Event()
    release_loop = threading.Event()

    def _thread_factory(target=None, args=(), daemon=None, **kw):
        if getattr(target, "__name__", "") == "monitor_loop":
            return threading.Thread(target=target, daemon=True)
        spawned.append({"target": target, "args": args})
        return types.SimpleNamespace(start=lambda: None)

    monkeypatch.setattr(bg, "threading", types.SimpleNamespace(Thread=_thread_factory))

    real_sleep = time.sleep
    calls = {"n": 0}

    def _fake_sleep(seconds):
        calls["n"] += 1
        if calls["n"] == 1:
            loop_started.set()
            release_loop.wait(timeout=30)
        elif release_loop.is_set():
            # 断言完成后永久泊住——放行后循环线程若继续跑，teardown 还原
            # monkeypatch 后会打到真实数据库（见终审 I-1）
            threading.Event().wait()
        else:
            real_sleep(seconds)

    monkeypatch.setattr(bg._time, "sleep", _fake_sleep)
    return spawned, loop_started, release_loop


class TestMonitorResumePaths:
    def test_wake_path_claims_increments_and_stays_running(self, tmp_db, monkeypatch):
        """定时唤醒：CAS 认领 → 直接起 worker，状态保持 running（无降级），rc+1。"""
        import api.background as bg
        tid = _insert_task(tmp_db, status="backgrounded", wake_at="2000-01-01 00:00:00")
        spawned, loop_started, release_loop = _install_monitor_harness(monkeypatch, bg)

        bg.start_background_monitor()
        try:
            assert loop_started.wait(timeout=15), "monitor loop did not reach first sleep"
        finally:
            release_loop.set()
        assert len(spawned) == 1
        assert spawned[0]["target"] is bg._run_background_task
        assert spawned[0]["args"][0] == tid and spawned[0]["args"][3] is True
        st = _task_state(tmp_db, tid)
        assert st["status"] == "running"   # 若仍写回 interrupted 则此处暴露
        assert st["resume_count"] == 1     # wake 路径计数

    def test_shell_done_path_claims_increments_and_stays_running(self, tmp_db, monkeypatch):
        """shell 完成：补上的 CAS 认领 → 起 worker，状态保持 running（无降级），rc+1。"""
        import api.background as bg
        tid = _insert_task(tmp_db, status="backgrounded")
        spawned, loop_started, release_loop = _install_monitor_harness(monkeypatch, bg)
        # 进程已死 → 触发恢复（monitor 循环内是本地 import，须打 tools.shell 源头）
        monkeypatch.setattr("tools.shell.get_background_processes",
                            lambda: {str(tid): {"999999": {"pid": 999999, "output_file": "",
                                                           "command": "echo hi", "started_at": 1.0}}})
        monkeypatch.setattr(bg, "pid_alive", lambda pid: False)
        monkeypatch.setattr("tools.shell.cleanup_background_process", lambda key: None)
        monkeypatch.setattr(bg, "_broadcast_task_history", lambda *a, **k: None)

        bg.start_background_monitor()
        try:
            assert loop_started.wait(timeout=15), "monitor loop did not reach first sleep"
        finally:
            release_loop.set()
        assert len(spawned) == 1
        assert spawned[0]["target"] is bg._run_background_task
        assert spawned[0]["args"][0] == tid and spawned[0]["args"][3] is True
        st = _task_state(tmp_db, tid)
        assert st["status"] == "running"
        assert st["resume_count"] == 1     # shell 路径计数


# ---------- 源码级回归（Guardian / Scheduler / 降级删除） ----------

_BG_SRC = Path(__file__).resolve().parent.parent / "api" / "background.py"
_RS_SRC = Path(__file__).resolve().parent.parent / "api" / "routes" / "routes_settings.py"
_WS_SRC = Path(__file__).resolve().parent.parent / "api" / "ws.py"


class TestConvergenceSource:
    def test_backoff_wired_into_guardian_selection(self):
        src = _BG_SRC.read_text(encoding="utf-8")
        assert "_is_backoff_elapsed(_updated, _rc)" in src

    def test_max_resume_exceeded_marks_background_failed(self):
        src = _BG_SRC.read_text(encoding="utf-8")
        assert "max_resume_exceeded" in src

    def test_resume_count_single_counting_point(self):
        """background.py 不再有任何 resume_count 自增（唯一计数点在
        task_core.claim_task_for_resume 的 CAS 内）。"""
        src = _BG_SRC.read_text(encoding="utf-8")
        assert "resume_count = resume_count + 1" not in src

    def test_no_post_claim_downgrade_to_interrupted(self):
        """三处 CAS 后降级已删：认领成功后不得再写回 interrupted。"""
        src = _BG_SRC.read_text(encoding="utf-8")
        assert '"用户已回答，恢复执行"' not in src
        assert '"定时唤醒", interruption_reason="background_complete"' not in src
        assert '"后台命令完成", interruption_reason="background_complete"' not in src

    def test_shell_path_claims_backgrounded_before_spawn(self):
        src = _BG_SRC.read_text(encoding="utf-8")
        assert "claim_task_for_resume(tid, ('backgrounded',))" in src

    def test_download_resume_atomic_flag_and_cas(self):
        src = _RS_SRC.read_text(encoding="utf-8")
        assert ("UPDATE downloads SET background_resumed=1 "
                "WHERE id=? AND background_resumed=0") in src
        assert "claim_task_for_resume(task_id, ('backgrounded',))" in src

    def test_ws_late_sandbox_resume_uses_cas_no_downgrade(self):
        """ws.py 迟沙箱授权恢复：起线程前 CAS 认领，不再无条件写回 interrupted。"""
        src = _WS_SRC.read_text(encoding="utf-8")
        assert "claim_task_for_resume(_tid2, ('backgrounded', 'interrupted'))" in src
        assert "延迟授权触发恢复" not in src


# ---------- 成功完成清零 resume_count ----------

class TestResumeCountResetOnCompletion:
    """Scheduler 点火/手动恢复的 CAS 每次 +1；成功完成必须清零，否则长寿命
    cron 任务计数单调累积，一次普通中断即被判超限。"""

    def test_completed_resets_resume_count(self, tmp_db):
        from api.task_core import handle_task_completion
        tid = _insert_task(tmp_db, status="running", resume_count=7)
        result = handle_task_completion(tid, "任务已完成，结果如下。", [])
        assert result == 'completed'
        st = _task_state(tmp_db, tid)
        assert st["status"] == "completed" and st["resume_count"] == 0

    def test_interrupted_keeps_resume_count(self, tmp_db):
        """max_iterations 中断不归零——退避/超限判定依赖该计数。"""
        from api.task_core import handle_task_completion
        tid = _insert_task(tmp_db, status="running", resume_count=3)
        result = handle_task_completion(tid, "[MAX_ITERATIONS_REACHED] 步骤过多", [])
        assert result == 'interrupted'
        st = _task_state(tmp_db, tid)
        assert st["status"] == "interrupted" and st["resume_count"] == 3

    def test_failed_keeps_resume_count(self, tmp_db):
        from api.task_core import handle_task_completion
        tid = _insert_task(tmp_db, status="running", resume_count=5)
        result = handle_task_completion(tid, "", [])
        assert result == 'failed'
        st = _task_state(tmp_db, tid)
        assert st["status"] == "failed" and st["resume_count"] == 5


# ---------- LLM_ERROR 必须判失败，不得伪装 completed ----------

class TestLlmErrorCompletion:
    """[LLM_ERROR] 前缀响应此前落到 Normal completion 分支，任务被错误标为
    completed（生产实证：任务列表里「已完成」但执行结果是 APIConnectionError）。"""

    def test_llm_error_marks_failed(self, tmp_db):
        from api.task_core import handle_task_completion
        tid = _insert_task(tmp_db, status="running")
        result = handle_task_completion(
            tid,
            "[LLM_ERROR] 模型服务调用失败（APIConnectionError，第 2 轮）。点「继续」可重试；反复失败请检查模型配置或更换模型。",
            [])
        assert result == 'failed'
        st = _task_state(tmp_db, tid)
        assert st["status"] == "failed"
        assert st["interruption_reason"] == "error"

    def test_llm_error_keeps_resume_count(self, tmp_db):
        from api.task_core import handle_task_completion
        tid = _insert_task(tmp_db, status="running", resume_count=4)
        result = handle_task_completion(
            tid, "[LLM_ERROR] 模型服务调用失败", [])
        assert result == 'failed'
        st = _task_state(tmp_db, tid)
        assert st["status"] == "failed" and st["resume_count"] == 4
