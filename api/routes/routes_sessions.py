"""Sessions, Agents, and Models API endpoints."""
import os
import json
import sqlite3
from fastapi import APIRouter, HTTPException
from api.db import DB_PATH
from api.config import load_config, CONFIG_PATH
from core.paths import get_data_path

router = APIRouter()


@router.get("/api/sessions")
async def get_sessions():
    """List all sessions, ordered by most recent first."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT s.*, (SELECT COUNT(*) FROM messages WHERE session_id=s.id) as message_count
        FROM sessions s ORDER BY s.updated_at DESC
    """)
    rows = cursor.fetchall()
    conn.close()
    sessions = []
    for r in rows:
        s = dict(r)
        if s.get("email_password"):
            s["email_password"] = "***"
        sessions.append(s)
    return {"sessions": sessions}


@router.post("/api/sessions")
async def create_session(body: dict = {}):
    """Create a new session, optionally with email config."""
    name = body.get("name", None)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    if not name:
        cursor.execute("SELECT COUNT(*) FROM sessions")
        count = cursor.fetchone()[0] + 1
        name = f"会话 {count}"
    fields = ["name"]
    values = [name]
    email_fields = ["email_enabled", "email_account", "email_password",
                    "email_imap_server", "email_smtp_server", "owner_email"]
    for f in email_fields:
        if f in body:
            fields.append(f)
            values.append(body[f])
    placeholders = ",".join("?" * len(fields))
    sql = f"INSERT INTO sessions ({','.join(fields)}) VALUES ({placeholders})"
    cursor.execute(sql, values)
    session_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return {"session": {"id": session_id, "name": name}}


def _cascade_cleanup_session(session_id: int):
    """Clean up all session-associated data from memory, KG, trajectories, and auto-tools."""
    try:
        mem_conn = sqlite3.connect(get_data_path("memory.db"))
        mem_conn.execute("DELETE FROM memories WHERE session_id=?", (session_id,))
        mem_conn.commit()
        mem_conn.close()
    except Exception as e:
        print(f"[CleanupSession] Memory error: {e}")
    try:
        kg_conn = sqlite3.connect(get_data_path("agent.db"))
        kg_conn.execute(
            "DELETE FROM kg_relations WHERE source_id IN "
            "(SELECT id FROM kg_entities WHERE session_id=?)", (session_id,))
        kg_conn.execute("DELETE FROM kg_entities WHERE session_id=?", (session_id,))
        kg_conn.execute("DELETE FROM task_trajectories WHERE session_id=?", (session_id,))
        kg_conn.commit()
        kg_conn.close()
    except Exception as e:
        print(f"[CleanupSession] KG error: {e}")
    try:
        import shutil
        auto_tools_dir = get_data_path(f"auto_tools/{session_id}")
        if os.path.exists(auto_tools_dir):
            shutil.rmtree(auto_tools_dir)
    except Exception as e:
        print(f"[CleanupSession] Auto-tools error: {e}")


@router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: int):
    """Delete a session and all associated data. Default session (id=1) cannot be deleted."""
    if session_id == 1:
        raise HTTPException(status_code=403, detail="默认会话不可删除，只能强制清空数据。请使用 clear 端点。")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
    cursor.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    conn.commit()
    conn.close()
    _cascade_cleanup_session(session_id)
    return {"ok": True}


@router.post("/api/sessions/{session_id}/clear")
async def clear_session(session_id: int):
    """Clear all data for a session without deleting the session itself."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
    conn.execute("UPDATE sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (session_id,))
    conn.commit()
    conn.close()
    _cascade_cleanup_session(session_id)
    return {"ok": True}


@router.put("/api/sessions/{session_id}")
async def update_session(session_id: int, body: dict = {}):
    """Update a session's name and/or email config."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    updates = []
    params = []
    if "name" in body:
        updates.append("name=?")
        params.append(body["name"])
    email_fields = ["email_enabled", "email_account", "email_password",
                    "email_imap_server", "email_smtp_server", "owner_email"]
    for f in email_fields:
        if f in body:
            updates.append(f"{f}=?")
            params.append(body[f])
    if updates:
        updates.append("updated_at=CURRENT_TIMESTAMP")
        params.append(session_id)
        sql = f"UPDATE sessions SET {','.join(updates)} WHERE id=?"
        cursor.execute(sql, params)
    conn.commit()
    conn.close()
    return {"ok": True}


# ── Agent Profiles ──

def _load_agents():
    config = load_config()
    raw = config.get("agent_profiles", [])
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return []
    return list(raw) if isinstance(raw, list) else []


def _save_agents(agents: list):
    config = load_config()
    config["agent_profiles"] = agents
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


@router.get("/api/agents")
async def get_agents():
    return {"agents": _load_agents()}


@router.post("/api/agents")
async def create_agent(body: dict):
    agents = _load_agents()
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Agent name is required")
    if any(a.get("name") == name for a in agents):
        raise HTTPException(status_code=400, detail="Agent name already exists")
    agents.append({
        "name": name,
        "prompt": body.get("prompt", ""),
        "model": body.get("model", ""),
        "temperature": body.get("temperature", 0.7),
        "max_tokens": body.get("max_tokens", 4096),
    })
    _save_agents(agents)
    return {"agents": agents}


@router.put("/api/agents/{agent_name}")
async def update_agent(agent_name: str, body: dict):
    agents = _load_agents()
    for a in agents:
        if a.get("name") == agent_name:
            a.update({k: v for k, v in body.items() if k != "name"})
            _save_agents(agents)
            return {"agents": agents}
    raise HTTPException(status_code=404, detail="Agent not found")


@router.delete("/api/agents/{agent_name}")
async def delete_agent(agent_name: str):
    agents = _load_agents()
    agents = [a for a in agents if a.get("name") != agent_name]
    _save_agents(agents)
    return {"agents": agents}


# ── Models API ──

@router.get("/api/models/available")
async def get_available_models():
    """Return available models from config + local inference servers."""
    config = load_config()
    models = []
    if config.get("default_model"):
        models.append(config["default_model"])
    for fb in config.get("fallback_models", []):
        if fb not in models:
            models.append(fb)
    try:
        from core.llamacpp_manager import get_llamacpp_manager
        lm = get_llamacpp_manager()
        for m in lm.list_models():
            mname = f"llamacpp/{m}" if not m.startswith("llamacpp/") else m
            if mname not in models:
                models.append(mname)
    except Exception:
        pass
    for cm in config.get("models", []):
        if cm not in models:
            models.append(cm)
    return {"models": models}
