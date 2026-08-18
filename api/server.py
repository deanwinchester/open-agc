import os
import shutil
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
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
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
from api.routes.routes_system import router as system_router
from api.routes.routes_stats import router as stats_router
from api.routes.routes_goals import router as goals_router
from api.routes.routes_tasks import router as tasks_router
from api.routes.routes_settings import router as settings_router
from api.routes.routes_secrets import router as secrets_router
from api.routes.routes_sandbox import router as sandbox_router
from api.routes.routes_auth import router as auth_router

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

# ── 访问控制中间件：本机免密 / 局域网凭密码 / 公网 403（含 IPv6）──
# 纯 ASGI 实现，覆盖全部 HTTP 路由、静态挂载与 /ws WebSocket；只认 TCP
# 对端 IP（request.client.host），不信任 X-Forwarded-For。密码改动即时生效。
from core.access_control import AccessControlMiddleware
app.add_middleware(AccessControlMiddleware)

app.include_router(benchmark_router)
app.include_router(downloads_router)
app.include_router(uploads_router)
app.include_router(skills_router)
app.include_router(memories_router)
app.include_router(sessions_router)
app.include_router(plugins_router)
app.include_router(searxng_router)
app.include_router(system_router)
app.include_router(stats_router)
app.include_router(goals_router)
app.include_router(tasks_router)
app.include_router(settings_router)
app.include_router(secrets_router)
app.include_router(sandbox_router)
app.include_router(auth_router)

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

# ── Plugin Discovery (only from data/plugins — built-in plugins are copied there on first run) ──
from core.paths import get_user_plugins_dir
_user_plugins_dir = get_user_plugins_dir()

# Copy built-in plugins from the source tree to data/plugins (first run only)
_builtin_plugins_dir = os.path.abspath(os.path.join(os.path.dirname(DB_PATH), "..", "plugins"))
if os.path.isdir(_builtin_plugins_dir):
    for _entry in os.listdir(_builtin_plugins_dir):
        _src = os.path.join(_builtin_plugins_dir, _entry)
        _dst = os.path.join(_user_plugins_dir, _entry)
        if os.path.isdir(_src) and not os.path.exists(_dst):
            try:
                shutil.copytree(_src, _dst)
                print(f"[Server] Copied built-in plugin {_entry} -> data/plugins")
            except Exception as e:
                print(f"[Server] Failed to copy plugin {_entry}: {e}")

_plugins = discover_plugins(plugins_dir=_user_plugins_dir, broadcast_fn=_plugin_broadcast,
                  server_config=load_config() if "load_config" in dir() else {})

# _mount_plugins 的实现移到 api/plugin_mount.py（import-light，幽灵路由剪除可单测）；
# 此处保留原名以兼容 routes_plugins.py 的 _srv._mount_plugins 调用。
from api.plugin_mount import mount_plugins as _mount_plugins

_mount_plugins(app, _plugins)
print(f"[Server] Loaded {len(_plugins)} plugin(s)")

init_db()

# 交付物登记制：启动幂等回填存量归属（outputs/task_*/、检查点 files_dir、
# task_steps.generated_files → deliverables/task_deliverables；只补缺失行，
# 不刷新 updated_at——见 api/deliverables_registry.backfill_deliverables）
try:
    from api.deliverables_registry import backfill_deliverables
    backfill_deliverables()
except Exception as _dbfill_err:
    print(f"[Server] Deliverables backfill error: {_dbfill_err}")

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
    """On startup, mark 'running'/'backgrounded' tasks as interrupted (server was restarted)."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        # Mark running tasks as interrupted
        running = cursor.execute(
            "SELECT id FROM tasks WHERE status='running'").fetchall()
        cursor.execute(
            "UPDATE tasks SET status='interrupted', interruption_reason='server_restart', "
            "updated_at=CURRENT_TIMESTAMP WHERE status='running'")
        count = cursor.rowcount
        conn.commit()

        # Also mark backgrounded tasks as interrupted if no surviving process info
        # (in-memory process dict is lost on restart, so they can't be monitored)
        bg_count = cursor.execute(
            "UPDATE tasks SET status='interrupted', interruption_reason='server_restart', "
            "updated_at=CURRENT_TIMESTAMP WHERE status='backgrounded'"
        ).rowcount
        if bg_count:
            print(f"[Startup] Marked {bg_count} backgrounded task(s) as interrupted (server restart)")
            conn.commit()

        # For each interrupted task, try to build a fallback snapshot
        # from task_steps + messages table (since run_turn never completed
        # and context_snapshot was never saved).
        # Build fallback snapshots for both running and backgrounded tasks
        all_interrupted = running + [(r[0],) for r in cursor.execute(
            "SELECT id FROM tasks WHERE status='interrupted' AND interruption_reason='server_restart'"
        ).fetchall() if r[0] not in [x[0] for x in running]]
        for (tid,) in all_interrupted:
            try:
                _ctx = get_task_context(tid)
                if _ctx and len(_ctx) > 1:
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

# 分身持久化（M2.5）：启动时把仍为 running 的 dispatches 判 lost——新进程
# 没有任何活跃线程，running 即失联（与上面 tasks 的 server_restart 同理）。
try:
    from agent.dispatcher import mark_stale_dispatches_lost as _mdl
    _mdl()
except Exception as _mdl_e:
    print(f"[Startup] dispatch lost-mark error: {_mdl_e}")

# reconcile_backgrounded_after_restart 移至 api.background（统一定义于恢复链路
# 所在模块，便于测试）；启动时通过 _bg.reconcile_backgrounded_after_restart() 调用。

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

# Mount the static directory (handle PyInstaller bundle path)
import sys as _sys
_MEIPASS = getattr(_sys, '_MEIPASS', None)
_cwd = os.getcwd()
_static_dir = os.path.join(_MEIPASS or _cwd, "static")
print(f"[Server] Serving static from: {_static_dir} (MEIPASS={_MEIPASS}, CWD={_cwd})")
if not os.path.isdir(_static_dir):
    print(f"[Server] WARNING: static directory not found at {_static_dir}")
    # Fallback: try project root relative path
    _static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static")
    print(f"[Server] Falling back to: {os.path.abspath(_static_dir)}")
# ── Vue3 SPA (唯一前端) ──
# Built by `npm run build` from vue-app/ into static/vue (gitignored).
_vue_static_dir = os.path.join(_static_dir, "vue")
os.makedirs(_vue_static_dir, exist_ok=True)
app.mount("/static/vue", StaticFiles(directory=_vue_static_dir), name="vue_static")

def _vue_spa_index_response():
    """返回 Vue SPA 入口页；产物未构建时返回 503 而非让 FileResponse 抛 500。"""
    index = os.path.join(_vue_static_dir, "index.html")
    if not os.path.isfile(index):
        return PlainTextResponse("Vue SPA 未构建，请先运行 npm run build", status_code=503)
    return FileResponse(index)

@app.get("/app")
async def vue_app_index():
    """Serve the new Vue3 SPA entry page."""
    return _vue_spa_index_response()

@app.get("/app/{path:path}")
async def vue_app_spa_fallback(path: str):
    """SPA fallback for Vue client-side routes under /app (e.g. /app/chat)."""
    return _vue_spa_index_response()

app.mount("/static", StaticFiles(directory=_static_dir), name="static")

@app.get("/")
async def read_index():
    """根入口：新 Vue SPA 已是默认前端，重定向到 /app。"""
    return RedirectResponse("/app")

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
        except Exception as _cfg_e:
            print(f"[Server] Config read error: {_cfg_e}")
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

# Legacy SPA fallback: redirect known legacy view paths to the Vue SPA (preserves
# old bookmarks); everything else unmatched is a 404 — notably /api/* must stay a
# JSON 404 and never be swallowed by a redirect.
_LEGACY_VIEW_PREFIXES = frozenset({
    "chat", "tasks", "task-detail", "goals", "downloads", "sandbox", "settings", "debug", "logs",
})

@app.get("/{full_path:path}")
async def spa_fallback(full_path: str, request: Request):
    first_segment = full_path.split("/", 1)[0]
    if first_segment in _LEGACY_VIEW_PREFIXES:
        target = f"/app/{full_path}"
        if request.url.query:
            target += f"?{request.url.query}"
        return RedirectResponse(target)
    raise HTTPException(status_code=404, detail="Not Found")

# Start background systems
import api.state as _state_mod
import api.background as _bg
_bg.start_email_listener()
_bg.start_task_scheduler()
_bg.start_background_monitor()
_bg.start_guardian_loop()
_bg.start_stale_rescue_loop()

# 沙箱 Janitor（沙箱治理二期）：定时 TTL 清理 tmp/ + 硬水位强制清空
try:
    from core.sandbox_janitor import start_sandbox_janitor as _start_sandbox_janitor
    _start_sandbox_janitor()
except Exception as _sj_e:
    print(f"[Server] Sandbox janitor start error: {_sj_e}")

# 访问密码环境变量播种（一次性）：config.json 未配置且
# OPEN_AGC_ACCESS_PASSWORD 有值时写入 config.json，之后判定只看 config
try:
    from core.access_control import seed_access_password_from_env as _seed_pw
    _seed_pw()
except Exception as _seed_e:
    print(f"[Server] Access password seed error: {_seed_e}")

# Restore persisted background process registry BEFORE reconcile: tasks whose
# pre-restart processes are still alive stay backgrounded and BgMonitor takes
# over (reconcile skips them), instead of being flipped to interrupted.
try:
    from tools.shell import restore_background_processes as _restore_bg_procs
    _restore_bg_procs()
except Exception as _rbp_e:
    print(f"[Server] Background process restore error: {_rbp_e}")

# Call local startup reconciliation
reconcile_tasks()
_bg.reconcile_backgrounded_after_restart()

# Run security audit on configuration
try:
    from core.config_audit import audit_all
    for _warn in audit_all():
        print(_warn)
except Exception as _audit_e:
    print(f"[Server] Config audit error: {_audit_e}")

# Run database maintenance (cleanup old logs, vacuum)
try:
    from core.db_maintenance import cleanup_old_data
    result = cleanup_old_data(days=30, min_cost=0.0)
    if result.get("model_logs", {}).get("deleted_rows", 0) > 0:
        print(f"[Server] DB cleanup: removed {result['model_logs']['deleted_rows']} old log entries")
    if result.get("vacuum", {}).get("bytes_freed", 0) > 0:
        mb = result["vacuum"]["bytes_freed"] / 1024 / 1024
        print(f"[Server] DB vacuum: freed {mb:.1f} MB")
except Exception as _db_e:
    print(f"[Server] DB maintenance error: {_db_e}")
