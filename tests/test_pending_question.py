# -*- coding: utf-8 -*-
"""ask_user 待回答问题持久可见回归：此前问题只活在实时进度卡片里，
用户不在聊天页/卡片重建后永远看不到，超时后任务静默转后台（生产实证：
task_354 两次提问用户均未收到）。修复：pending_question 列 +
设置/回答/收官清除 + 任务列表暴露 + 前端双入口（聊天页与任务详情页）。"""
import asyncio
import os
import sqlite3
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import api.db as db_mod  # noqa: E402
import api.task_core as tc  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "t.db")
    monkeypatch.setattr(db_mod, "DB_PATH", db_file)
    monkeypatch.setattr(tc, "DB_PATH", db_file)
    db_mod.init_db()
    conn = sqlite3.connect(db_file)
    conn.execute("INSERT INTO tasks (id, title, user_query, status) "
                 "VALUES (1, 't', 'q', 'running')")
    conn.commit()
    conn.close()
    return db_file


class TestPendingQuestion:
    def test_set_get_clear_roundtrip(self, tmp_db):
        tc.set_pending_question(1, "用哪个邮箱？", ["Gmail", "163"])
        pq = tc.get_pending_question(1)
        assert pq["question"] == "用哪个邮箱？"
        assert pq["options"] == ["Gmail", "163"]
        assert pq["asked_at"]
        tc.clear_pending_question(1)
        assert tc.get_pending_question(1) is None

    def test_task_list_exposes_pending_question(self, tmp_db, monkeypatch):
        import api.routes.routes_tasks as rt
        monkeypatch.setattr(rt, "DB_PATH", tmp_db)
        tc.set_pending_question(1, "继续吗？")
        data = asyncio.run(rt.get_tasks(session_id=None))
        row = [t for t in data["tasks"] if t["id"] == 1][0]
        assert row["pending_question"] and "继续吗" in row["pending_question"]

    def test_agent_sets_pending_on_ask(self):
        """agent.wait_for_user_input 发射提问时必须写 pending_question。"""
        src = open(os.path.join(PROJECT_ROOT, "agent", "agent.py"),
                   encoding="utf-8").read()
        assert "set_pending_question" in src
        assert "clear_pending_question" in src

    def test_completion_clears_pending(self, tmp_db):
        tc.set_pending_question(1, "还继续吗？")
        tc.handle_task_completion(1, "任务完成的结果", [], session_id=1)
        assert tc.get_pending_question(1) is None

    def test_llm_error_failure_clears_pending(self, tmp_db):
        """LLM 失败收官（[LLM_ERROR] 前缀）同样清除 pending_question——
        此前只在成功分支清除，失败任务的详情页永远挂着一张答不了的
        僵尸提问卡片（生产实证）。"""
        tc.set_pending_question(1, "还继续吗？")
        r = tc.handle_task_completion(1, "[LLM_ERROR] 模型服务调用失败。点击「重试」继续。", [], session_id=1)
        assert r == 'failed'
        assert tc.get_pending_question(1) is None

    def test_no_response_failure_clears_pending(self, tmp_db):
        """空响应失败分支同口径清除。"""
        tc.set_pending_question(1, "还继续吗？")
        r = tc.handle_task_completion(1, "", [], session_id=1)
        assert r == 'failed'
        assert tc.get_pending_question(1) is None

    def test_reply_endpoint_clears(self, tmp_db, monkeypatch):
        import api.routes.routes_tasks as rt
        monkeypatch.setattr(rt, "DB_PATH", tmp_db)
        tc.set_pending_question(1, "选一个？")
        # 无 live agent：走 resume_task_with_late_answer（mock 成功）
        monkeypatch.setattr("api.state._background_agents", {})
        monkeypatch.setattr(
            "api.background.resume_task_with_late_answer",
            lambda tid, ans: {"ok": True, "message": "resumed"})
        resp = asyncio.run(rt.reply_to_background_task(1, {"answer": "选 A"}))
        assert resp["status"] == "success"
        assert tc.get_pending_question(1) is None
