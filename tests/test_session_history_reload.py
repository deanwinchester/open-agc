# -*- coding: utf-8 -*-
"""Regression test: _load_session_history must return the latest DB messages.

The WebSocket handler caches session_history at connection time; without
reloading before each turn, messages written by background tasks or other
connections are missing from the next agent context.
"""
import sqlite3

from api import ws as api_ws


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE messages ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT, content TEXT, "
        "timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, session_id INTEGER, "
        "task_id INTEGER, attachments TEXT)"
    )
    conn.commit()
    conn.close()


def _insert(db_path, role, content, session_id=1):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO messages (role, content, session_id) VALUES (?,?,?)",
        (role, content, session_id),
    )
    conn.commit()
    conn.close()


def test_load_session_history_reflects_new_messages(tmp_path, monkeypatch):
    db_path = str(tmp_path / "chat.db")
    _make_db(db_path)
    _insert(db_path, "user", "hello")
    _insert(db_path, "agent", "hi")

    monkeypatch.setattr(api_ws, "db_connect", lambda: sqlite3.connect(db_path))

    h1 = api_ws._load_session_history(1)
    assert [m["content"] for m in h1] == ["hello", "hi"]

    # A later message (e.g. from a background task) must appear on reload.
    _insert(db_path, "agent", "latest reply")

    h2 = api_ws._load_session_history(1)
    assert [m["content"] for m in h2] == ["hello", "hi", "latest reply"]
