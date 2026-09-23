# -*- coding: utf-8 -*-
"""中间交付自动续跑（promise-auto）测试。

生产实证 #529：agent 承诺「让后台继续拉，我每隔一会儿检查一次，一就绪
就自动跑」后任务直接 completed——后续无人跟进。机制：此类回复在
handle_task_completion 一律转 backgrounded + 定时唤醒。
"""
import pytest

from api.task_core import handle_task_completion


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import api.db as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "test.db"))
    db_mod.init_db()
    return db_mod


def _insert(db_mod, title="测试任务"):
    conn = db_mod.db_connect()
    cur = conn.execute(
        "INSERT INTO tasks (title, user_query, status, task_type, session_id) "
        "VALUES (?, ?, 'running', 'oneshot', 1)", (title, "q"))
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


PROMISE = ("镜像拉取还在进行。给你个中间汇报，别让你干等："
           "我每隔一会儿检查一次，一就绪就自动跑后续部署。")


def test_promise_becomes_backgrounded_with_wake(db):
    tid = _insert(db)
    result = handle_task_completion(tid, PROMISE, [], session_id=1)
    assert result == 'backgrounded'
    conn = db.db_connect()
    row = conn.execute(
        "SELECT status, wake_at, interruption_reason FROM tasks WHERE id=?",
        (tid,)).fetchone()
    conn.close()
    assert row[0] == "backgrounded"
    assert row[1]  # wake_at 已设置
    assert row[2] == "promise_auto"


def test_normal_completion_unaffected(db):
    tid = _insert(db)
    result = handle_task_completion(tid, "任务完成，已保存到 outputs/x.md。", [],
                                    session_id=1)
    assert result == 'completed'
    conn = db.db_connect()
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    assert row[0] == "completed"


def test_non_agent_promise_not_matched(db):
    """系统侧通知（下载完成自动推送）不是 agent 承诺，不应误转后台。"""
    tid = _insert(db)
    result = handle_task_completion(tid, "已提交下载，完成后系统会自动推送通知。", [],
                                    session_id=1)
    assert result == 'completed'
