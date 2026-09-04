"""Global state for the Open-AGC API server.

All shared mutable state lives here so it can be imported by any module
without circular dependency issues.
"""
import asyncio
import os
import threading
from datetime import datetime, timezone
from typing import Dict, Optional

# Store the main event loop for cross-thread WebSocket broadcasts
_main_event_loop: asyncio.AbstractEventLoop = None

# Server PID — set at startup so agent tools can protect against self-kill
_server_pid: int = os.getpid()
_SERVER_START_TIME = datetime.now(timezone.utc)

# Connected WebSocket clients (for background task push)
connected_websockets: list = []  # List of active WebSocket connections

# Final responses waiting to be delivered to reconnecting clients
# Maps session_id -> {"content": str, "task_id": int}
_pending_final_responses: Dict[int, dict] = {}

# Sandbox auth waits: {session_id: {"event": threading.Event, "result": dict}}
_sandbox_waits: dict = {}

# Pending sandbox approvals: {session_id: [paths]} — late approvals applied on task resume
_pending_sandbox_approvals: dict = {}


def resolve_sandbox_wait(wait: dict, msg: dict) -> None:
    """Populate a sandbox wait's result holder from a client response and set
    the event. Single implementation shared by ws.py (both sandbox_response
    paths) and /api/sandbox/approve.

    For category='secret' waits (request_secret popup) the form fields are
    passed through to the agent's result_holder — in-memory only: never
    logged, never written to messages or disk by this layer.
    """
    wait["result"]["action"] = msg.get("action", "deny_once")
    wait["result"]["path"] = msg.get("path", "")
    if msg.get("password"):
        wait["result"]["password"] = msg["password"]
    if (wait.get("payload") or {}).get("category") == "secret":
        for _k in ("secret_name", "secret_type", "host", "username", "note"):
            if msg.get(_k) is not None:
                wait["result"][_k] = msg[_k]
    wait["event"].set()

# Session-level sudo password cache: {session_id: str}
# A new agent instance is created per message, so the instance-level cache
# alone loses the sudo authorization — the password popup then appears (or
# fails to appear) unpredictably. This store survives agent re-creation.
# Passwords live here only (never sent to the LLM, never persisted to disk).
_session_sudo_passwords: Dict[int, str] = {}

# Deleted-task tombstones: {task_id}
# 删除运行中的任务时，agent 可能因注册表竞态错过中断标志（先注册后运行
# 的窗口）；完成/恢复路径据此拒绝再为该任务写库或重启线程。
_deleted_task_ids: set = set()
_deleted_task_ids_lock = threading.Lock()


def mark_task_deleted(task_id) -> None:
    if task_id is None:
        return
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        return
    with _deleted_task_ids_lock:
        _deleted_task_ids.add(tid)


def is_task_deleted(task_id) -> bool:
    try:
        tid = int(task_id)
    except (TypeError, ValueError):
        return False
    with _deleted_task_ids_lock:
        return tid in _deleted_task_ids

# Session-level permission whitelist: {session_id: set(categories)}
_session_permission_whitelists: Dict[int, set] = {}

# Session-level ONE-SHOT permission grants: {session_id: set(command_strings)}
# approve_once（「授权本次」）只授权当前这一条命令——shell 执行到该命令时
# 消费掉（用完即焚），下一条同类命令重新弹窗；与 _session_permission_whitelists
# （approve_session/approve_always 的整类放行）语义区分。
_session_permission_once: Dict[int, set] = {}


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
            "SELECT step_number, tool_name, tool_label, args_preview, result_preview, full_result, full_args, success, thinking_content, sub_task "
            "FROM task_steps WHERE task_id=? ORDER BY id ASC", (task_id,)).fetchall()
        conn.close()
        if not rows:
            return
        steps = []
        for r in rows:
            full_r = r["full_result"] or ""
            full_a = r["full_args"] or ""
            steps.append({
                "step_number": r["step_number"],
                "tool_name": r["tool_name"],
                "tool_label": r["tool_label"] or r["tool_name"],
                "args_preview": r["args_preview"] or "",
                "result_preview": r["result_preview"] or "",
                # full_result/full_args 不随 WS 广播：老任务的全量输出动辄
                # 数百 KB，连接即推送会在旧 libsoup2（UOS WebKitGTK 2.38）上
                # 把网络进程冲垮（100% CPU 空转、全部请求饿死）。改为标记
                # has_full，前端展开步骤详情时按需走 REST 拉取。
                "has_full": bool(full_r or full_a),
                "success": bool(r["success"]),
                "thinking_content": r["thinking_content"] if "thinking_content" in r.keys() else None,
                "sub_task": r["sub_task"] or "",
            })
        _broadcast_to_websockets({
            "type": "history_steps", "task_id": task_id,
            "session_id": session_id, "steps": steps, "task_status": task_status,
        })
    except Exception as e:
        print(f"[Task] Failed to broadcast task history: {e}")


# WS 消息分片（libsoup2 兼容）：UOS/deepin 的 WebKitGTK 2.38 + libsoup2 2.64
# 处理 >16KB 的 WebSocket 帧有缺陷——网络进程主循环卡死在 100% CPU 空转，
# 页面所有请求饿死（生产实证，实测阈值在 16~32KB 之间）。大消息拆成 8KB 的
# _chunk 帧，前端重组后按原消息分发；小消息（绝大多数）不受影响。
WS_CHUNK_SIZE = 8000


def _ws_chunk_frames(message) -> list:
    """把待发送的 dict 消息拆成一组 JSON 文本帧（<=8KB）。小消息原样单帧。"""
    import json as _json
    import uuid as _uuid
    text = _json.dumps(message, ensure_ascii=False)
    if len(text.encode("utf-8")) <= WS_CHUNK_SIZE:
        return [text]
    cid = _uuid.uuid4().hex[:8]
    pieces = [text[i:i + WS_CHUNK_SIZE] for i in range(0, len(text), WS_CHUNK_SIZE)]
    n = len(pieces)
    return [
        _json.dumps({"type": "_chunk", "id": cid, "i": i, "n": n, "data": p},
                    ensure_ascii=False)
        for i, p in enumerate(pieces)
    ]


async def _ws_send_safe(ws, message):
    """Send a message via WebSocket, ignoring connection errors.

    大消息自动分片（见 _ws_chunk_frames 注释）。
    """
    try:
        for frame in _ws_chunk_frames(message):
            await ws.send_text(frame)
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
