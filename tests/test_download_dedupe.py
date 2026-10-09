# -*- coding: utf-8 -*-
"""queue_download 防重复回归测试：大小写不敏感 + 磁盘兜底（模型目录/沙箱 downloads）。

生产实证：同一模型先以某文件名下载中断（partial 残留），agent 换路径重新全量
下载一遍——DB 精确匹配大小写失配没拦住，磁盘上已有文件也没拦住。
"""
import os
import sqlite3

import pytest

from tools.download import DownloadTool


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "chat_history.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE downloads (id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT, "
        "label TEXT, repo_id TEXT, filename TEXT, source TEXT, url TEXT, "
        "target_path TEXT, partial_path TEXT, total_size INTEGER, "
        "downloaded_bytes INTEGER, status TEXT, progress REAL)")
    conn.commit()
    conn.close()
    monkeypatch.setattr("core.paths.get_data_path", lambda name: db_path)
    # 让磁盘兜底拿不到管理器/沙箱（保持默认，测试里单独注入）
    return db_path, tmp_path


def _insert(db_path, filename, status):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO downloads (filename, status) VALUES (?, ?)", (filename, status))
    conn.commit()
    conn.close()


def test_case_insensitive_completed_short_circuits(env):
    db_path, _ = env
    _insert(db_path, "Ternary-Bonsai-2-27B-PTQ1_0.gguf", "completed")
    out = DownloadTool().execute(repo_id="x/y", filename="ternary-bonsai-2-27b-ptq1_0.gguf")
    assert "already completed" in out


def test_case_insensitive_paused_suggests_resume(env):
    db_path, _ = env
    _insert(db_path, "Ternary-Bonsai-2-27B-PTQ1_0.gguf", "paused")
    out = DownloadTool().execute(repo_id="x/y", filename="ternary-bonsai-2-27b-ptq1_0.gguf")
    assert "resumed" in out


def test_existing_file_on_disk_short_circuits(env, monkeypatch):
    """DB 无记录但文件已在模型目录 → 不重复下载。"""
    db_path, tmp_path = env
    models = tmp_path / "models"
    models.mkdir()
    (models / "mymodel.gguf").write_bytes(b"done")

    class _M:
        models_dir = str(models)

    monkeypatch.setattr("core.llamacpp_manager.get_llamacpp_manager", lambda: _M())
    monkeypatch.setattr("core.paths.resolve_sandbox_dir",
                        lambda _c=None: str(tmp_path / "no_sandbox"))
    out = DownloadTool().execute(repo_id="x/y", filename="MyModel.gguf")
    assert "无需重复下载" in out


def test_partial_on_disk_suggests_resume(env, monkeypatch):
    db_path, tmp_path = env
    models = tmp_path / "models"
    models.mkdir()
    (models / "mymodel.gguf.partial").write_bytes(b"x" * 1024)

    class _M:
        models_dir = str(models)

    monkeypatch.setattr("core.llamacpp_manager.get_llamacpp_manager", lambda: _M())
    monkeypatch.setattr("core.paths.resolve_sandbox_dir",
                        lambda _c=None: str(tmp_path / "no_sandbox"))
    out = DownloadTool().execute(repo_id="x/y", filename="MyModel.gguf")
    assert "续传" in out


# ── 管理动作（action=list/resume/delete，与下载页同一数据源）──

@pytest.fixture()
def mdb(tmp_path, monkeypatch):
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


def _minsert(db_path, label, status, progress=0.0, partial=""):
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


def test_list_empty(mdb):
    assert "没有记录" in DownloadTool().execute(action="list")


def test_list_shows_status_and_size(mdb):
    _minsert(mdb, "mymodel.gguf", "paused", progress=0.355)
    out = DownloadTool().execute(action="list")
    assert "paused" in out and "mymodel.gguf" in out and "36%" in out


def test_resume_requires_paused_or_failed(mdb):
    rid = _minsert(mdb, "x.gguf", "completed")
    out = DownloadTool().execute(action="resume", download_id=rid)
    assert "续传失败" in out and "completed" in out


def test_resume_nonexistent(mdb):
    out = DownloadTool().execute(action="resume", download_id=999)
    assert "续传失败" in out


def test_delete_removes_record(mdb):
    rid = _minsert(mdb, "gone.gguf", "failed")
    out = DownloadTool().execute(action="delete", download_id=rid)
    assert "已删除" in out
    conn = sqlite3.connect(mdb)
    n = conn.execute("SELECT COUNT(*) FROM downloads WHERE id=?", (rid,)).fetchone()[0]
    conn.close()
    assert n == 0


def test_unknown_action(mdb):
    assert "未知 action" in DownloadTool().execute(action="hack")
