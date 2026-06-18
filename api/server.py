import os
import sys
import json
import re
import asyncio
import sqlite3
import threading
import signal
import time as _time
from datetime import datetime, timezone, timedelta

# --- Tiktoken Monkeypatch for PyInstaller ---
try:
    import tiktoken
    from tiktoken.core import Encoding
    
    def get_mock_encoding(name):
        # Basic cl100k_base definition to satisfy LiteLLM / Tiktoken
        return Encoding(
            name="cl100k_base",
            pat_str=r"""(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\r\n\p{L}\p{N}]?\p{L}+|\p{N}{1,3}| ?[^\s\p{L}\p{N}]+[\r\n]*|\s*[\r\n]|\s+(?!\S)|\s+""",
            mergeable_ranks={},
            special_tokens={"<|endoftext|>": 100257, "<|fim_prefix|>": 100258, "<|fim_middle|>": 100259, "<|fim_suffix|>": 100260, "<|endofprompt|>": 100276}
        )

    # Only patch if it's actually failing (Unknown encoding)
    try:
        tiktoken.get_encoding("cl100k_base")
    except Exception:
        tiktoken.get_encoding = lambda name: get_mock_encoding(name)
        tiktoken.encoding_for_model = lambda model: get_mock_encoding("cl100k_base")
except Exception:
    pass
# --------------------------------------------
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Form, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Dict, Optional, Any
from dotenv import load_dotenv, set_key

from core.paths import get_data_path, get_skills_dir
from core.llamacpp_manager import get_llamacpp_manager
from tools.shell import interrupt_shell
from core.plugin_manager import discover_plugins, list_plugins, list_all_plugins, unload_plugin, toggle_plugin, install_from_git, fetch_marketplace

# ── Route modules ──
from api.routes.benchmark import router as benchmark_router, init_benchmark_routes
from api.routes.downloads import router as downloads_router, init_download_routes
from api.routes.uploads import router as uploads_router
from api.routes.routes_skills import router as skills_router
from api.routes.routes_memories import router as memories_router
from api.routes.routes_sessions import router as sessions_router
from api.routes.routes_plugins import router as plugins_router
from api.routes.routes_searxng import router as searxng_router
from api.routes.routes_goals import router as goals_router
from api.routes.routes_tasks import router as tasks_router
from api.routes.routes_settings import router as settings_router

# Load environment variables
env_file = get_data_path(".env")
load_dotenv(env_file)

from agent.agent import OpenAGCAgent
import litellm
# Fix for PyInstaller bundling issue with tiktoken
litellm.num_tokens_logging = False 
litellm.supports_token_counter = False
# LiteLLM debug logging — uncomment to debug model/pricing issues
# litellm._turn_on_debug()
# litellm.set_verbose = True

# Import modularized components
from api.config import load_config, save_config, log_agent_error
from api.db import DB_PATH, init_db, create_indexes
from api.state import (
    connected_websockets, _sandbox_waits, _pending_sandbox_approvals,
    _apply_pending_sandbox_approvals, _active_agents, _background_agents,
    _session_enabled_tools, _guardian_resume_lock, _llamacpp_download_state,
    _SERVER_START_TIME, _broadcast_to_websockets, _ws_send_safe,
)
from api.task_core import (
    create_task, update_task_status, update_task_type, get_task_context, save_task_context,
    add_task_step, _extract_task_title, _record_task_deliverables, _load_session_context,
    _get_task_step_count, _resolve_task_for_query, _resolve_goal_for_query,
    _check_goal_completeness,
)

# Ensure local connections bypass proxy
for var in ["no_proxy", "NO_PROXY"]:
    current = os.environ.get(var, "")
    local_hosts = "localhost,127.0.0.1"
    if not current:
        os.environ[var] = local_hosts
    elif "localhost" not in current or "127.0.0.1" not in current:
        os.environ[var] = f"{current.rstrip(',')},{local_hosts}"

app = FastAPI(title="Open-AGC UI Server")
app.include_router(benchmark_router)
app.include_router(downloads_router)
app.include_router(uploads_router)
app.include_router(skills_router)
app.include_router(memories_router)
app.include_router(sessions_router)
app.include_router(plugins_router)
app.include_router(searxng_router)
app.include_router(goals_router)
app.include_router(tasks_router)
app.include_router(settings_router)

# Initialize benchmark and download route modules with dependencies
init_benchmark_routes(
    db_path=DB_PATH,
    download_state=_llamacpp_download_state,
    install_state={},
    broadcast_fn=_broadcast_to_websockets,
    get_engine=lambda: None,
    get_llamacpp=get_llamacpp_manager,
    load_config=load_config,
)
init_download_routes(
    db_path=DB_PATH,
    download_state=_llamacpp_download_state,
    install_state={"active": False, "stage": "idle", "label": "", "progress": 0, "error": ""},
    broadcast_fn=_broadcast_to_websockets,
    training_avail=False,
    get_llamacpp=get_llamacpp_manager,
    load_config=load_config,
)

@app.on_event("startup")
async def _capture_event_loop():
    loop = asyncio.get_running_loop()
    _state_mod._main_event_loop = loop
    print(f"[Server] Event loop captured: {loop}")

# Initialize Database
DB_PATH = get_data_path("chat_history.db")

# Lazy broadcast wrapper: _broadcast_to_websockets is defined later in this file
# (at line ~2314), so we resolve it dynamically via globals().
def _plugin_broadcast(data):
    f = globals().get('_broadcast_to_websockets')
    if f:
        f(data)

# ── Plugin Discovery ──
_plugins_dir = os.path.abspath(os.path.join(os.path.dirname(DB_PATH), "..", "plugins"))
_plugins = discover_plugins(plugins_dir=_plugins_dir, broadcast_fn=_plugin_broadcast, server_config=load_config() if "load_config" in dir() else {})

def _mount_plugins(app, plugins):
    for p in plugins:
        inst = p.instance
        if inst and inst.router:
            prefix = inst.router_prefix or f"/api/plugin/{p.name}"
            app.include_router(inst.router, prefix=prefix)
            print(f"[Server] Mounted plugin router: {p.name} -> {prefix}")
        if inst and inst.static_dir and os.path.isdir(inst.static_dir):
            from fastapi.staticfiles import StaticFiles
            app.mount(f"/static/plugins/{p.name}", StaticFiles(directory=inst.static_dir), name=f"plugin_{p.name}_static")
            print(f"[Server] Mounted plugin static: {p.name}")

_mount_plugins(app, _plugins)
print(f"[Server] Loaded {len(_plugins)} plugin(s)")

init_db()

# ── Create indexes for query performance ──

def reconcile_downloads():
    """On startup, scan .partial files and reconcile DB records."""
    from core.llamacpp_manager import get_llamacpp_manager
    manager = get_llamacpp_manager()
    models_dir = manager.models_dir
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 1. Find all .partial files
    partial_files = {}
    if os.path.exists(models_dir):
        for f in os.listdir(models_dir):
            if f.endswith(".partial"):
                partial_path = os.path.join(models_dir, f)
                gguf_path = partial_path[:-len(".partial")]
                if os.path.exists(gguf_path):
                    try:
                        os.remove(partial_path)
                    except OSError:
                        pass
                else:
                    partial_files[f] = partial_path

    # 2. Fetch existing DB records
    cursor.execute("SELECT id, partial_path, target_path, status FROM downloads")
    existing = {row[1]: {"id": row[0], "target_path": row[2], "status": row[3]}
                for row in cursor.fetchall()}

    # 3. Reconcile .partial files with DB
    for partial_name, partial_path in partial_files.items():
        file_size = os.path.getsize(partial_path)
        if partial_path in existing:
            rec = existing[partial_path]
            if rec["status"] == "downloading":
                cursor.execute(
                    "UPDATE downloads SET status='paused', downloaded_bytes=?, "
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (file_size, rec["id"])
                )
                cursor.execute(
                    "UPDATE downloads SET progress="
                    "CASE WHEN total_size > 0 THEN CAST(downloaded_bytes AS REAL) / total_size ELSE 0 END "
                    "WHERE id=?", (rec["id"],)
                )
        else:
            label = partial_name.replace(".partial", "")
            target_path = os.path.join(models_dir, label)
            cursor.execute(
                '''INSERT INTO downloads (type, label, filename, target_path, partial_path,
                   downloaded_bytes, progress, status, source)
                   VALUES ('model', ?, ?, ?, ?, ?, 0.0, 'paused', 'huggingface')''',
                (f"{label} (待恢复)", label, target_path, partial_path, file_size)
            )
            cursor.execute(
                "UPDATE downloads SET progress="
                "CASE WHEN total_size > 0 THEN CAST(downloaded_bytes AS REAL) / total_size ELSE 0 END "
                "WHERE id=last_insert_rowid()"
            )

    # 4. Mark orphaned DB records
    cursor.execute(
        "SELECT id, partial_path, target_path, status FROM downloads "
        "WHERE status IN ('downloading', 'paused')"
    )
    for row in cursor.fetchall():
        rec_id, partial_path, target_path, status = row
        if partial_path and not os.path.exists(partial_path):
            if target_path and os.path.exists(target_path):
                cursor.execute(
                    "UPDATE downloads SET status='completed', progress=1.0, "
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (rec_id,)
                )
            else:
                cursor.execute(
                    "UPDATE downloads SET status='failed', "
                    "error_message='Server restart - partial file lost', "
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (rec_id,)
                )

    conn.commit()
    conn.close()

reconcile_downloads()

def reconcile_tasks():
    """On startup, mark any 'running' tasks as interrupted (server was restarted)."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        interrupted = cursor.execute(
            "SELECT id FROM tasks WHERE status='running'").fetchall()
        cursor.execute(
            "UPDATE tasks SET status='interrupted', interruption_reason='server_restart', "
            "updated_at=CURRENT_TIMESTAMP WHERE status='running'")
        count = cursor.rowcount
        conn.commit()

        # For each interrupted task, try to build a fallback snapshot
        # from task_steps + messages table (since run_turn never completed
        # and context_snapshot was never saved).
        for (tid,) in interrupted:
            try:
                _ctx = get_task_context(tid)
                if _ctx and len(_ctx) > 1:
                    # A valid snapshot already exists — skip
                    continue
                # Reconstruct from task_steps + messages table
                _cid = cursor.execute(
                    "SELECT session_id, user_query, created_at FROM tasks WHERE id=?",
                    (tid,)).fetchone()
                if not _cid:
                    continue
                _sid, _uq_text, _task_created = _cid
                _rebuilt = []
                # Get tool steps with timestamps
                _steps = cursor.execute(
                    "SELECT tool_name, full_result, tool_call_id, full_args, "
                    "created_at FROM task_steps WHERE task_id=? ORDER BY created_at ASC",
                    (tid,)).fetchall()
                # Collect all entries with timestamps for interleaving
                _entries = []  # (ts, type, data)
                if _uq_text:
                    _entries.append((_task_created or "", "user", {"role": "user", "content": _uq_text}))
                for _s in _steps:
                    _ts = _s[4] or ""
                    _tc_id = _s[2] or f"call_recon_{_s[0]}"
                    _args = _s[3] or "{}"
                    _entries.append((_ts, "assistant", {
                        "role": "assistant", "content": None,
                        "tool_calls": [{
                            "id": _tc_id, "type": "function",
                            "function": {"name": _s[0], "arguments": _args}
                        }]
                    }))
                    _entries.append((_ts, "tool", {
                        "role": "tool", "tool_call_id": _tc_id,
                        "name": _s[0],
                        "content": (_s[1] or "(无输出)")[:5000]
                    }))
                # Add user + agent text from messages table (any time — may precede task creation)
                if _sid:
                    _msgs = cursor.execute(
                        "SELECT role, content, created_at FROM messages "
                        "WHERE session_id=? ORDER BY id ASC", (_sid,)
                    ).fetchall()
                    for _m in _msgs:
                        _role = _m[0]
                        _content = str(_m[1] or "")
                        _ts = str(_m[2] or "")
                        if not _content:
                            continue
                        if _role == "user":
                            # Deduplicate: skip if same content already in entries
                            if not any(
                                e[2].get("role") == "user" and e[2].get("content") == _content
                                for e in _entries
                            ):
                                _entries.append((_ts, "user", {"role": "user", "content": _content}))
                        elif _role == "agent":
                            # Agent text responses — skip system/meta messages
                            if not _content.startswith("[") and not _content.startswith("【"):
                                _entries.append((_ts, "assistant", {
                                    "role": "assistant", "content": _content
                                }))
                # Sort all entries by timestamp, then type (user before tool for same ts)
                _type_order = {"user": 0, "assistant": 1, "tool": 2}
                _entries.sort(key=lambda x: (x[0], _type_order.get(x[1], 99)))
                _rebuilt = [e[2] for e in _entries]
                if len(_rebuilt) > len(_ctx or []):
                    save_task_context(tid, _rebuilt)
                    print(f"[Startup] Saved fallback snapshot for task {tid}: {len(_rebuilt)} msgs")
            except Exception as _re_err:
                print(f"[Startup] Fallback snapshot error for task {tid}: {_re_err}")

        conn.close()
        if count:
            print(f"[Startup] Marked {count} running task(s) as interrupted (server restart)")
    except Exception as e:
        print(f"[Startup] Task reconciliation error: {e}")

# _extract_task_title imported from api.task_core

reconcile_tasks()

def reconcile_backgrounded_after_restart():
    """After server restart, check for completed downloads and lost shell processes
    linked to backgrounded tasks."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        pairs = conn.execute(
            "SELECT d.id as dl_id, d.task_id, t.status as task_status FROM downloads d "
            "JOIN tasks t ON t.id = d.task_id "
            "WHERE d.status = 'completed' AND d.background_resumed = 0 "
            "AND t.status = 'backgrounded'"
        ).fetchall()
        for p in pairs:
            tid = p["task_id"]
            ctx = get_task_context(tid)
            if ctx:
                ctx.append({"role": "user", "content": (
                    "【系统通知】服务器重启，后台下载任务已完成，文件已就绪。"
                    "请继续执行之前未完成的任务。"
                )})
                update_task_status(tid, "interrupted",
                    "服务器重启，后台任务已完成", interruption_reason="background_complete")
                save_task_context(tid, ctx)
                conn.execute("UPDATE downloads SET background_resumed=1 WHERE id=?",
                             (p["dl_id"],))
                conn.commit()
                print(f"[Startup] Recovered backgrounded task {tid} from completed download {p['dl_id']}")
        # Also reconcile failed downloads linked to any task
        failed_pairs = conn.execute(
            "SELECT DISTINCT d.id as dl_id, d.task_id, d.label, d.filename, d.error_message, "
            "(SELECT session_id FROM task_steps WHERE task_id = d.task_id AND session_id IS NOT NULL LIMIT 1) as session_id "
            "FROM downloads d "
            "JOIN tasks t ON t.id = d.task_id "
            "WHERE d.status = 'failed' AND d.background_resumed = 0 "
            "AND t.status IN ('completed', 'interrupted', 'background_failed')"
        ).fetchall()
        for p in failed_pairs:
            label = p["label"] or p["filename"] or f"download #{p['dl_id']}"
            err = p["error_message"] or "未知错误"
            session_id = p["session_id"] or 1
            save_message("system",
                f"❌ 下载失败: {label}\n错误信息: {err}",
                session_id)
            conn.execute("UPDATE downloads SET background_resumed=1 WHERE id=?",
                         (p["dl_id"],))
            conn.commit()
            print(f"[Startup] Recovered failed download #{p['dl_id']} (task {p['task_id']}) — message saved to session {session_id}")

        # Reconcile backgrounded tasks whose shell process info was lost (in-memory dict)
        lost_tasks = conn.execute(
            "SELECT id FROM tasks WHERE status='backgrounded' "
            "AND id NOT IN (SELECT DISTINCT task_id FROM downloads WHERE status='completed' AND background_resumed=0)"
        ).fetchall()
        if lost_tasks:
            print(f"[Startup] Found {len(lost_tasks)} backgrounded task(s) with lost process info (server restart)")
            for t in lost_tasks:
                try:
                    tid = t["id"]
                    ctx = get_task_context(tid)
                    if ctx:
                        ctx.append({"role": "user", "content": (
                            "【系统通知】服务器重启，后台命令的进程信息已丢失。"
                            "请检查之前的工作状态，如有需要请重新执行。"
                        )})
                        save_task_context(tid, ctx)
                    update_task_status(tid, "interrupted",
                        "服务器重启，后台进程信息丢失", interruption_reason="process_lost")
                    print(f"[Startup] Task {tid}: marked interrupted (process info lost on restart)")
                except Exception as e:
                    print(f"[Startup] Task {tid}: recovery error: {e}")

        conn.close()
    except Exception as e:
        print(f"[Startup] Background recovery error: {e}")

# Note: reconcile_backgrounded_after_restart() is called later after all helper functions are defined

# Initialize StatsManager singleton with correct database
from core.stats_manager import get_stats_manager
get_stats_manager(DB_PATH)

# Task helper functions
# create_task imported from api.task_core
_CONTINUATION_PREFIXES = frozenset({
    # Chinese
    '继续', '再', '还', '然后', '是的', '对的', '对', '好', '行', '嗯',
    '这个', '那个', '他', '她', '它', '他们', '她们', '它们',
    '重试', '重新', '再来', '下一步', '下一条', '上一个',
    '是', '不', '不要', '算了', '换', '换一个', '换一批',
    # English
    'yes', 'no', 'ok', 'okay', 'sure', 'go on', 'continue',
    'retry', 'again', 'next', 'previous', 'that', 'this', 'it',
    'him', 'her', 'them', 'that one', 'this one',
})
# _resolve_goal_for_query imported from api.task_core
# Download record helpers
# ==========================================

# create_download_record - in routes_settings.py
# update_download_progress - in routes_settings.py
# _direct_resume_background_task - in routes_settings.py
# log_download_event - in routes_settings.py
# get_download_events - in routes_settings.py
# get_download_record - in routes_settings.py
# list_download_records - in routes_settings.py
# delete_download_record - in routes_settings.py
def save_message(role: str, content: str, session_id: int = 1):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO messages (role, content, session_id) VALUES (?, ?, ?)", (role, content, session_id))
    cursor.execute("UPDATE sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (session_id,))
    conn.commit()
    conn.close()

# Mount the static directory
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def read_index():
    return FileResponse("static/index.html")

@app.get("/api/files/{file_path:path}")
async def get_sandbox_file(file_path: str):
    """Serve files dynamically from the current sandbox directory to the UI."""
    config = load_config()
    sandbox_dir = config.get("sandbox_dir", os.path.abspath(os.path.join(os.getcwd(), "workspace")))
    full_path = os.path.abspath(os.path.join(sandbox_dir, file_path))
    if not full_path.startswith(os.path.abspath(sandbox_dir)):
        raise HTTPException(status_code=403, detail="Forbidden directory traversal")
    if not os.path.exists(full_path) or not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(full_path)

# --- Configuration System ---
CONFIG_PATH = get_data_path("config.json")

def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "api_keys": {
            "llamacpp": "http://localhost:8080/v1",
            "huggingface": "",
            "tavily": "",
            "brave_search": "",
            "searxng": ""
        },
        "default_model": "moonshot/kimi-latest",
        "fallback_models": ["deepseek/deepseek-chat"],
        "disabled_skills": [],
        "sandbox_mode": True,
        "sandbox_dir": os.path.abspath(os.path.join(os.getcwd(), "workspace")),
        "llamacpp_ctx_size": 32768,
        "browser_headless": False,
        "http_proxy": "",
        "heartbeat_enabled": False,
        "heartbeat_interval": 180,
        "email_listener_enabled": False,
        "email_account": "",
        "email_password": "",
        "email_imap_server": "",
        "email_smtp_server": "",
        "owner_email": "",
        "mcp_servers": {},
        "max_correction_attempts": 5
    }

# Settings, Llamacpp, Downloads, AgentDesign routes imported from api.routes.routes_settings
# Register WebSocket endpoint
from api.ws import websocket_endpoint
app.websocket("/ws")(websocket_endpoint)

# SPA fallback: serve index.html for all unmatched frontend routes
@app.get("/{full_path:path}")
async def spa_fallback(full_path: str):
    return FileResponse("static/index.html")

# Start background systems
import api.state as _state_mod
import api.background as _bg
_bg.start_email_listener()
_bg.start_task_scheduler()
_bg.start_background_monitor()
_bg.start_guardian_loop()

# Call local startup reconciliation
reconcile_tasks()
reconcile_backgrounded_after_restart()
