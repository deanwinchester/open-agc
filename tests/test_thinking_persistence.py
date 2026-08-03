# -*- coding: utf-8 -*-
"""思考内容持久化回归：reasoning_content 此前只实时流转到前端、从不写入
task_steps（thinking_content 列存在但无人写入），刷新页面后思考消失。
修复：thinking 事件缓冲 → 下一个 tool_start 落库时随步骤写入；
前端历史卡片把 thinking_content 渲染为 thinking 条目。"""
import os
import sqlite3
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    import api.db as db_mod
    import api.task_core as tc
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


class TestThinkingPersistence:
    def test_add_task_step_stores_thinking(self, tmp_db):
        from api.task_core import add_task_step
        add_task_step(1, 1, "execute_shell", thinking_content="我先分析一下需求")
        conn = sqlite3.connect(tmp_db)
        row = conn.execute(
            "SELECT thinking_content FROM task_steps WHERE task_id=1").fetchone()
        conn.close()
        assert row and row[0] == "我先分析一下需求"

    def test_ws_handler_buffers_and_attaches(self):
        """ws.py 进度处理：thinking 事件缓存 + tool_start 携带 thinking_content。"""
        src = open(os.path.join(PROJECT_ROOT, "api", "ws.py"),
                   encoding="utf-8").read()
        assert '_pending_thinking' in src
        assert 'event.get("event") == "thinking"' in src
        assert 'thinking_content=_pending_thinking["content"]' in src

    def test_background_handler_attaches(self):
        src = open(os.path.join(PROJECT_ROOT, "api", "background.py"),
                   encoding="utf-8").read()
        assert "_hb_thinking" in src
        assert "thinking_content=_hb_thinking[\"content\"]" in src

    def test_history_card_renders_thinking(self):
        src = open(os.path.join(PROJECT_ROOT, "vue-app", "src", "views",
                                "ChatView.vue"), encoding="utf-8").read()
        assert "s.thinking_content" in src
        assert "kind: 'thinking'" in src
