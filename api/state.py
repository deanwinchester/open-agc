"""Global state for the Open-AGC API server.

All shared mutable state lives here so it can be imported by any module
without circular dependency issues.
"""
import asyncio
import os
import threading
from datetime import datetime, timezone

# Store the main event loop for cross-thread WebSocket broadcasts
_main_event_loop: asyncio.AbstractEventLoop = None

# Server PID — set at startup so agent tools can protect against self-kill
_server_pid: int = os.getpid()
_SERVER_START_TIME = datetime.now(timezone.utc)

# Connected WebSocket clients (for background task push)
connected_websockets: list = []  # List of active WebSocket connections

# Sandbox auth waits: {session_id: {"event": threading.Event, "result": dict}}
_sandbox_waits: dict = {}

# Pending sandbox approvals: {session_id: [paths]} — late approvals applied on task resume
_pending_sandbox_approvals: dict = {}


def check_protected_pid(pid: int) -> bool:
    """Check if a PID belongs to the Open-AGC server or its parent processes.

    Only protects UPWARDS (server + its parent chain like VS Code debugger).
    Child processes (agent-launched) are NOT protected — the agent can manage them.
    """
    if pid <= 0:
        return False
    if pid == _server_pid:
        return True
    # Check parent chain (VS Code debugger, terminal, etc.)
    try:
        import psutil
        current = psutil.Process(_server_pid)
        ancestors = current.parents()
        for anc in ancestors[:3]:
            if anc.pid == pid:
                return True
    except ImportError:
        pass
    except Exception:
        pass
    return False


def _apply_pending_sandbox_approvals(agent, session_id):
    """Load pending sandbox approvals into agent's session whitelist."""
    paths = _pending_sandbox_approvals.pop(session_id, [])
    for p in paths:
        path = p.get("path", "")
        if not path:
            continue
        import os as _ap_os
        abs_p = _ap_os.path.abspath(path)
        agent._session_sandbox_whitelist.add(abs_p)
        parent = _ap_os.path.dirname(abs_p)
        if parent != abs_p:
            agent._session_sandbox_whitelist.add(parent)
        print(f"[Sandbox] Loaded pending approval: {path}")


# Active agents: {session_id: {task_id: OpenAGCAgent}} — multi-task concurrent support
_active_agents: dict = {}

# Background agents: {task_id: OpenAGCAgent} — background tasks for interrupt
_background_agents: dict = {}

# Progressive tool persistence: {session_id: set(tool_names)}
_session_enabled_tools: dict = {}

# Guardian resume lock — prevents concurrent guardian resume executions
_guardian_resume_lock = threading.Lock()

# Llama download progress tracking
_llamacpp_download_state = {
    "active": False,
    "type": "",
    "label": "",
    "progress": 0.0,
    "stage": "",
    "error": "",
    "cancelled": False,
}

# Server start time — used by BgMonitor to detect restart-induced process loss
_SERVER_START_TIME = datetime.now(timezone.utc)

# Agent log file path
_AGENT_LOG_FILE = None


def _broadcast_task_history(task_id: int, session_id: int, task_status: str = "interrupted"):
    """Fetch task steps and broadcast as history_steps for UI rendering."""
    try:
        from api.db import DB_PATH
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT step_number, tool_name, tool_label, args_preview, result_preview, full_result, full_args, success, thinking_content "
            "FROM task_steps WHERE task_id=? ORDER BY created_at ASC", (task_id,)).fetchall()
        conn.close()
        if not rows:
            return
        steps = []
        for r in rows:
            steps.append({
                "step_number": r["step_number"],
                "tool_name": r["tool_name"],
                "tool_label": r["tool_label"] or r["tool_name"],
                "args_preview": r["args_preview"] or "",
                "result_preview": r["result_preview"] or "",
                "full_result": r["full_result"] or "",
                "full_args": r["full_args"] or "",
                "success": bool(r["success"]),
                "thinking_content": r["thinking_content"] if "thinking_content" in r.keys() else None,
            })
        _broadcast_to_websockets({
            "type": "history_steps", "task_id": task_id,
            "session_id": session_id, "steps": steps, "task_status": task_status,
        })
    except Exception as e:
        print(f"[Task] Failed to broadcast task history: {e}")


async def _ws_send_safe(ws, message):
    """Send a message via WebSocket, ignoring connection errors."""
    try:
        await ws.send_json(message)
    except Exception:
        pass


def _broadcast_to_websockets(message: dict):
    """Send a message to all connected WebSocket clients (best-effort). Thread-safe."""
    global _main_event_loop
    loop = _main_event_loop
    if loop is None or loop.is_closed() or not loop.is_running():
        return
    dead = []
    for ws in list(connected_websockets):
        try:
            asyncio.run_coroutine_threadsafe(_ws_send_safe(ws, message), loop)
        except Exception:
            dead.append(ws)
    for ws in dead:
        try:
            connected_websockets.remove(ws)
        except ValueError:
            pass
