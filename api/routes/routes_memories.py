"""Memories and history API endpoints."""
import sqlite3
from fastapi import APIRouter
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
    sql = "SELECT id, role, content FROM messages WHERE {} ORDER BY id DESC LIMIT ?".format(" AND ".join(where))
    cursor.execute(sql, params + [limit])
    rows = cursor.fetchall()
    history = [{"id": r["id"], "role": r["role"], "content": r["content"]} for r in reversed(rows)]
    conn.close()
    oldest_id = history[0]["id"] if history else 0
    has_more = oldest_id > 1
    return {"history": history, "oldest_id": oldest_id, "has_more": has_more}
