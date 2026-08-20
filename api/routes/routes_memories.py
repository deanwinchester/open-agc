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
    msg_ids = [r["id"] for r in rows]
    # 联查用户反馈（M3）：一消息一条，message_id 主键
    feedback_map = {}
    if msg_ids:
        ph = ",".join("?" * len(msg_ids))
        try:
            for fr in cursor.execute(
                    f"SELECT message_id, score FROM message_feedback WHERE message_id IN ({ph})",
                    msg_ids).fetchall():
                feedback_map[fr[0]] = fr[1]
        except Exception:
            pass  # 表不存在（旧库）时静默降级
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
                        "timestamp": r["timestamp"], "attachments": atts,
                        "feedback": feedback_map.get(r["id"], 0)})
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


# ── 用户反馈（M3 测评指标：好评率） ──

@router.post("/api/feedback")
async def post_feedback(payload: dict):
    """保存/更新一条消息的用户反馈：score=1 好评 / -1 差评 / 0 撤销。"""
    message_id = payload.get("message_id")
    score = payload.get("score", 0)
    comment = str(payload.get("comment", "") or "")[:500]
    if not isinstance(message_id, int) or message_id <= 0:
        raise HTTPException(status_code=400, detail="message_id 无效")
    if score not in (-1, 0, 1):
        raise HTTPException(status_code=400, detail="score 只能是 -1/0/1")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    msg = conn.execute(
        "SELECT session_id, task_id FROM messages WHERE id=?", (message_id,)).fetchone()
    if not msg:
        conn.close()
        raise HTTPException(status_code=404, detail="消息不存在")
    if score == 0:
        conn.execute("DELETE FROM message_feedback WHERE message_id=?", (message_id,))
    else:
        conn.execute(
            """INSERT INTO message_feedback (message_id, score, comment, session_id, task_id, updated_at)
               VALUES (?,?,?,?,?, CURRENT_TIMESTAMP)
               ON CONFLICT(message_id) DO UPDATE SET
                 score=excluded.score, comment=excluded.comment,
                 updated_at=CURRENT_TIMESTAMP""",
            (message_id, score, comment, msg["session_id"], msg["task_id"]))
    conn.commit()
    conn.close()
    return {"status": "success", "message_id": message_id, "score": score}


@router.get("/api/feedback/stats")
async def feedback_stats(days: int = 7):
    """好评率聚合（默认近 7 天）：总数/好评/差评/好评率 + 按会话分布。"""
    days = max(1, min(days, 90))
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """SELECT COUNT(*) total,
                      SUM(CASE WHEN score=1 THEN 1 ELSE 0 END) good,
                      SUM(CASE WHEN score=-1 THEN 1 ELSE 0 END) bad
               FROM message_feedback
               WHERE updated_at >= datetime('now', ?)""",
            (f"-{days} days",)).fetchone()
        by_session = conn.execute(
            """SELECT session_id,
                      SUM(CASE WHEN score=1 THEN 1 ELSE 0 END) good,
                      SUM(CASE WHEN score=-1 THEN 1 ELSE 0 END) bad
               FROM message_feedback
               WHERE updated_at >= datetime('now', ?)
               GROUP BY session_id ORDER BY (good+bad) DESC LIMIT 20""",
            (f"-{days} days",)).fetchall()
    except Exception:
        conn.close()
        return {"days": days, "total": 0, "good": 0, "bad": 0,
                "good_rate": 0, "by_session": []}
    conn.close()
    total = row["total"] or 0
    good = row["good"] or 0
    bad = row["bad"] or 0
    return {
        "days": days, "total": total, "good": good, "bad": bad,
        "good_rate": round(good / total * 100, 1) if total else 0,
        "by_session": [dict(r) for r in by_session],
    }
