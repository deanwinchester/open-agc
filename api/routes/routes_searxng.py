"""SearXNG, Version, Upgrade, Logs, Tools API endpoints."""
import os
import json
import sqlite3
from datetime import datetime
from fastapi import APIRouter, HTTPException
from typing import Optional
from core.paths import get_data_path
from core.version import get_version
from core.searxng_manager import get_searxng_manager
from api.db import DB_PATH
from api.config import load_config, log_agent_error

router = APIRouter()


# ── SearXNG API ──

@router.get("/api/searxng/status")
async def get_searxng_status():
    sm = get_searxng_manager()
    return sm.get_status()


@router.post("/api/searxng/install")
async def install_searxng():
    sm = get_searxng_manager()
    ok = sm.install()
    if not ok:
        raise HTTPException(status_code=500, detail="Installation failed")
    return {"status": "ok", "message": "SearXNG installed"}


@router.post("/api/searxng/control")
async def control_searxng(body: dict):
    sm = get_searxng_manager()
    action = body.get("action", "")
    if action == "start":
        sm.start()
    elif action == "stop":
        sm.stop()
    else:
        raise HTTPException(status_code=400, detail="Invalid action")
    return {"status": "ok"}


# ── Version / Upgrade API ──

@router.get("/api/version")
async def get_api_version():
    from core.auto_upgrade import AutoUpgrader
    upgrader = AutoUpgrader()
    current = get_version()
    latest = upgrader.fetch_latest_release()
    return {
        "current": current,
        "latest": latest or current,
        "update_available": bool(latest and latest != current),
    }


@router.post("/api/upgrade")
async def upgrade_server():
    from core.auto_upgrade import AutoUpgrader
    upgrader = AutoUpgrader()
    success = upgrader.perform_upgrade()
    if not success:
        raise HTTPException(status_code=500, detail="Upgrade failed")
    return {"status": "ok", "message": "Upgrade completed"}


# ── Logs API ──

@router.get("/api/logs")
async def get_server_logs(lines: int = 100):
    """Read the last N lines of server.log."""
    log_path = os.path.join(get_data_path("logs"), "server.log")
    if not os.path.exists(log_path):
        return {"lines": []}
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
    return {"lines": all_lines[-lines:]}


# ── Model Call Logs API ──

@router.get("/api/model-logs/status")
async def get_model_logging_status():
    from api.config import load_config
    cfg = load_config()
    enabled = cfg.get("model_logging_enabled", False)
    return {"enabled": enabled}


@router.post("/api/model-logs/toggle")
async def toggle_model_logging(body: dict = {}):
    from api.config import load_config, save_config
    cfg = load_config()
    enabled = body.get("enabled", None)
    if enabled is not None:
        cfg["model_logging_enabled"] = enabled
        save_config(cfg)
    return {"enabled": cfg.get("model_logging_enabled", False)}


@router.post("/api/model-logs/clear")
async def clear_model_logs():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM model_call_logs")
    conn.commit()
    conn.close()
    return {"status": "ok"}


@router.get("/api/model-logs/filters")
async def get_model_log_filters():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    providers = [r[0] for r in conn.execute("SELECT DISTINCT provider FROM model_call_logs ORDER BY provider").fetchall()]
    models = [r[0] for r in conn.execute("SELECT DISTINCT model FROM model_call_logs ORDER BY model").fetchall()]
    conn.close()
    return {"providers": providers, "models": models}


@router.get("/api/model-logs")
async def get_model_logs(page: int = 1, page_size: int = 50, provider: str = None,
                         model: str = None, session_id: int = None):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    where = []
    params = []
    if provider:
        where.append("provider=?")
        params.append(provider)
    if model:
        where.append("model=?")
        params.append(model)
    if session_id:
        where.append("session_id=?")
        params.append(session_id)
    where_clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM model_call_logs{where_clause}", params).fetchone()[0]
    offset = (page - 1) * page_size
    rows = conn.execute(
        f"SELECT * FROM model_call_logs{where_clause} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [page_size, offset]
    ).fetchall()
    conn.close()
    return {"logs": [dict(r) for r in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/api/model-logs/{log_id}")
async def get_model_log_detail(log_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM model_call_logs WHERE id=?", (log_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Log not found")
    return dict(row)


# ── Tools Stats API ──

@router.get("/api/tools/stats")
async def get_tool_stats():
    """Get tool usage statistics."""
    try:
        from core.stats_manager import get_stats_manager
        sm = get_stats_manager()
        tools = sm.get_tool_stats() if hasattr(sm, 'get_tool_stats') else []
    except Exception:
        tools = []
    return {"tools": tools}


@router.get("/api/tools/auto-tools")
async def get_auto_tools():
    """List auto-generated tools with usage stats."""
    import os as _os
    from core.paths import get_data_path as _gdp
    tools_dir = _os.path.join(_gdp("auto_tools"), "1")
    tools = []
    if _os.path.exists(tools_dir):
        for f in sorted(_os.listdir(tools_dir)):
            if f.endswith(".py"):
                name = f.replace(".py", "")
                tools.append({
                    "name": name,
                    "session": "1",
                    "calls": 0,
                    "sessions": 0,
                    "type": "auto_tool",
                    "last_used": "-",
                })
    return {"tools": tools}
