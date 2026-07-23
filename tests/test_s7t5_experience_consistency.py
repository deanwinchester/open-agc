"""阶段 7 Task 5（B5）：体验与一致性收尾。

覆盖：
- _user_facing 协议串剥离：[TASK_BACKGROUNDED] → None（不聊天/不广播）；
  [MAX_ITERATIONS_REACHED] → 剥前缀 + 可继续提示；普通文本原样返回
- ws.py 落库/广播出口统一走 _user_facing；handle_task_completion 状态解析仍拿原文（源码级回归）
- _resolve_task_for_query 归属矩阵：续接词无视长度 / >10 字盲续仅限 interrupted|backgrounded /
  窗口基于 updated_at / running 复用前查 _background_agents 存活 → 哨兵 + queue_message
- history_steps 回放含 sub_task 且按 id 稳定排序（行为 + 三处 SQL 源码检查）
- 死代码：is_heartbeat 不可达分支已删；resume 合成提示不落库
"""
import re
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


def _insert_task(db_mod, status="completed", updated_at=None, session_id=1):
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


# ---------- 协议串剥离（_user_facing） ----------

class TestUserFacing:
    def test_backgrounded_returns_none(self):
        """[TASK_BACKGROUNDED] 不存聊天、不广播——task_backgrounded 事件已单独提示。"""
        from api.ws import _user_facing
        assert _user_facing("[TASK_BACKGROUNDED] 命令仍在后台运行，自动转入后台。") is None

    def test_max_iterations_stripped_with_continue_hint(self):
        """[MAX_ITERATIONS_REACHED] 剥前缀、保留正文、附可继续提示。"""
        from api.ws import _user_facing
        out = _user_facing(
            "[MAX_ITERATIONS_REACHED] Agent stopped: Reached maximum iterations (50) "
            "without a final answer. The task may be incomplete.")
        assert out is not None
        assert "[MAX_ITERATIONS_REACHED]" not in out
        assert "Reached maximum iterations" in out   # 正文保留
        assert "继续" in out                          # 可继续提示

    def test_max_iterations_empty_body_still_has_hint(self):
        from api.ws import _user_facing
        out = _user_facing("[MAX_ITERATIONS_REACHED]")
        assert out and "继续" in out

    def test_normal_text_passthrough(self):
        from api.ws import _user_facing
        assert _user_facing("普通回复") == "普通回复"
        assert _user_facing(None) is None
        assert _user_facing("") == ""


# ---------- 归属启发式矩阵 ----------

class TestResolveAttribution:
    def test_continuation_prefix_ignores_length(self, tmp_db):
        """命中续接词（"继续"，仅 2 字）无视长度续接——completed 任务也可续。"""
        from api.task_core import _resolve_task_for_query
        tid = _insert_task(tmp_db, status="completed")
        assert _resolve_task_for_query(1, "继续") == tid
        conn = tmp_db.db_connect()
        st = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        conn.close()
        assert st == "running"

    def test_long_query_does_not_blind_continue_completed(self, tmp_db):
        """>10 字盲续仅限 interrupted/backgrounded——completed 已收官，长消息开新任务。"""
        from api.task_core import _resolve_task_for_query
        tid = _insert_task(tmp_db, status="completed")
        new_tid = _resolve_task_for_query(1, "请帮我写一个全新的详细计划方案")
        assert new_tid != tid

    def test_long_query_blind_continues_interrupted(self, tmp_db):
        from api.task_core import _resolve_task_for_query
        tid = _insert_task(tmp_db, status="interrupted")
        assert _resolve_task_for_query(1, "请帮我写一个全新的详细计划方案") == tid

    def test_long_query_blind_continues_backgrounded(self, tmp_db):
        from api.task_core import _resolve_task_for_query
        tid = _insert_task(tmp_db, status="backgrounded")
        assert _resolve_task_for_query(1, "请帮我写一个全新的详细计划方案") == tid

    def test_short_new_topic_creates_new_task(self, tmp_db):
        """短消息且非续接词 → 新话题，开新任务。"""
        from api.task_core import _resolve_task_for_query
        tid = _insert_task(tmp_db, status="interrupted")
        new_tid = _resolve_task_for_query(1, "好的")
        assert new_tid != tid

    def test_window_uses_updated_at_not_created_at(self, tmp_db):
        """窗口看 updated_at：created_at 很新（刚建）但 updated_at 陈旧 →
        即使命中续接词也不续接。"""
        from api.task_core import _resolve_task_for_query
        tid = _insert_task(tmp_db, status="interrupted", updated_at=_utc_ts(minutes=40))
        new_tid = _resolve_task_for_query(1, "继续")
        assert new_tid != tid

    def test_running_with_live_background_agent_queues_message(self, tmp_db, monkeypatch):
        """running 复用前查活：_background_agents 有存活 agent →
        返回哨兵且消息 queue_message 给它，不再复用开第二个 agent。"""
        import api.task_core as tc
        tid = _insert_task(tmp_db, status="running")
        sent = []

        class _FakeAgent:
            is_interrupted = False

            def queue_message(self, msg):
                sent.append(msg)

        monkeypatch.setitem(tc._background_agents, tid, _FakeAgent())
        assert tc._resolve_task_for_query(1, "新的指令请处理一下") == tc.QUEUED_TO_LIVE_AGENT
        assert sent == ["新的指令请处理一下"]

    def test_running_with_dead_background_agent_reused(self, tmp_db, monkeypatch):
        """后台 agent 已中断（句柄尸骸）→ 正常复用任务，不排队。"""
        import api.task_core as tc
        tid = _insert_task(tmp_db, status="running")

        class _DeadAgent:
            is_interrupted = True

            def queue_message(self, msg):
                raise AssertionError("不应排队给已中断的 agent")

        monkeypatch.setitem(tc._background_agents, tid, _DeadAgent())
        assert tc._resolve_task_for_query(1, "新的指令请处理一下") == tid

    def test_running_without_background_agent_reused(self, tmp_db):
        from api.task_core import _resolve_task_for_query
        tid = _insert_task(tmp_db, status="running")
        assert _resolve_task_for_query(1, "新的指令请处理一下") == tid


# ---------- 回放 sub_task + 排序稳定 ----------

class TestHistoryReplay:
    def test_broadcast_task_history_includes_sub_task_ordered_by_id(self, tmp_db, monkeypatch):
        """history_steps 携带 sub_task；created_at 全部相同时仍按 id（插入序）稳定返回。"""
        import api.state as state_mod
        from api.task_core import add_task_step
        tid = _insert_task(tmp_db)
        add_task_step(tid, 1, "shell", sub_task="子任务A")
        add_task_step(tid, 2, "write_file", sub_task="子任务B")
        add_task_step(tid, 3, "self_review")
        # created_at 秒级精度——强制全部相同，排序只能依赖 id
        conn = tmp_db.db_connect()
        conn.execute(
            "UPDATE task_steps SET created_at='2026-01-01 00:00:00' WHERE task_id=?", (tid,))
        conn.commit()
        conn.close()

        sent = []
        monkeypatch.setattr(state_mod, "_broadcast_to_websockets", sent.append)
        state_mod._broadcast_task_history(tid, 1, "interrupted")
        assert len(sent) == 1
        msg = sent[0]
        assert msg["type"] == "history_steps"
        steps = msg["steps"]
        assert [s["step_number"] for s in steps] == [1, 2, 3]
        assert [s["sub_task"] for s in steps] == ["子任务A", "子任务B", ""]


# ---------- 源码级回归检查 ----------

_SRC = Path(__file__).resolve().parent.parent


def _squash(text: str) -> str:
    """去掉空白与字符串引号，便于跨多行字符串拼接的 SQL 做整体匹配。"""
    return re.sub(r'[\s"\']+', '', text)


def test_replay_sql_has_sub_task_and_order_by_id():
    """三处 history_steps 查询（state.py 广播 + ws.py 连接回放/恢复回放）
    补 sub_task 列并统一 ORDER BY id；task_core 上下文重建同步改序。"""
    ws = _squash((_SRC / "api" / "ws.py").read_text(encoding="utf-8"))
    st = _squash((_SRC / "api" / "state.py").read_text(encoding="utf-8"))
    core = _squash((_SRC / "api" / "task_core.py").read_text(encoding="utf-8"))
    # ws.py 两处 history_steps 查询：连接时回放 + resume 回放
    assert ws.count("sub_taskFROMtask_stepsWHEREtask_id=?ORDERBYid") == 2
    # state.py _broadcast_task_history
    assert st.count("sub_taskFROMtask_stepsWHEREtask_id=?ORDERBYidASC") == 1
    # task_core get_task_context 回放重建
    assert "created_atFROMtask_stepsWHEREtask_id=?ORDERBYid" in core
    # 旧的 created_at 排序在 task_steps 回放查询中已清除（state/ws）
    assert "task_stepsWHEREtask_id=?ORDERBYcreated_at" not in ws
    assert "task_stepsWHEREtask_id=?ORDERBYcreated_at" not in st


def test_dead_is_heartbeat_branch_removed():
    """ws.py 主循环超时分支的 is_heartbeat 不可达代码（变量未定义）已删除。"""
    ws_src = (_SRC / "api" / "ws.py").read_text(encoding="utf-8")
    assert "is_heartbeat" not in ws_src


def test_resume_synthetic_prompt_not_saved():
    """resume_task_id 非空时跳过 save_message('user', query)——
    合成提示（"【系统提示】任务已恢复…"）不落聊天库。"""
    ws_src = (_SRC / "api" / "ws.py").read_text(encoding="utf-8")
    assert re.search(
        r'if not resume_task_id:\s*\n\s*try:\s*\n\s*save_message\("user", query, ws_session_id\)',
        ws_src), "resume 时 save_message('user', query) 应被 if not resume_task_id 跳过"


def test_completion_exits_strip_protocol_strings():
    """落库与广播出口统一走 _user_facing；handle_task_completion 状态解析仍拿原文。"""
    ws_src = (_SRC / "api" / "ws.py").read_text(encoding="utf-8")
    # 原文直存/直广播的写法已清除
    assert 'save_message("agent", _tb_response' not in ws_src
    assert 'save_message("agent", response,' not in ws_src
    assert '"content": response, "session_id"' not in ws_src
    # 状态解析入口仍用原文（_tb_response 未经 _user_facing 处理）
    assert re.search(r'handle_task_completion\(\s*\n\s*_tb_ws_task_id, _tb_response,', ws_src)


def test_queued_sentinel_short_circuits_new_run():
    """ws.py 处理 QUEUED_TO_LIVE_AGENT 哨兵：不再开新 agent 循环。"""
    ws_src = (_SRC / "api" / "ws.py").read_text(encoding="utf-8")
    assert "QUEUED_TO_LIVE_AGENT" in ws_src
    assert "return (None, None)" in ws_src
