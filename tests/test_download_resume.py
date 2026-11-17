# -*- coding: utf-8 -*-
"""下载续传必须沿用原文件类型、来源和目标目录。"""
import sqlite3

from api.routes import routes_settings


class _Manager:
    def __getattr__(self, name):
        raise AssertionError(f"普通文件续传不应调用模型下载器: {name}")


def test_direct_file_resume_uses_original_target(monkeypatch, tmp_path):
    target = tmp_path / "downloads" / "model.safetensors"
    calls = []

    def fake_direct(url, actual_target, progress_callback=None):
        calls.append((url, actual_target))
        return True

    monkeypatch.setattr("tools.download._download_direct", fake_direct)
    record = {
        "type": "file", "source": "direct", "url": "https://example.com/model",
        "repo_id": "", "filename": target.name, "target_path": str(target),
    }
    assert routes_settings._execute_download_record(record, _Manager(), None) is True
    assert calls == [(record["url"], str(target))]


def test_direct_gguf_still_uses_model_manager(monkeypatch):
    calls = []

    class Manager:
        def download_model(self, url, filename, progress_callback=None, resume=True):
            calls.append((url, filename, resume))
            return True

    record = {
        "type": "model", "source": "direct", "url": "https://example.com/model.gguf",
        "repo_id": "", "filename": "model.gguf", "target_path": "/models/model.gguf",
    }
    assert routes_settings._execute_download_record(record, Manager(), None) is True
    assert calls == [(record["url"], "model.gguf", True)]


def test_progress_is_throttled_but_failure_is_immediate(monkeypatch, tmp_path):
    db_path = str(tmp_path / "chat.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE downloads (id INTEGER PRIMARY KEY, progress REAL, "
        "downloaded_bytes INTEGER, status TEXT, error_message TEXT, "
        "updated_at DATETIME, task_id INTEGER, label TEXT, filename TEXT, target_path TEXT)")
    conn.execute(
        "CREATE TABLE download_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "download_id INTEGER, event_type TEXT, message TEXT, details TEXT, "
        "created_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO downloads (id, progress, status, task_id) VALUES (1, 0, 'downloading', NULL)")
    conn.commit()
    conn.close()

    def connect():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(routes_settings, "db_connect", connect)
    monkeypatch.setattr(routes_settings, "_DOWNLOAD_PROGRESS_WRITE_INTERVAL", 1000)
    routes_settings._download_progress_last_write.clear()

    assert routes_settings.update_download_progress(1, 0.1, status="downloading") is True
    assert routes_settings.update_download_progress(1, 0.2, status="downloading") is False
    assert routes_settings.update_download_progress(
        1, 0.42, downloaded_bytes=4200, status="failed", error_message="timeout") is True

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT progress, downloaded_bytes, status, error_message FROM downloads WHERE id=1").fetchone()
    conn.close()
    assert row == (0.42, 4200, "failed", "timeout")
