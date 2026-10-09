# -*- coding: utf-8 -*-
"""reconcile_downloads（api/server.py 启动对账）回归测试。

生产实证：DB 记录的是 HF 仓库原名（大写），磁盘保留首次写入的小写名，
字符串比对失配 → 启动时补建无 URL 的「待恢复」重复记录、真记录永远卡在
downloading。修复：归一化（normcase+abspath）匹配 + 同名记录去重 +
孤儿 partial 继承同文件名记录的来源信息。
"""
import os
import sqlite3

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    # 运行期导入：模块级导入会让 api.server 在 pytest 收集阶段执行
    # （import 即 load_dotenv 真实 .env + 对真实库跑 reconcile），污染
    # test_core 等靠前用例的环境变量（生产实证：DEEPSEEK_API_KEY 断言挂）
    import api.server as srv
    _srv = srv
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    db_path = str(tmp_path / "chat.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE downloads ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT, label TEXT, repo_id TEXT, "
        "filename TEXT, source TEXT, url TEXT, target_path TEXT, partial_path TEXT, "
        "total_size INTEGER, downloaded_bytes INTEGER, status TEXT, progress REAL, "
        "error_message TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
        "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP, task_id INTEGER, "
        "background_resumed INTEGER DEFAULT 0)")
    conn.execute(
        "CREATE TABLE download_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, download_id INTEGER, event_type TEXT, "
        "message TEXT, details TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(srv, "DB_PATH", db_path)

    class _Mgr:
        pass
    mgr = _Mgr()
    mgr.models_dir = str(models_dir)
    monkeypatch.setattr(
        "core.llamacpp_manager.get_llamacpp_manager", lambda: mgr)
    return models_dir, db_path, _srv


def _row(db_path, rid):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM downloads WHERE id=?", (rid,)).fetchone()
    conn.close()
    return dict(r) if r else None


def _all(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM downloads ORDER BY id")]
    conn.close()
    return rows


def test_case_mismatch_matches_and_pauses(env):
    """DB 大写路径 vs 磁盘小写文件：应匹配并置 paused，不产生重复记录。"""
    models_dir, db_path, srv = env
    # 磁盘是小写名
    partial = models_dir / "ternary-bonsai-2-27b-ptq1_0.gguf.partial"
    partial.write_bytes(b"x" * 1000)
    # DB 记录用大写名（HF 仓库原名）
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO downloads (type, label, repo_id, filename, source, url, "
        "target_path, partial_path, total_size, downloaded_bytes, status, progress) "
        "VALUES ('model','bonsai','prism-ml/x','Ternary-Bonsai-2-27B-PTQ1_0.gguf',"
        "'huggingface','https://hf/x',?, ?, 2000, 0, 'downloading', 0.0)",
        (os.path.join(str(models_dir), "Ternary-Bonsai-2-27B-PTQ1_0.gguf"),
         os.path.join(str(models_dir), "Ternary-Bonsai-2-27B-PTQ1_0.gguf.partial")))
    conn.commit()
    conn.close()

    srv.reconcile_downloads()

    rows = _all(db_path)
    assert len(rows) == 1                      # 无重复记录
    assert rows[0]["status"] == "paused"       # 不再卡 downloading
    assert rows[0]["downloaded_bytes"] == 1000
    assert rows[0]["progress"] == 0.5


def test_dedup_keeps_record_with_url(env):
    """同一 partial 两条记录（待恢复无 url + 正式有 url）→ 删无 url 的。"""
    models_dir, db_path, srv = env
    conn = sqlite3.connect(db_path)
    for label, url in (("a （待恢复）", None), ("a", "https://hf/x")):
        conn.execute(
            "INSERT INTO downloads (type, label, filename, source, url, "
            "target_path, partial_path, total_size, downloaded_bytes, status, progress) "
            "VALUES ('model', ?, 'a.gguf', 'huggingface', ?, 't', 'p', 0, 0, 'paused', 0.0)",
            (label, url))
    conn.execute(
        "INSERT INTO download_events (download_id, event_type, message) VALUES (1, 'started', 'x')")
    conn.commit()
    conn.close()

    srv.reconcile_downloads()

    rows = _all(db_path)
    assert len(rows) == 1
    assert rows[0]["url"] == "https://hf/x"
    conn = sqlite3.connect(db_path)
    n_events = conn.execute("SELECT COUNT(*) FROM download_events").fetchone()[0]
    conn.close()
    assert n_events == 0  # 被删记录的事件一并清理


def test_orphan_partial_inherits_donor_url(env):
    """孤儿 partial 与同目标文件名的历史记录合并（继承 url），不新建死记录。"""
    models_dir, db_path, srv = env
    partial = models_dir / "foo.gguf.partial"
    partial.write_bytes(b"y" * 500)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO downloads (type, label, filename, source, url, "
        "target_path, partial_path, total_size, downloaded_bytes, status, progress) "
        "VALUES ('model', 'foo', 'foo.gguf', 'huggingface', 'https://hf/foo', ?, ?, "
        "1000, 0, 'failed', 0.0)",
        (os.path.join(str(models_dir), "foo.gguf"),
         os.path.join(str(models_dir), "old-dir", "foo.gguf.partial")))
    conn.commit()
    conn.close()

    srv.reconcile_downloads()

    rows = _all(db_path)
    assert len(rows) == 1
    assert rows[0]["status"] == "paused"
    assert rows[0]["downloaded_bytes"] == 500
    assert rows[0]["url"] == "https://hf/foo"
    assert rows[0]["partial_path"] == str(partial)


def test_true_orphan_still_creates_recovery_record(env):
    """完全无历史记录的孤儿 partial → 仍补建「待恢复」记录（行为不变）。"""
    models_dir, db_path, srv = env
    partial = models_dir / "lonely.gguf.partial"
    partial.write_bytes(b"z" * 100)

    srv.reconcile_downloads()

    rows = _all(db_path)
    assert len(rows) == 1
    assert rows[0]["status"] == "paused"
    assert "待恢复" in rows[0]["label"]
