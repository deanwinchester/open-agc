"""Memories and history API endpoints."""
import json
import sqlite3
from fastapi import APIRouter, HTTPException
from api.db import DB_PATH

router = APIRouter()


@router.get("/api/memories")
async def get_memories(category: str = None, query: str = None):
    """Search or list memories."""
    from core.memory_store import MemoryStore
    from core.paths import get_data_path
    store = MemoryStore(db_path=get_data_path("memory.db"))
    if query:
        results = store.search_memories(query, top_k=10, category=category)
        return {"memories": results, "type": "search"}
    else:
        results = store.get_all_memories(category=category, limit=50)
        return {"memories": results, "type": "all"}


@router.get("/api/memories/categories")
async def get_memory_categories():
    """Get memory category summary."""
    from core.memory_store import MemoryStore
    from core.paths import get_data_path
    store = MemoryStore(db_path=get_data_path("memory.db"))
    return {"categories": store.get_categories_summary()}


@router.get("/api/history")
async def get_history(session_id: int = None, before_id: int = 0, limit: int = 100):
    """Retrieve chat history with pagination."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    params = []
    where = ["session_id=?", "role != 'tool_step'"] if session_id else ["role != 'tool_step'"]
    if session_id:
        params.append(session_id)
    if before_id > 0:
        where.append("id < ?")
        params.append(before_id)
    sql = "SELECT id, role, content, timestamp, attachments FROM messages WHERE {} ORDER BY id DESC LIMIT ?".format(" AND ".join(where))
    cursor.execute(sql, params + [limit])
    rows = cursor.fetchall()
    history = []
    for r in reversed(rows):
        atts = []
        if r["attachments"]:
            try:
                parsed = json.loads(r["attachments"])
                if isinstance(parsed, list):
                    atts = [a for a in parsed if isinstance(a, str)]
            except Exception:
                atts = []
        history.append({"id": r["id"], "role": r["role"], "content": r["content"],
                        "timestamp": r["timestamp"], "attachments": atts})
    oldest_id = history[0]["id"] if history else 0
    has_more = False
    if oldest_id:
        if session_id:
            has_more = bool(cursor.execute(
                "SELECT EXISTS(SELECT 1 FROM messages WHERE session_id=? AND id < ?)",
                (session_id, oldest_id)
            ).fetchone()[0])
        else:
            has_more = bool(cursor.execute(
                "SELECT EXISTS(SELECT 1 FROM messages WHERE id < ?)",
                (oldest_id,)
            ).fetchone()[0])
    conn.close()
    return {"history": history, "oldest_id": oldest_id, "has_more": has_more}


@router.delete("/api/history/{message_id}")
async def delete_history_message(message_id: int):
    """删除会话中的单条消息记录（用户手动清理用）。

    只影响聊天展示层（messages 表）；关联的 task_steps/任务记录不动。
    不存在返回 404。"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("DELETE FROM messages WHERE id=?", (message_id,))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    if not deleted:
        raise HTTPException(status_code=404, detail="消息不存在")
    return {"status": "success", "deleted": message_id}
