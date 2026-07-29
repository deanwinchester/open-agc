# -*- coding: utf-8 -*-
"""大任务检查点恢复机制测试。

覆盖：
- read_task_checkpoint：文件存在返回 dict；不存在返回 None；JSON 损坏/
  非 JSON 对象不抛异常返回 None
- format_checkpoint_notice：有效检查点时注入文本包含 done/total/last_cursor
  与断点续跑指引；无检查点/损坏时返回空串
- get_task_context 恢复注入（task_core 行为级）：构造检查点文件后，恢复快照
  上下文末尾追加检查点提示；快照里历次恢复留下的旧提示被去重（至多一条、
  永远最新读盘结果）；无检查点时上下文原样返回
"""
import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import api.task_core as tc  # noqa: E402

CKPT = {
    "task": "从 MongoDB 导出 180 万条数据",
    "total": 1800000,
    "done": 450000,
    "last_cursor": "507f1f77bcf86cd799439011",
    "phase": "exporting",
    "files_dir": "workspace/mongo_export",
    "updated_at": "2026-07-28T02:00:00",
}


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """把 api.db.DB_PATH 指到临时库。"""
    import api.db as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "test.db"))
    db_mod.init_db()
    return db_mod


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """把 api.task_core.load_config 指到临时 sandbox_dir（隔离真实 workspace）。"""
    sb = tmp_path / "workspace"
    (sb / ".checkpoints").mkdir(parents=True)
    monkeypatch.setattr(tc, "load_config", lambda: {"sandbox_dir": str(sb)})
    return sb


def _write_checkpoint(sandbox, task_id, data):
    path = sandbox / ".checkpoints" / f"task_{task_id}.json"
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _insert_task(db_mod, snapshot=None):
    conn = db_mod.db_connect()
    cur = conn.execute(
        "INSERT INTO tasks (title, user_query, status, session_id) "
        "VALUES (?, ?, 'interrupted', 1)",
        ("导出数据", "从 MongoDB 导出 180 万条数据"))
    tid = cur.lastrowid
    if snapshot is not None:
        conn.execute("UPDATE tasks SET context_snapshot=? WHERE id=?",
                     (json.dumps(snapshot, ensure_ascii=False), tid))
    conn.commit()
    conn.close()
    return tid


# ---------- read_task_checkpoint ----------

class TestReadTaskCheckpoint:
    def test_missing_file_returns_none(self, sandbox):
        assert tc.read_task_checkpoint(999) is None

    def test_valid_checkpoint_returns_dict(self, sandbox):
        _write_checkpoint(sandbox, 7, CKPT)
        data = tc.read_task_checkpoint(7)
        assert data["done"] == 450000
        assert data["total"] == 1800000
        assert data["last_cursor"] == "507f1f77bcf86cd799439011"

    def test_corrupt_json_returns_none_no_raise(self, sandbox):
        _write_checkpoint(sandbox, 7, "{not valid json")
        assert tc.read_task_checkpoint(7) is None

    def test_non_dict_json_returns_none(self, sandbox):
        _write_checkpoint(sandbox, 7, [1, 2, 3])
        assert tc.read_task_checkpoint(7) is None


# ---------- format_checkpoint_notice ----------

class TestFormatCheckpointNotice:
    def test_notice_contains_progress_and_cursor(self, sandbox):
        _write_checkpoint(sandbox, 7, CKPT)
        text = tc.format_checkpoint_notice(7)
        assert text.startswith(tc._CHECKPOINT_NOTICE_PREFIX)
        assert "450000" in text                      # done
        assert "1800000" in text                     # total
        assert "507f1f77bcf86cd799439011" in text    # last_cursor
        assert "last_cursor" in text
        assert "严禁" in text and "从头重跑" in text  # 断点续跑指引

    def test_missing_checkpoint_returns_empty(self, sandbox):
        assert tc.format_checkpoint_notice(999) == ""

    def test_corrupt_checkpoint_returns_empty_no_raise(self, sandbox):
        _write_checkpoint(sandbox, 7, "{{{")
        assert tc.format_checkpoint_notice(7) == ""


# ---------- get_task_context 恢复注入（task_core 行为级）----------

class TestGetTaskContextInjection:
    def _snapshot(self):
        return [
            {"role": "user", "content": "从 MongoDB 导出 180 万条数据"},
            {"role": "assistant", "content": "好的，开始分批导出。"},
        ]

    def test_resume_context_appends_checkpoint_notice(self, tmp_db, sandbox):
        tid = _insert_task(tmp_db, snapshot=self._snapshot())
        _write_checkpoint(sandbox, tid, CKPT)
        ctx = tc.get_task_context(tid)
        assert len(ctx) == 3
        last = ctx[-1]
        assert last["role"] == "user"
        assert last["content"].startswith(tc._CHECKPOINT_NOTICE_PREFIX)
        assert "507f1f77bcf86cd799439011" in last["content"]
        assert "450000" in last["content"] and "1800000" in last["content"]

    def test_no_checkpoint_leaves_context_unchanged(self, tmp_db, sandbox):
        tid = _insert_task(tmp_db, snapshot=self._snapshot())
        ctx = tc.get_task_context(tid)
        assert ctx == self._snapshot()

    def test_stale_notice_deduplicated(self, tmp_db, sandbox):
        """快照里历次恢复留下的旧检查点提示被剔除，只保留最新读盘结果。"""
        old_notice = (tc._CHECKPOINT_NOTICE_PREFIX +
                      "：旧提示，done=100/total=1800000，last_cursor=OLD_CURSOR")
        snap = self._snapshot() + [{"role": "user", "content": old_notice}]
        tid = _insert_task(tmp_db, snapshot=snap)
        _write_checkpoint(sandbox, tid, CKPT)
        ctx = tc.get_task_context(tid)
        notices = [m for m in ctx
                   if m.get("role") == "user"
                   and isinstance(m.get("content"), str)
                   and m["content"].startswith(tc._CHECKPOINT_NOTICE_PREFIX)]
        assert len(notices) == 1
        assert "OLD_CURSOR" not in notices[0]["content"]
        assert "507f1f77bcf86cd799439011" in notices[0]["content"]

    def test_reconstruction_path_also_injects(self, tmp_db, sandbox):
        """无快照时走 task_steps 重建路径，检查点提示同样追加在末尾。"""
        tid = _insert_task(tmp_db)  # 无 context_snapshot
        _write_checkpoint(sandbox, tid, CKPT)
        ctx = tc.get_task_context(tid)
        assert ctx[-1]["role"] == "user"
        assert "507f1f77bcf86cd799439011" in ctx[-1]["content"]

    def test_missing_checkpoint_file_no_raise(self, tmp_db, sandbox):
        """任务无任何检查点文件时注入静默跳过，不影响原上下文。"""
        tid = _insert_task(tmp_db, snapshot=self._snapshot())
        ctx = tc.get_task_context(tid)
        assert all(not m.get("content", "").startswith(tc._CHECKPOINT_NOTICE_PREFIX)
                   if isinstance(m.get("content"), str) else True
                   for m in ctx)
