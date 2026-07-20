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
    import asyncio
    from core.auto_upgrade import AutoUpgrader
    upgrader = AutoUpgrader()
    # perform_upgrade 是同步下载+安装（分钟级），移出事件循环
    success = await asyncio.get_running_loop().run_in_executor(None, upgrader.perform_upgrade)
    if not success:
        raise HTTPException(status_code=500, detail="Upgrade failed")
    return {"status": "ok", "message": "Upgrade completed"}


# ── Logs API ──

@router.get("/api/logs")
async def get_server_logs(lines: int = 200):
    """Return the last N lines of the agent log file."""
    from api.state import _AGENT_LOG_FILE
    log_path = _AGENT_LOG_FILE
    if not log_path or not os.path.exists(log_path):
        return {"lines": [], "total": 0}
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        all_lines = content.split("\n")
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {"lines": tail, "total": len(all_lines)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 200:
        page_size = 50
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
    result = dict(row)
    # Load full request/response from file if stored as paths
    resp_data = result.get("response_data", "")
    if resp_data and "|" in resp_data:
        parts = resp_data.split("|", 1)
        req_path, resp_path = parts[0], parts[1]
        try:
            with open(req_path, "r", encoding="utf-8") as _f:
                result["request_data"] = _f.read()
        except Exception:
            result["request_data"] = "(文件读取失败)"
        try:
            with open(resp_path, "r", encoding="utf-8") as _f:
                result["response_data"] = _f.read()
        except Exception:
            result["response_data"] = "(文件读取失败)"
    return result


# ── Tools Stats API ──

@router.get("/api/tools/stats")
async def get_tool_stats():
    """Return tool usage statistics from tool_frequency.json."""
    from core.paths import get_data_path as _gdp
    freq_path = os.path.join(os.path.dirname(_gdp("config.json")), "tool_frequency.json")
    if not os.path.exists(freq_path):
        return {"tools": [], "summary": {"total_calls": 0, "total_tools": 0}}
    try:
        with open(freq_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"tools": [], "summary": {"total_calls": 0, "total_tools": 0}}
    tools = []
    total_calls = 0
    for name, info in sorted(data.items(), key=lambda x: -x[1].get("calls", 0)):
        info["name"] = name
        total_calls += info.get("calls", 0)
        tools.append(info)
    return {"tools": tools, "summary": {"total_calls": total_calls, "total_tools": len(tools)}}


@router.get("/api/tools/auto-tools")
async def get_auto_tools():
    """Return auto-generated tools list with usage info."""
    import os as _os
    from core.paths import get_data_path as _gdp
    _auto_dir = _gdp("auto_tools")
    # Load frequency data for enrichment
    _freq = {}
    try:
        _fp = _os.path.join(_os.path.dirname(_gdp("config.json")), "tool_frequency.json")
        if _os.path.exists(_fp):
            with open(_fp, "r", encoding="utf-8") as f:
                _freq = json.load(f)
    except Exception:
        pass
    tools = []
    if _os.path.exists(_auto_dir):
        for sess_dir in sorted(_os.listdir(_auto_dir)):
            sess_path = _os.path.join(_auto_dir, sess_dir)
            if not _os.path.isdir(sess_path):
                continue
            for f in sorted(_os.listdir(sess_path)):
                if f.endswith(".py"):
                    name = f.replace(".py", "")
                    freq = _freq.get(name, {})
                    tools.append({
                        "name": name,
                        "session": sess_dir,
                        "calls": freq.get("calls", 0),
                        "sessions": freq.get("sessions", 0),
                        "type": "auto_tool",
                        "last_used": freq.get("last_used", "-"),
                    })
    return {"tools": tools}
