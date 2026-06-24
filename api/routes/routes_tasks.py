"""Tasks and Processes API endpoints."""
import os
import json
import re
import sqlite3
import asyncio
import time as _time
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.db import DB_PATH
from api.config import load_config
from api.state import _active_agents, _background_agents, connected_websockets, _broadcast_to_websockets
from api.task_core import (
    create_task, update_task_status, update_task_type, get_task_context,
    save_task_context, add_task_step, _extract_task_title,
    _record_task_deliverables, increment_task_resume, _check_goal_completeness,
)
from tools.shell import interrupt_shell, get_background_processes, get_orphan_processes, cleanup_background_process, adopt_orphan_processes

router = APIRouter()


@router.get("/api/tasks")
async def get_tasks(status: str = None, q: str = None, session_id: int = None,
                    page: int = 1, page_size: int = 50):
    """List tasks with optional status filter, search, and pagination."""
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 200:
        page_size = 50
    offset = (page - 1) * page_size
    conn = sqlite3.connect(DB_PATH, timeout=2)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=2000")
    columns = ("t.id, t.title, t.user_query, t.status, t.task_type, "
               "t.created_at, t.updated_at, t.result_summary, "
               "t.session_id, t.schedule_cron, t.schedule_enabled, "
               "t.next_run_at, t.resume_count, "
               "t.total_tokens, t.total_cost, "
               "t.prompt_tokens, t.completion_tokens, t.cached_tokens")
    conditions = []
    params = []
    if status and status != 'all':
        if status == 'scheduled':
            conditions.append("t.task_type = 'scheduled'")
        else:
            conditions.append("t.status = ?")
            params.append(status)
    if q:
        conditions.append("(t.title LIKE ? OR t.user_query LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    if session_id is not None:
        conditions.append("t.session_id = ?")
        params.append(session_id)
    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
    t0 = _time.time()
    total_count = conn.execute("SELECT COUNT(*) FROM tasks t" + where_clause, params).fetchone()[0]
    query = ("SELECT " + columns + ", sess.name as session_name, "
             "(SELECT COUNT(*) FROM task_steps WHERE task_id = t.id) as step_count "
             "FROM tasks t LEFT JOIN sessions sess ON sess.id = t.session_id" +
             where_clause + " ORDER BY t.created_at DESC LIMIT ? OFFSET ?")
    rows = conn.execute(query, params + [page_size, offset]).fetchall()
    conn.close()
    t2 = _time.time()
    tasks = []
    for row in rows:
        tasks.append({
            "id": row["id"], "title": row["title"], "user_query": row["user_query"],
            "status": row["status"], "task_type": row["task_type"] if "task_type" in row.keys() else "oneshot",
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "result_summary": row["result_summary"],
            "step_count": row["step_count"],
            "session_id": row["session_id"] if "session_id" in row.keys() else None,
            "session_name": row["session_name"] if "session_name" in row.keys() else None,
            "schedule_cron": row["schedule_cron"] if "schedule_cron" in row.keys() else None,
            "schedule_enabled": bool(row["schedule_enabled"]) if "schedule_enabled" in row.keys() else False,
            "next_run_at": row["next_run_at"] if "next_run_at" in row.keys() else None,
            "resume_count": row["resume_count"] if "resume_count" in row.keys() else 0,
            "total_tokens": row["total_tokens"] if "total_tokens" in row.keys() else 0,
            "total_cost": row["total_cost"] if "total_cost" in row.keys() else 0.0,
            "prompt_tokens": row["prompt_tokens"] if "prompt_tokens" in row.keys() else 0,
            "completion_tokens": row["completion_tokens"] if "completion_tokens" in row.keys() else 0,
            "cached_tokens": row["cached_tokens"] if "cached_tokens" in row.keys() else 0,
        })
    return {"tasks": tasks, "total_count": total_count, "page": page, "page_size": page_size}


@router.get("/api/tasks/{task_id}")
async def get_task_detail(task_id: int):
    """Get task detail with all steps."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Task not found")
    steps = conn.execute(
        "SELECT * FROM task_steps WHERE task_id=? ORDER BY created_at ASC", (task_id,)
    ).fetchall()
    conn.close()
    task = dict(row)
    if task.get("output_files"):
        try:
            task["output_files"] = json.loads(task["output_files"])
        except Exception:
            task["output_files"] = []
    task["steps"] = [dict(s) for s in steps]
    task["total_tokens"] = task.get("total_tokens", 0)
    task["total_cost"] = task.get("total_cost", 0.0)
    task["prompt_tokens"] = task.get("prompt_tokens", 0)
    task["completion_tokens"] = task.get("completion_tokens", 0)
    task["cached_tokens"] = task.get("cached_tokens", 0)
    return {"task": task}


@router.get("/api/tasks/{task_id}/steps")
async def get_task_steps(task_id: int, page: int = 1, page_size: int = 50):
    """Get paginated task steps."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    total = conn.execute("SELECT COUNT(*) FROM task_steps WHERE task_id=?", (task_id,)).fetchone()[0]
    pages = max(1, (total + page_size - 1) // page_size)
    offset = (page - 1) * page_size
    rows = conn.execute(
        "SELECT * FROM task_steps WHERE task_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (task_id, page_size, offset)
    ).fetchall()
    conn.close()
    return {"steps": [dict(s) for s in rows], "total": total, "page": page, "page_size": page_size, "total_pages": pages}


@router.post("/api/tasks/{task_id}/interrupt")
async def interrupt_task(task_id: int):
    """Mark a task as interrupted by user and stop its agent."""
    from api.state import _agent_log_file
    for _agents in _active_agents.values():
        for _aid, _a in list(_agents.items()):
            if _aid == task_id or _aid == 0:
                _a.is_interrupted = True
    for _tid, _bg_a in list(_background_agents.items()):
        if _tid == task_id:
            _bg_a.is_interrupted = True
    interrupt_shell()
    _llamacpp = globals().get('_llamacpp_download_state')
    if _llamacpp:
        _llamacpp["cancelled"] = True
    update_task_status(task_id, "interrupted", interruption_reason="user")
    return {"status": "success", "message": "Task marked as interrupted"}


@router.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int):
    """Delete a task and its associated data."""
    # Interrupt running agents
    for _agents in _active_agents.values():
        for _aid, _a in list(_agents.items()):
            if _aid == task_id or _aid == 0:
                try:
                    _a.is_interrupted = True
                except Exception:
                    pass
    for _tid, _bg_a in list(_background_agents.items()):
        if _tid == task_id:
            try:
                _bg_a.is_interrupted = True
            except Exception:
                pass
    interrupt_shell()
    # Collect and remove temp files
    try:
        conn_tmp = sqlite3.connect(DB_PATH)
        conn_tmp.row_factory = sqlite3.Row
        steps = conn_tmp.execute(
            "SELECT generated_files FROM task_steps WHERE task_id=?", (task_id,)
        ).fetchall()
        for s in steps:
            gf = s["generated_files"]
            if gf:
                try:
                    parsed = json.loads(gf) if isinstance(gf, str) else gf
                    if isinstance(parsed, list):
                        for f in parsed:
                            fpath = f.get("path", "") if isinstance(f, dict) else f
                            if fpath and f.get("type") == "temp":
                                try:
                                    if os.path.exists(fpath):
                                        os.remove(fpath)
                                except Exception:
                                    pass
                except Exception:
                    pass
        conn_tmp.execute("DELETE FROM task_steps WHERE task_id=?", (task_id,))
        conn_tmp.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        conn_tmp.commit()
        conn_tmp.close()
    except Exception as e:
        print(f"[Task] Delete error: {e}")
    # Clean up goal association
    try:
        from tools.task_plan import load_goals as _lg, save_goals as _sg
        _goals = _lg()
        _changed = False
        for item in _goals.get("items", []):
            tids = item.get("task_ids", [])
            if task_id in tids:
                item["task_ids"] = [t for t in tids if t != task_id]
                _changed = True
        if _changed:
            _sg(_goals)
    except Exception:
        pass
    return {"status": "success", "message": "Task deleted"}


@router.post("/api/tasks/{task_id}/reset-resume")
async def reset_task_resume(task_id: int):
    """Reset a task's resume_count to 0."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE tasks SET resume_count=0, updated_at=CURRENT_TIMESTAMP WHERE id=?", (task_id,))
    conn.commit()
    conn.close()
    return {"status": "success"}


@router.post("/api/tasks/{task_id}/complete")
async def complete_task(task_id: int):
    """Manually mark a task as completed."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE tasks SET status='completed', updated_at=CURRENT_TIMESTAMP WHERE id=?", (task_id,))
    conn.commit()
    conn.close()
    return {"status": "success"}


# ── Schedule ──

class ScheduleTaskRequest(BaseModel):
    title: str
    query: str
    cron: str
    session_id: int = 1


@router.post("/api/tasks/schedule")
async def create_scheduled_task(req: ScheduleTaskRequest):
    """Create a scheduled task."""
    try:
        from croniter import croniter
        croniter(req.cron)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid cron expression")
    task_id = create_task(
        title=req.title, user_query=req.query, task_type='scheduled',
        schedule_cron=req.cron, schedule_enabled=True, session_id=req.session_id
    )
    return {"status": "success", "task_id": task_id}


@router.post("/api/tasks/{task_id}/toggle-schedule")
async def toggle_schedule(task_id: int):
    """Toggle a scheduled task on/off."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT schedule_enabled FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Task not found")
    enabled = 0 if row[0] else 1
    if enabled:
        try:
            from croniter import croniter
            from datetime import datetime as _dt
            cron = conn.execute("SELECT schedule_cron FROM tasks WHERE id=?", (task_id,)).fetchone()
            if cron and cron[0]:
                next_run = croniter(cron[0], _dt.now()).get_next(_dt).strftime('%Y-%m-%d %H:%M:%S')
                conn.execute("UPDATE tasks SET schedule_enabled=?, next_run_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                             (enabled, next_run, task_id))
            else:
                conn.execute("UPDATE tasks SET schedule_enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                             (enabled, task_id))
        except Exception:
            conn.execute("UPDATE tasks SET schedule_enabled=?, status='paused' WHERE id=?", (0, task_id))
    else:
        conn.execute("UPDATE tasks SET schedule_enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (enabled, task_id))
    conn.commit()
    conn.close()
    return {"status": "success", "enabled": bool(enabled)}


@router.put("/api/tasks/{task_id}/schedule")
async def update_schedule(task_id: int, req: ScheduleTaskRequest):
    """Update a scheduled task's config."""
    try:
        from croniter import croniter
        croniter(req.cron)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid cron expression")
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE tasks SET title=?, user_query=?, schedule_cron=?, "
        "updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (req.title, req.query, req.cron, task_id)
    )
    conn.commit()
    conn.close()
    return {"status": "success"}


# ── Process Management ──

@router.get("/api/processes")
async def list_processes():
    """List all running background shell processes (including orphans)."""
    procs = get_background_processes()
    orphans = get_orphan_processes()
    # Merge orphans into main list (orphan key prefix = no task_id assigned yet)
    for oid, info in orphans.items():
        procs[oid] = info
    return {"processes": procs}


@router.get("/api/tasks/{task_id}/process")
async def get_task_process(task_id: int):
    """Get process info for a task. Also adopts orphan processes if found."""
    # Try to adopt any orphans that might belong to this task
    adopt_orphan_processes(task_id)
    procs = get_background_processes()
    pinfo = procs.get(str(task_id))
    if not pinfo:
        pinfo = get_orphan_processes().get(str(task_id))
    if not pinfo:
        return {"process": None}
    uptime = _time.time() - pinfo.get("started_at", _time.time())
    return {
        "process": {
            "pid": pinfo.get("pid"),
            "command": pinfo.get("command", ""),
            "alive": True,
            "uptime": round(uptime, 1),
            "output_file": pinfo.get("output_file", ""),
        }
    }


@router.get("/api/tasks/{task_id}/logs")
async def get_task_logs(task_id: int, lines: int = 50):
    """Get tail of a task's process output file."""
    procs = get_background_processes()
    pinfo = procs.get(str(task_id))
    if pinfo and pinfo.get("output_file"):
        output_path = pinfo["output_file"]
    else:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT output_files FROM tasks WHERE id=?", (task_id,)).fetchone()
        conn.close()
        if row and row[0]:
            try:
                files = json.loads(row[0])
                output_path = files[0] if isinstance(files, list) and files else None
            except Exception:
                output_path = None
        else:
            output_path = None
    if not output_path or not os.path.exists(output_path):
        return {"logs": "", "lines": []}
    with open(output_path, "r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
    selected = all_lines[-lines:]
    return {"logs": "".join(selected), "lines": selected}


@router.post("/api/tasks/{task_id}/kill")
async def kill_task_process(task_id: int):
    """Kill the background shell process for a task."""
    procs = get_background_processes()
    pinfo = procs.get(str(task_id))
    output_text = ""
    if pinfo and pinfo.get("output_file"):
        try:
            with open(pinfo["output_file"], "r", encoding="utf-8", errors="replace") as f:
                output_text = f.read()
        except Exception:
            pass
    cleanup_background_process(task_id)
    if output_text:
        context = get_task_context(task_id) or []
        context.append({"role": "system", "content": f"Process killed by user.\n---Output---\n{output_text[-3000:]}"})
        save_task_context(task_id, context)
    update_task_status(task_id, "interrupted", output_text[-200:], interruption_reason="user")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE tasks SET resume_count=0 WHERE id=?", (task_id,))
    conn.commit()
    conn.close()
    return {"status": "success", "message": "Process killed"}


@router.post("/api/tasks/{task_id}/reset-resume-count")
async def reset_task_resume_count(task_id: int):
    """Reset a task's resume_count to 0 so guardian loop can retry."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE tasks SET resume_count=0, updated_at=CURRENT_TIMESTAMP WHERE id=?", (task_id,))
    conn.commit()
    conn.close()
    from api.config import log_agent_error
    log_agent_error(f"Task #{task_id}: resume_count manually reset to 0")
    return {"status": "success"}


@router.post("/api/tasks/{task_id}/reply")
async def reply_to_background_task(task_id: int, body: dict):
    """Reply to a background task that called ask_user_question.
    Puts the answer into the agent's user_input_queue to unblock it.
    """
    answer = body.get("answer", "")
    if not answer:
        raise HTTPException(status_code=400, detail="answer is required")
    from api.state import _background_agents
    agent = _background_agents.get(task_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Task not found or not running in background")
    try:
        from queue import Queue
        agent.user_input_queue.put_nowait(answer)
        return {"status": "success", "message": f"Answer delivered to task #{task_id}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to deliver answer: {e}")
