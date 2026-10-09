# -*- coding: utf-8 -*-
"""manage_downloads 工具测试：list/resume/delete 走真实下载记录函数。"""
import sqlite3

import pytest

from tools.manage_downloads import ManageDownloadsTool


@pytest.fixture()
def db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "chat_history.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE downloads (id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT, "
        "label TEXT, repo_id TEXT, filename TEXT, source TEXT, url TEXT, "
        "target_path TEXT, partial_path TEXT, total_size INTEGER, "
        "downloaded_bytes INTEGER, status TEXT, progress REAL, error_message TEXT, "
        "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME "
        "DEFAULT CURRENT_TIMESTAMP, task_id INTEGER, background_resumed INTEGER DEFAULT 0)")
    conn.execute(
        "CREATE TABLE download_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "download_id INTEGER, event_type TEXT, message TEXT, details TEXT, "
        "created_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
    conn.commit()
    conn.close()
    import api.routes.routes_settings as rs
    monkeypatch.setattr(rs, "DB_PATH", db_path)
    return db_path


def _insert(db_path, label, status, progress=0.0, partial=""):
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO downloads (type, label, filename, source, partial_path, "
        "total_size, downloaded_bytes, status, progress) "
        "VALUES ('model', ?, ?, 'huggingface', ?, 1000000, 0, ?, ?)",
        (label, label, partial, status, progress))
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return rid


def test_list_empty(db):
    assert "没有记录" in ManageDownloadsTool().execute(action="list")


def test_list_shows_status_and_size(db):
    _insert(db, "mymodel.gguf", "paused", progress=0.355)
    out = ManageDownloadsTool().execute(action="list")
    assert "paused" in out and "mymodel.gguf" in out and "36%" in out


def test_resume_requires_paused_or_failed(db):
    rid = _insert(db, "x.gguf", "completed")
    out = ManageDownloadsTool().execute(action="resume", download_id=rid)
    assert "续传失败" in out and "completed" in out


def test_resume_nonexistent(db):
    out = ManageDownloadsTool().execute(action="resume", download_id=999)
    assert "续传失败" in out


def test_delete_removes_record(db):
    rid = _insert(db, "gone.gguf", "failed")
    out = ManageDownloadsTool().execute(action="delete", download_id=rid)
    assert "已删除" in out
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM downloads WHERE id=?", (rid,)).fetchone()[0]
    conn.close()
    assert n == 0


def test_unknown_action(db):
    assert "未知 action" in ManageDownloadsTool().execute(action="hack")
