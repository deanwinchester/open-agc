# -*- coding: utf-8 -*-
"""schedule_task 工具测试：cron 周期任务的创建/查询/启停。

背景：cron 调度器早存在但无 agent 入口（生产实证：用户要每 10 分钟发
新闻，agent 研究半天没建成——创建入口只有 REST API）。
"""
import pytest

from tools.schedule_task import ScheduleTaskTool, _next_run_utc


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import api.db as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "test.db"))
    db_mod.init_db()
    return db_mod


class _Ctx:
    session_id = 7


def test_create_scheduled_task(db):
    out = ScheduleTaskTool().execute(
        action="create", title="每10分钟发热门新闻",
        query="搜索最新热门新闻并汇报", cron="*/10 * * * *",
        _agent_context=_Ctx())
    assert "已创建定时任务 #" in out
    import sqlite3
    conn = db.db_connect()
    row = conn.execute(
        "SELECT task_type, schedule_cron, schedule_enabled, next_run_at, session_id "
        "FROM tasks").fetchone()
    conn.close()
    assert row[0] == "scheduled" and row[1] == "*/10 * * * *"
    assert row[2] == 1 and row[3] and row[4] == 7


def test_create_invalid_cron(db):
    out = ScheduleTaskTool().execute(
        action="create", title="t", query="q", cron="not a cron",
        _agent_context=_Ctx())
    assert "cron 表达式无效" in out


def test_create_missing_params(db):
    out = ScheduleTaskTool().execute(action="create", title="t")
    assert "需要 title、query、cron" in out


def test_list_and_toggle(db):
    tool = ScheduleTaskTool()
    assert "没有定时任务" in tool.execute(action="list", _agent_context=_Ctx())
    tool.execute(action="create", title="t1", query="q", cron="0 * * * *",
                 _agent_context=_Ctx())
    out = tool.execute(action="list", _agent_context=_Ctx())
    assert "#1" in out and "启用" in out
    out = tool.execute(action="toggle", task_id=1)
    assert "已停用" in out
    out = tool.execute(action="toggle", task_id=1)
    assert "已启用" in out


def test_toggle_nonexistent(db):
    out = ScheduleTaskTool().execute(action="toggle", task_id=999)
    assert "不存在" in out


def test_next_run_utc_format():
    s = _next_run_utc("*/10 * * * *")
    import datetime
    datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
