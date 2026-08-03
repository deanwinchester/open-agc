# -*- coding: utf-8 -*-
"""会话窗口功能回归：历史消息返回时间戳（消息时间显示）+
DELETE /api/history/{id} 删除单条记录（用户手动清理）。"""
import asyncio
import os
import sqlite3
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import api.db as db_mod  # noqa: E402
import api.routes.routes_memories as rm  # noqa: E402
from fastapi import HTTPException  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test.db")
    monkeypatch.setattr(db_mod, "DB_PATH", db_file)
    monkeypatch.setattr(rm, "DB_PATH", db_file)
    db_mod.init_db()
    conn = sqlite3.connect(db_file)
    conn.execute("INSERT INTO messages (role, content, session_id) "
                 "VALUES ('user', '你好', 1)")
    conn.execute("INSERT INTO messages (role, content, session_id) "
                 "VALUES ('agent', '你好！有什么可以帮你？', 1)")
    conn.commit()
    conn.close()
    return db_file


class TestHistoryTimestamp:
    def test_history_includes_timestamp(self, tmp_db):
        data = asyncio.run(rm.get_history(session_id=1))
        assert len(data["history"]) == 2
        for m in data["history"]:
            assert m.get("timestamp"), "历史消息必须带 timestamp（消息时间显示）"
            assert "id" in m and "role" in m and "content" in m


class TestDeleteHistoryMessage:
    def test_delete_existing(self, tmp_db):
        conn = sqlite3.connect(tmp_db)
        mid = conn.execute("SELECT id FROM messages LIMIT 1").fetchone()[0]
        conn.close()
        resp = asyncio.run(rm.delete_history_message(mid))
        assert resp["status"] == "success"
        conn = sqlite3.connect(tmp_db)
        left = conn.execute("SELECT count(*) FROM messages WHERE id=?",
                            (mid,)).fetchone()[0]
        conn.close()
        assert left == 0

    def test_delete_missing_404(self, tmp_db):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(rm.delete_history_message(999999))
        assert ei.value.status_code == 404


class TestFrontendContracts:
    def test_message_item_time_and_delete(self):
        src = open(os.path.join(PROJECT_ROOT, "vue-app", "src", "components",
                                "chat", "MessageItem.vue"), encoding="utf-8").read()
        assert "timeText" in src and "msg-time" in src
        assert "msg-del" in src and "canDelete" in src

    def test_chat_view_delete_handler(self):
        src = open(os.path.join(PROJECT_ROOT, "vue-app", "src", "views",
                                "ChatView.vue"), encoding="utf-8").read()
        assert "onDeleteMessage" in src
        assert "/api/history/" in src and "DELETE" in src


# ── 附件（粘贴图片）持久化与视觉上下文重建 ──

class TestMessageAttachments:
    def test_save_and_history_roundtrip(self, tmp_db):
        from api.task_core import save_message
        save_message("user", "看看这张截图", session_id=1,
                     attachments=["uploads/paste_a.png"])
        data = asyncio.run(rm.get_history(session_id=1))
        saved = [m for m in data["history"] if m["content"] == "看看这张截图"]
        assert saved and saved[0]["attachments"] == ["uploads/paste_a.png"]

    def test_history_without_attachments_empty_list(self, tmp_db):
        data = asyncio.run(rm.get_history(session_id=1))
        assert all(m["attachments"] == [] for m in data["history"])

    def test_session_history_rebuilds_multimodal(self, tmp_db, tmp_path, monkeypatch):
        """落盘的粘贴图片在历史重建时恢复为 image_url 内容块（后续轮次可见）。"""
        import api.ws as wsm
        sandbox = tmp_path / "workspace"
        (sandbox / "uploads").mkdir(parents=True)
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d494844520000000100000001080600000"
            "01f15c4890000000d49444154789c626001000000ffff030000060005"
            "57bfabd40000000049454e44ae426082")
        (sandbox / "uploads" / "paste_a.png").write_bytes(png)
        from api.task_core import save_message
        save_message("user", "分析这张图", session_id=1,
                     attachments=["uploads/paste_a.png"])
        monkeypatch.setattr("api.config.load_config",
                            lambda: {"sandbox_dir": str(sandbox)})
        history = wsm._load_session_history(1)
        target = [m for m in history
                  if isinstance(m.get("content"), list)]
        assert target, "带附件的用户消息应重建为多模态内容块"
        blocks = target[0]["content"]
        assert any(b.get("type") == "image_url" for b in blocks)
        assert any(b.get("type") == "text" for b in blocks)

    def test_missing_attachment_file_keeps_text(self, tmp_db, monkeypatch):
        """附件文件已被清理时不报错，退回纯文本消息。"""
        import api.ws as wsm
        from api.task_core import save_message
        save_message("user", "图没了", session_id=1,
                     attachments=["uploads/gone.png"])
        monkeypatch.setattr("api.config.load_config", lambda: {})
        history = wsm._load_session_history(1)
        target = [m for m in history if m.get("content") == "图没了"]
        assert target, "文件缺失时文本消息必须保留"
