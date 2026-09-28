# -*- coding: utf-8 -*-
"""会话搜索（GET /api/sessions/search）与首任务自动命名（_maybe_auto_title）的回归测试。"""
import asyncio
import sqlite3
import time

import api.task_core as task_core
from api.routes import routes_sessions


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, "
        "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "CREATE TABLE messages ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT, content TEXT, "
        "timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, session_id INTEGER, "
        "task_id INTEGER, attachments TEXT)"
    )
    conn.execute(
        "CREATE TABLE tasks ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, user_query TEXT, session_id INTEGER)"
    )
    conn.commit()
    conn.close()


def _seed(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO sessions (id, name) VALUES (1, '会话 1')")
    conn.execute("INSERT INTO sessions (id, name) VALUES (2, '部署笔记')")
    conn.execute("INSERT INTO messages (role, content, session_id) VALUES ('user', '帮我查一下 grafana 怎么部署', 1)")
    conn.execute("INSERT INTO messages (role, content, session_id) VALUES ('agent', 'grafana 可以用 docker 部署', 1)")
    conn.execute("INSERT INTO messages (role, content, session_id) VALUES ('user', '今天天气怎么样', 2)")
    conn.commit()
    conn.close()


def _search(db_path, q):
    orig = routes_sessions.DB_PATH
    routes_sessions.DB_PATH = db_path
    try:
        return asyncio.run(routes_sessions.search_sessions(q))["results"]
    finally:
        routes_sessions.DB_PATH = orig


def test_search_hits_message_content(tmp_path):
    db = str(tmp_path / "chat.db")
    _make_db(db)
    _seed(db)
    res = _search(db, "grafana")
    assert [r["id"] for r in res] == [1]
    assert "grafana" in res[0]["snippet"]
    assert res[0]["message_count"] == 2


def test_search_hits_session_name(tmp_path):
    db = str(tmp_path / "chat.db")
    _make_db(db)
    _seed(db)
    res = _search(db, "部署")
    ids = {r["id"] for r in res}
    assert 2 in ids  # 名称命中
    assert 1 in ids  # 消息内容也含“部署”


def test_search_empty_query_returns_empty(tmp_path):
    db = str(tmp_path / "chat.db")
    _make_db(db)
    _seed(db)
    assert _search(db, "") == []
    assert _search(db, "   ") == []


def test_search_no_match(tmp_path):
    db = str(tmp_path / "chat.db")
    _make_db(db)
    _seed(db)
    assert _search(db, "不存在的关键词xyz") == []


class _FakeMsg:
    content = "Grafana 监控部署"


class _FakeChoice:
    message = _FakeMsg()


class _FakeResp:
    choices = [_FakeChoice()]


class _FakeLLM:
    def chat(self, messages, **kw):
        return _FakeResp(), "fake-model"


def _run_auto_title(db_path, monkeypatch, session_name="会话 3", agent_msgs=0,
                    expect_change=True, llm_output="Grafana 监控部署"):
    """驱动 _maybe_auto_title 的守护线程并等待其落库，返回最终会话名。"""
    import core.llm_client as llm_mod

    class _Msg:
        content = llm_output

    class _LLM:
        def chat(self, messages, **kw):
            return type("R", (), {"choices": [type("C", (), {"message": _Msg})()]} )(), "fake-model"

    monkeypatch.setattr(llm_mod, "LLMClient", _LLM)
    monkeypatch.setattr(task_core, "db_connect", lambda: sqlite3.connect(db_path))

    conn = sqlite3.connect(db_path)
    cur = conn.execute("INSERT INTO sessions (id, name) VALUES (10, ?)", (session_name,))
    sid = cur.lastrowid
    conn.execute("INSERT INTO tasks (user_query, session_id) VALUES ('帮我部署 grafana 监控', ?)", (sid,))
    tid = conn.execute("SELECT id FROM tasks WHERE session_id=?", (sid,)).fetchone()[0]
    for _ in range(agent_msgs):
        conn.execute("INSERT INTO messages (role, content, session_id) VALUES ('agent', 'x', ?)", (sid,))
    conn.commit()
    conn.close()

    task_core._maybe_auto_title(sid, tid)
    # 命中条件时守护线程几十毫秒内落库；不命中时不会有任何写入，短等即可
    deadline = time.time() + (3 if expect_change else 0.6)
    while time.time() < deadline:
        conn = sqlite3.connect(db_path)
        name = conn.execute("SELECT name FROM sessions WHERE id=?", (sid,)).fetchone()[0]
        conn.close()
        if name != session_name:
            return name
        time.sleep(0.05)
    return session_name


def test_auto_title_renames_default_named_session(tmp_path, monkeypatch):
    db = str(tmp_path / "chat.db")
    _make_db(db)
    name = _run_auto_title(db, monkeypatch, session_name="会话 3")
    assert name == "Grafana 监控部署"


def test_auto_title_skips_user_named_session(tmp_path, monkeypatch):
    db = str(tmp_path / "chat.db")
    _make_db(db)
    name = _run_auto_title(db, monkeypatch, session_name="我的重要会话", expect_change=False)
    assert name == "我的重要会话"


def test_auto_title_skips_when_agent_already_replied(tmp_path, monkeypatch):
    db = str(tmp_path / "chat.db")
    _make_db(db)
    name = _run_auto_title(db, monkeypatch, session_name="会话 5", agent_msgs=2, expect_change=False)
    assert name == "会话 5"


def test_auto_title_never_touches_main_session(tmp_path, monkeypatch):
    """主会话（id=1）永不自动改名——用户实证主会话被误改。"""
    db = str(tmp_path / "chat.db")
    _make_db(db)
    import core.llm_client as llm_mod
    monkeypatch.setattr(llm_mod, "LLMClient", _FakeLLM)
    monkeypatch.setattr(task_core, "db_connect", lambda: sqlite3.connect(db))
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO sessions (id, name) VALUES (1, '默认会话')")
    conn.execute("INSERT INTO tasks (user_query, session_id) VALUES ('部署 grafana', 1)")
    conn.commit()
    conn.close()
    task_core._maybe_auto_title(1, 1)
    time.sleep(0.5)
    conn = sqlite3.connect(db)
    name = conn.execute("SELECT name FROM sessions WHERE id=1").fetchone()[0]
    conn.close()
    assert name == "默认会话"


def test_auto_title_rejects_meta_title(tmp_path, monkeypatch):
    """模型产出「原始查询主题概括」这类元描述时丢弃，回退请求截断。"""
    db = str(tmp_path / "chat.db")
    _make_db(db)
    name = _run_auto_title(db, monkeypatch, session_name="会话 7",
                           llm_output="原始查询主题概括")
    assert name == "帮我部署 grafana 监控"[:16]

