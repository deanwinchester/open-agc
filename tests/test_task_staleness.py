"""阶段 4 Task 3：任务系统健壮性测试（临时 sqlite DB）。

覆盖：
- _is_task_stale 陈腐判定（新鲜 / 陈腐 / 边界 / 异常输入）
- _resolve_task_for_query 复用 running 任务前的新鲜度检查（陈腐 → 另建新任务并复位旧任务）
- _get_step_offset 统一性：resume 后新步骤号 = MAX(step_number)+1，不与旧步骤撞号
- add_task_step 的 updated_at 心跳（健康长任务不被误判陈腐）
- ws.py 进度回调 step 单次偏移、历史过滤排除 system（源码级回归检查）
"""
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """把 api.db.DB_PATH 指到临时库。"""
    import api.db as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "test.db"))
    db_mod.init_db()
    return db_mod


def _insert_task(db_mod, status="running", updated_at=None, session_id=1):
    conn = db_mod.db_connect()
    cur = conn.execute(
        "INSERT INTO tasks (title, user_query, status, session_id) VALUES (?, ?, ?, ?)",
        ("测试任务", "原始查询", status, session_id))
    tid = cur.lastrowid
    if updated_at is not None:
        conn.execute("UPDATE tasks SET updated_at=? WHERE id=?", (updated_at, tid))
    conn.commit()
    conn.close()
    return tid


def _utc_ts(**ago):
    return (datetime.now(timezone.utc) - timedelta(**ago)).strftime('%Y-%m-%d %H:%M:%S')


# ---------- 陈腐判定 ----------

class TestIsTaskStale:
    def test_fresh_task_not_stale(self):
        from api.task_core import _is_task_stale
        assert _is_task_stale(_utc_ts(minutes=2)) is False
        assert _is_task_stale(_utc_ts(minutes=34)) is False

    def test_old_task_stale(self):
        from api.task_core import _is_task_stale
        assert _is_task_stale(_utc_ts(minutes=36)) is True
        assert _is_task_stale(_utc_ts(hours=2)) is True

    def test_llm_retry_window_not_stale(self):
        """健康任务最坏无步窗口（LLM 600s timeout x 3 retries ~= 30min）不得误判。"""
        from api.task_core import _is_task_stale
        assert _is_task_stale(_utc_ts(minutes=30)) is False

    def test_boundary_deterministic(self):
        """规则：严格大于 N 分钟才算陈腐；恰好 35 分钟仍新鲜。"""
        from api.task_core import _is_task_stale, _STALE_RUNNING_MINUTES
        assert _STALE_RUNNING_MINUTES == 35
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        assert _is_task_stale("2026-01-01 11:25:01", now=now) is False  # 34m59s
        assert _is_task_stale("2026-01-01 11:25:00", now=now) is False  # 恰好 35m
        assert _is_task_stale("2026-01-01 11:24:59", now=now) is True   # 35m01s

    def test_missing_or_garbage_is_stale(self):
        from api.task_core import _is_task_stale
        assert _is_task_stale(None) is True
        assert _is_task_stale("") is True
        assert _is_task_stale("not-a-date") is True

    def test_iso_T_format_accepted(self):
        from api.task_core import _is_task_stale
        ts = _utc_ts(minutes=40).replace(' ', 'T')
        assert _is_task_stale(ts) is True


# ---------- running 任务复用的新鲜度检查 ----------

class TestResolveTaskForQuery:
    def test_fresh_running_task_reused(self, tmp_db):
        from api.task_core import _resolve_task_for_query
        tid = _insert_task(tmp_db, status="running")  # updated_at 默认当前时间
        assert _resolve_task_for_query(1, "请继续处理当前的任务内容") == tid

    def test_stale_running_task_not_reused(self, tmp_db):
        from api.task_core import _resolve_task_for_query
        tid = _insert_task(tmp_db, status="running", updated_at=_utc_ts(minutes=40))
        new_tid = _resolve_task_for_query(1, "这是一个全新的问题，请帮我详细分析一下")
        assert new_tid != tid
        # 旧任务被复位为 interrupted，Guardian 可接管恢复
        conn = tmp_db.db_connect()
        row = conn.execute(
            "SELECT status, interruption_reason FROM tasks WHERE id=?", (tid,)).fetchone()
        conn.close()
        assert row["status"] == "interrupted"
        assert row["interruption_reason"] == "stale_running"


# ---------- step offset 统一性 ----------

class TestStepOffset:
    def test_empty_task_offset_zero(self, tmp_db):
        from api.task_core import _get_step_offset
        tid = _insert_task(tmp_db)
        assert _get_step_offset(tid) == 0

    def test_offset_equals_max_step(self, tmp_db):
        from api.task_core import _get_step_offset, add_task_step
        tid = _insert_task(tmp_db)
        for n in (1, 2, 3):
            add_task_step(tid, n, "shell")
        assert _get_step_offset(tid) == 3

    def test_resume_steps_do_not_collide(self, tmp_db):
        """模拟 resume：agent 重新从 1 编号；新步骤落库为 MAX+1 起，
        tool_done 按 (task_id, step_number) 的 UPDATE 不得误改旧步骤。"""
        from api.task_core import _get_step_offset, add_task_step
        tid = _insert_task(tmp_db)
        for n in (1, 2, 3):  # 旧步骤，已带结果
            add_task_step(tid, n, "shell", result_preview=f"old-{n}")
        offset = _get_step_offset(tid)
        assert offset == 3
        for agent_step in (1, 2):  # 恢复后 agent 重新从 1 编号
            add_task_step(tid, agent_step + offset, "write_file")
            conn = tmp_db.db_connect()
            conn.execute(
                "UPDATE task_steps SET result_preview=? WHERE task_id=? AND step_number=?",
                (f"new-{agent_step}", tid, agent_step + offset))
            conn.commit()
            conn.close()
        conn = tmp_db.db_connect()
        rows = conn.execute(
            "SELECT step_number, result_preview FROM task_steps WHERE task_id=? "
            "ORDER BY step_number", (tid,)).fetchall()
        conn.close()
        got = {r["step_number"]: r["result_preview"] for r in rows}
        assert sorted(got) == [1, 2, 3, 4, 5]           # 新步骤 4,5 = MAX+1 起，不撞号
        assert got[1] == "old-1" and got[3] == "old-3"  # 旧步骤未被误改
        assert got[4] == "new-1" and got[5] == "new-2"

    def test_add_task_step_heartbeat_keeps_task_fresh(self, tmp_db):
        """add_task_step 触碰 tasks.updated_at：健康长任务不会被误判陈腐。"""
        from api.task_core import add_task_step, _is_task_stale
        tid = _insert_task(tmp_db, status="running", updated_at=_utc_ts(minutes=40))
        add_task_step(tid, 1, "shell")
        conn = tmp_db.db_connect()
        updated = conn.execute("SELECT updated_at FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        conn.close()
        assert _is_task_stale(updated) is False


# ---------- ws.py 源码级回归检查 ----------

_WS_SRC = Path(__file__).resolve().parent.parent / "api" / "ws.py"


def test_ws_progress_step_offset_applied_once():
    """广播前不得对 event['step'] 二次偏移（落库值已在回调顶部单次调整）。"""
    src = _WS_SRC.read_text(encoding="utf-8")
    assert 'event["step"] = adjusted_step' in src                     # 单次偏移保留
    assert 'event["step"] = event.get("step", 0) + step_offset' not in src  # 二次偏移已删


def test_ws_history_filter_maps_system_rows_to_user_notices():
    """会话历史把 system 行（下载通知等）映射为 user 角色的【系统通知】纳入上下文：
    既不向严格 provider 发送多条 system 角色消息，也保证 agent 看得到下载通知。"""
    src = _WS_SRC.read_text(encoding="utf-8")
    assert "role != 'tool_step'" not in src
    # 两处加载（连接时 + 切换会话时）统一走 _load_session_history
    assert src.count("_load_session_history(ws_session_id)") >= 2
    # system 行纳入查询，并以 user 角色 + 【系统通知】前缀进入上下文
    assert "role IN ('user','agent','system')" in src
    assert 'f"【系统通知】{content}"' in src
