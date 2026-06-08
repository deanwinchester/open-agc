import os
import sys
import json
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

# Store the main event loop for cross-thread WebSocket broadcasts
_main_event_loop: asyncio.AbstractEventLoop = None

@app.on_event("startup")
async def _capture_event_loop():
    global _main_event_loop
    _main_event_loop = asyncio.get_running_loop()
    print(f"[Server] Event loop captured: {_main_event_loop}")

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

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    cursor = conn.cursor()

    # Sessions table (new)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            session_id INTEGER DEFAULT 1,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            user_query TEXT NOT NULL,
            status TEXT DEFAULT 'running',
            task_type TEXT DEFAULT 'oneshot',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            result_summary TEXT,
            output_files TEXT DEFAULT '[]',
            total_tokens INTEGER DEFAULT 0,
            total_cost REAL DEFAULT 0.0,
            schedule_cron TEXT,
            schedule_enabled INTEGER DEFAULT 0,
            next_run_at DATETIME,
            last_run_at DATETIME,
            run_count INTEGER DEFAULT 0,
            max_resume_count INTEGER DEFAULT 10,
            resume_count INTEGER DEFAULT 0,
            context_snapshot TEXT,
            interruption_reason TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS task_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            step_number INTEGER NOT NULL,
            tool_name TEXT NOT NULL,
            tool_label TEXT,
            args_preview TEXT,
            result_preview TEXT,
            full_result TEXT,
            success INTEGER DEFAULT 1,
            thinking_content TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL DEFAULT 'model',
            label TEXT NOT NULL DEFAULT '',
            repo_id TEXT,
            filename TEXT,
            source TEXT DEFAULT 'huggingface',
            url TEXT,
            target_path TEXT NOT NULL DEFAULT '',
            partial_path TEXT NOT NULL DEFAULT '',
            total_size INTEGER DEFAULT 0,
            downloaded_bytes INTEGER DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'downloading',
            progress REAL DEFAULT 0.0,
            error_message TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS download_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            download_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            message TEXT DEFAULT '',
            details TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (download_id) REFERENCES downloads(id) ON DELETE CASCADE
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS benchmark_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_id TEXT NOT NULL,
            model_source TEXT DEFAULT 'online',
            benchmark_type TEXT NOT NULL,
            metrics_json TEXT NOT NULL DEFAULT '{}',
            num_questions INTEGER DEFAULT 0,
            avg_latency_ms REAL DEFAULT 0,
            tokens_per_second REAL DEFAULT 0,
            status TEXT DEFAULT 'completed',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            session_id INTEGER,
            task_id INTEGER,
            provider TEXT,
            model TEXT,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            cost_estimate REAL DEFAULT 0.0
        )
    ''')
    # Model call logs (detailed LLM call tracking)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS model_call_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            session_id INTEGER,
            task_id INTEGER,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            request_data TEXT,
            response_data TEXT,
            cache_hit TEXT DEFAULT 'unknown',
            latency_ms INTEGER DEFAULT 0,
            cost_estimate REAL DEFAULT 0.0
        )
    ''')
    # Ensure datasets storage directory exists
    datasets_dir = os.path.join(os.path.dirname(DB_PATH), "datasets")
    os.makedirs(datasets_dir, exist_ok=True)

    # Migrate existing databases
    # Add session_id to messages if missing
    try:
        cursor.execute("ALTER TABLE messages ADD COLUMN session_id INTEGER DEFAULT 1")
    except Exception:
        pass
    # Ensure at least one default session exists
    cursor.execute("SELECT COUNT(*) FROM sessions")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO sessions (name) VALUES (?)", ("默认会话",))

    # Migrate: add new columns if they don't exist yet
    try:
        cursor.execute("ALTER TABLE downloads ADD COLUMN category TEXT DEFAULT 'model'")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE tasks ADD COLUMN task_type TEXT DEFAULT 'oneshot'")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE tasks ADD COLUMN schedule_cron TEXT")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE tasks ADD COLUMN schedule_enabled INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE tasks ADD COLUMN next_run_at DATETIME")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE tasks ADD COLUMN last_run_at DATETIME")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE tasks ADD COLUMN run_count INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE tasks ADD COLUMN max_resume_count INTEGER DEFAULT 10")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE tasks ADD COLUMN resume_count INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE tasks ADD COLUMN context_snapshot TEXT")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE tasks ADD COLUMN total_tokens INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE tasks ADD COLUMN total_cost REAL DEFAULT 0.0")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE tasks ADD COLUMN interruption_reason TEXT")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE tasks ADD COLUMN session_id INTEGER DEFAULT 1")
    except Exception:
        pass

    # Email columns for per-session email binding
    try:
        cursor.execute("ALTER TABLE sessions ADD COLUMN email_enabled INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE sessions ADD COLUMN email_account TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE sessions ADD COLUMN email_password TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE sessions ADD COLUMN email_imap_server TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE sessions ADD COLUMN email_smtp_server TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE sessions ADD COLUMN owner_email TEXT DEFAULT ''")
    except Exception:
        pass

    # Add session_id to task_steps for session persistence
    try:
        cursor.execute("ALTER TABLE task_steps ADD COLUMN session_id INTEGER DEFAULT 1")
    except Exception:
        pass

    # Add tool_call_id and full_args for perfect context reconstruction
    try:
        cursor.execute("ALTER TABLE task_steps ADD COLUMN tool_call_id TEXT")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE task_steps ADD COLUMN full_args TEXT")
    except Exception:
        pass

    # Add task_id to downloads for background task linkage
    try:
        cursor.execute("ALTER TABLE downloads ADD COLUMN task_id INTEGER")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE downloads ADD COLUMN background_resumed INTEGER DEFAULT 0")
    except Exception:
        pass

    # Add cached_tokens to model_call_logs
    try:
        cursor.execute("ALTER TABLE model_call_logs ADD COLUMN cached_tokens INTEGER DEFAULT 0")
    except Exception:
        pass

    # Add token breakdown to tasks
    try:
        cursor.execute("ALTER TABLE tasks ADD COLUMN prompt_tokens INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE tasks ADD COLUMN completion_tokens INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE tasks ADD COLUMN cached_tokens INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE tasks ADD COLUMN plan_id TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE task_steps ADD COLUMN generated_files TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE tasks ADD COLUMN task_goal TEXT DEFAULT ''")
    except Exception:
        pass

    conn.commit()
    conn.close()

init_db()

# ── Create indexes for query performance ──
def create_indexes():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_task_steps_task_id ON task_steps(task_id)",
        "CREATE INDEX IF NOT EXISTS idx_task_steps_session_id ON task_steps(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_session_id ON tasks(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_task_type_status ON tasks(task_type, status)",
        "CREATE INDEX IF NOT EXISTS idx_downloads_task_id ON downloads(task_id)",
        "CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status)",
        "CREATE INDEX IF NOT EXISTS idx_downloads_filename ON downloads(filename)",
        "CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_model_call_logs_ts ON model_call_logs(timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_model_call_logs_provider ON model_call_logs(provider)",
        "CREATE INDEX IF NOT EXISTS idx_model_call_logs_model ON model_call_logs(model)",
    ]
    for idx in indexes:
        try:
            cursor.execute(idx)
        except Exception as e:
            print(f"[DB] Index error: {e}")
    conn.commit()
    conn.close()
    print(f"[DB] Created {len(indexes)} indexes")

create_indexes()

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
        cursor.execute(
            "UPDATE tasks SET status='interrupted', interruption_reason='server_restart', "
            "updated_at=CURRENT_TIMESTAMP WHERE status='running'")
        count = cursor.rowcount
        conn.commit()
        conn.close()
        if count:
            print(f"[Startup] Marked {count} running task(s) as interrupted (server restart)")
    except Exception as e:
        print(f"[Startup] Task reconciliation error: {e}")

def _extract_task_title(response: str) -> str:
    """Extract a meaningful task title from the agent's response.
    Uses the first non-empty, non-markdown, non-tool-call line.
    """
    if not response:
        return ""
    lines = response.strip().split('\n')
    for line in lines:
        line = line.strip()
        # Skip markdown headers, code blocks, tool calls, empty lines
        if not line:
            continue
        if line.startswith('```') or line.startswith('#'):
            continue
        if line.startswith('{') or line.startswith('<!--'):
            continue
        if len(line) > 3 and not line.startswith('- '):
            return line[:80]
    return ""

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
                increment_task_resume(tid)
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
def create_task(title: str, user_query: str, task_type: str = 'oneshot',
                schedule_cron: str = None, schedule_enabled: bool = False,
                session_id: int = 1) -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    next_run = None
    if schedule_cron and schedule_enabled:
        try:
            from croniter import croniter
            next_run = croniter(schedule_cron, datetime.now(timezone.utc)).get_next(datetime).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            pass
    cursor.execute(
        "INSERT INTO tasks (title, user_query, task_type, schedule_cron, schedule_enabled, next_run_at, session_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (title, user_query, task_type, schedule_cron, 1 if schedule_enabled else 0, next_run, session_id)
    )
    task_id = cursor.lastrowid
    conn.commit()
    conn.close()
    # Kick off background goal generation
    threading.Thread(target=_generate_task_goal_background, args=(task_id, user_query, session_id), daemon=True).start()
    return task_id

def _generate_task_goal_background(task_id: int, query: str, session_id: int = 1):
    """Use LLM to generate a proper task goal asynchronously."""
    try:
        from core.llm_client import LLMClient
        cfg = load_config()
        llm = LLMClient(default_model=cfg.get("default_model", "moonshot/kimi-latest"))
        resp, _ = llm.chat([{"role": "user", "content": (
            "根据用户的问题，用一句话概括任务目标（简洁、明确、直接）。\n"
            "要求：概括任务要做什么，而不是复述问题本身。\n\n"
            f"用户问题：{query[:300]}\n\n"
            "任务目标："
        )}])
        goal = (resp.choices[0].message.content or "").strip().strip("\"'「」")
        if goal and len(goal) > 10:
            _conn = sqlite3.connect(DB_PATH)
            _conn.execute("UPDATE tasks SET task_goal=? WHERE id=?", (goal[:500], task_id))
            _conn.commit()
            _conn.close()
    except Exception as e:
        print(f"[Task] Goal generation error for task {task_id}: {e}")

def _record_task_deliverables(task_id: int):
    """Extract deliverables from task_steps and update task's result_summary and output_files."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        # Collect generated files from steps
        steps = conn.execute(
            "SELECT tool_name, args_preview, result_preview, full_args, generated_files "
            "FROM task_steps WHERE task_id=? ORDER BY step_number", (task_id,)
        ).fetchall()

        output_files = []
        summaries = []

        for s in steps:
            tool = s["tool_name"] or ""
            args = s["full_args"] or s["args_preview"] or ""
            result = s["result_preview"] or ""
            gen_files = s["generated_files"] or ""

            # Collect written files
            if tool in ("write_file", "edit_file"):
                try:
                    import json as _jj
                    parsed = _jj.loads(args) if isinstance(args, str) else args
                    if isinstance(parsed, dict) and parsed.get("path"):
                        output_files.append(parsed["path"])
                except Exception:
                    pass

            # Collect generated files from steps
            if gen_files:
                try:
                    import json as _jj
                    gf = _jj.loads(gen_files) if isinstance(gen_files, str) else gen_files
                    if isinstance(gf, list):
                        output_files.extend(gf)
                except Exception:
                    pass

            # Collect execution summaries
            if tool == "execute_shell" and result and "Exit Code: 0" in result:
                lines = [l.strip() for l in result.split('\n') if l.strip() and not l.startswith('[')]
                if lines:
                    summaries.append(lines[-1][:200])

            if tool == "queue_download":
                if "下载完成" in result or "Download" in result:
                    summaries.append(result[:200])

        # Deduplicate files
        unique_files = []
        for f in output_files:
            if f and f not in unique_files:
                unique_files.append(f)

        summary_text = "; ".join(summaries[-5:]) if summaries else ""

        update_kwargs = {}
        if summary_text:
            update_kwargs["result_summary"] = summary_text[:1000]
        if unique_files:
            import json as _jj
            update_kwargs["output_files"] = _jj.dumps(unique_files, ensure_ascii=False)

        if update_kwargs:
            fields = [f"{k}=?" for k in update_kwargs]
            values = list(update_kwargs.values()) + [task_id]
            conn.execute(
                f"UPDATE tasks SET {', '.join(fields)}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                values,
            )
            conn.commit()
        conn.close()
    except Exception as e:
        print(f"[Task] Deliverable extraction error: {e}")

def update_task_status(task_id: int, status: str, result_summary: str = None,
                       interruption_reason: str = None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    fields = ["status=?", "updated_at=CURRENT_TIMESTAMP"]
    params = [status]
    if result_summary is not None:
        fields.append("result_summary=?")
        params.append(result_summary)
    if interruption_reason is not None:
        fields.append("interruption_reason=?")
        params.append(interruption_reason)
    params.append(task_id)
    cursor.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id=?", params)
    conn.commit()
    conn.close()

def update_task_type(task_id: int, task_type: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE tasks SET task_type=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (task_type, task_id))
    conn.commit()
    conn.close()


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


def _resolve_todo_for_query(query: str) -> int:
    """
    Determine which todo (if any) this query is continuing.
    Returns todo_id, or 0 for new task.
    """
    # Quick keyword check (no LLM)
    q = query.strip().lower()
    for kw in ["继续", "继续搞", "继续做", "继续下载", "接着", "retry", "continue",
               "再来", "再试", "重新", "唤醒", "恢复", "resume", "next", "yes", "继续做"]:
        if kw and (q.startswith(kw) or q == kw):
            # Continuation of any active todo — return first active
            try:
                from tools.task_plan import load_todos
                for item in load_todos().get("items", []):
                    if item.get("status") in ("doing", "todo"):
                        return item["id"]
            except Exception:
                pass
            return 0

    # Load todos — if none active, no need for LLM
    try:
        from tools.task_plan import load_todos
        todos = load_todos()
        active = [i for i in todos.get("items", []) if i.get("status") in ("doing", "todo")]
    except Exception:
        active = []

    if not active:
        return 0  # No active todos → definitely new task

    # Use LLM to determine association (only when todos exist and query is ambiguous)
    try:
        from core.llm_client import LLMClient
        _model = load_config().get("default_model", "moonshot/kimi-latest")

        todo_lines = "\n".join(f"{i['id']}. {i['desc']} ({i['status']})" for i in active)
        prompt = (
            f"当前待办：\n{todo_lines}\n\n"
            f"用户新输入：「{query[:200]}」\n\n"
            f"回答：如果是续接某个待办，仅回复数字 id；如果无关或全新任务，仅回复 0。"
        )
        llm = LLMClient(default_model=_model)
        resp, _ = llm.chat([{"role": "user", "content": prompt}])
        text = resp.choices[0].message.content.strip()
        # Extract number
        import re
        nums = re.findall(r'\d+', text)
        if nums:
            todo_id = int(nums[0])
            if any(i["id"] == todo_id for i in active):
                return todo_id
    except Exception:
        pass

    return 0


def _resolve_task_goal_via_llm(session_id: int, query: str) -> str:
    """When user confirms a proposal (agent last msg ends with ?), ask LLM to
    extract the task goal. Returns goal string or empty string if not confirming."""
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 10",
            (session_id,)
        ).fetchall()
        conn.close()
        if len(rows) < 1:
            return ""

        # Only trigger if the last agent message ends with a question mark
        last_agent = None
        for r in rows:
            if r[0] == 'agent':
                last_agent = r[1] or ""
                break
        if not last_agent or not last_agent.strip().endswith(('？', '?', '？\n', '?\n', '？"', '?"')):
            return ""

        context_lines = []
        for r in reversed(rows):
            content = (r[1] or "")[:500]
            context_lines.append(f"{r[0]}: {content}")
        context = "\n".join(context_lines)

        prompt = (
            "根据以下对话历史和用户的最后一条回复，判断用户是否在确认或同意某个任务提案。\n"
            "如果是，请直接提取该任务的目标描述（简洁、准确）。\n"
            "如果不是（用户拒绝、提出新需求、闲聊等），请仅回复 NO。\n\n"
            f"对话历史：\n{context}\n"
            f"用户最新回复：{query[:100]}\n\n"
            "回答（任务目标描述或 NO）："
        )

        from core.llm_client import LLMClient
        _cfg = load_config()
        llm = LLMClient(default_model=_cfg.get("default_model", "moonshot/kimi-latest"))
        resp, _ = llm.chat([{"role": "user", "content": prompt}])
        reply = (resp.choices[0].message.content or "").strip()

        if reply.upper() == "NO":
            return ""
        reply = reply.strip("\"'「」")
        return reply[:200]
    except Exception as e:
        print(f"[Task] LLM goal resolution error: {e}")
        return ""


def _resolve_task_for_query(session_id: int, query: str) -> int:
    """
    Determine the task_id for an incoming query BEFORE agent execution.

    Reuses a recent task if this query looks like a continuation; otherwise
    creates a new task. This ensures tool execution always has a valid task_id.
    """
    try:
        db_check = sqlite3.connect(DB_PATH)
        existing = db_check.execute(
            "SELECT id, status, created_at FROM tasks WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (session_id,)
        ).fetchone()
        db_check.close()

        if existing:
            tid, status, created = existing
            if status == 'running':
                print(f"[Task] Reusing running task {tid} for session {session_id}")
                return tid
            elif status in ('completed', 'interrupted', 'backgrounded', 'background_failed'):
                try:
                    from datetime import datetime, timezone, timedelta
                    created_dt = datetime.strptime(created, '%Y-%m-%d %H:%M:%S')
                    created_dt = created_dt.replace(tzinfo=timezone.utc)
                    now = datetime.now(timezone.utc)
                    is_recent = (now - created_dt) < timedelta(minutes=30)
                except Exception:
                    is_recent = False

                if is_recent and len(query.strip()) > 15 and _resolve_todo_for_query(query) > 0:
                    print(f"[Task] Continuing task {tid} for session {session_id} (continuation: {query[:50]})")
                    update_task_status(tid, "running")
                    return tid
    except Exception as e:
        print(f"[Task] Error resolving task: {e}")

    # Resolve task goal — if query is short, use LLM to check if user is confirming a proposal
    task_title = query if len(query.strip()) > 15 else _resolve_task_goal_via_llm(session_id, query)
    if not task_title:
        task_title = _extract_task_title(query) or query[:120]
    if len(task_title) > 120:
        task_title = task_title[:117] + '...'
    tid = create_task(task_title, query, session_id=session_id)
    print(f"[Task] Created task {tid} for session {session_id}")

    # Adopt any orphan shell processes that belong to this session
    try:
        from tools.shell import adopt_orphan_processes
        adopted = adopt_orphan_processes(tid, session_id=session_id)
        if adopted:
            print(f"[Task] Adopted {adopted} orphan process(es) for task {tid}")
    except Exception as e:
        print(f"[Task] Orphan adoption error: {e}")

    return tid


def save_task_context(task_id: int, messages: list):
    """Save agent conversation messages as a JSON snapshot for resume."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Save full context, relying on token_budget.py for pruning instead of hard limits
    snapshot = json.dumps(messages, ensure_ascii=False)
    cursor.execute("UPDATE tasks SET context_snapshot=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (snapshot, task_id))
    conn.commit()
    conn.close()

def get_task_context(task_id: int) -> list:
    """Load saved conversation context for a task, with fallback reconstruction."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT context_snapshot, user_query FROM tasks WHERE id=?", (task_id,))
    row = cursor.fetchone()
    if row and row[0]:
        try:
            ctx = json.loads(row[0])
            if ctx:  # valid non-empty snapshot
                conn.close()
                return ctx
        except Exception:
            pass

    # Fallback: reconstruct context from user_query + task_steps
    # Uses saved tool_call_id and full_args to build API-compatible tool_call/tool pairs
    user_query = row[1] if row else ""
    reconstructed = []
    reconstructed.append({"role": "user", "content": user_query})

    cursor.execute(
        "SELECT tool_name, tool_label, args_preview, result_preview, full_result, success, "
        "tool_call_id, full_args "
        "FROM task_steps WHERE task_id=? ORDER BY created_at ASC", (task_id,))
    steps = cursor.fetchall()
    conn.close()

    if steps:
        for s in steps:
            tool_name = s[0]
            label = s[1] or tool_name
            result = s[4] or s[3] or ""
            tc_id = s[6]
            if not tc_id:
                args_for_id = s[2] or ""
                tc_id = f"call_recon_{args_for_id[:8]}" if args_for_id else f"call_recon_{tool_name}"
            full_args = s[7] or "{}"

            reconstructed.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": full_args
                    }
                }]
            })
            reconstructed.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "name": tool_name,
                "content": result[:5000] if result else "(无输出)"
            })

        print(f"[Task] Reconstructed context for task {task_id} from {len(steps)} step(s)")

    return reconstructed

def increment_task_resume(task_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE tasks SET resume_count = resume_count + 1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (task_id,))
    conn.commit()
    conn.close()

# Run backgrounded task reconciliation now that all helper functions are loaded
reconcile_backgrounded_after_restart()

def add_task_step(task_id: int, step_number: int, tool_name: str, tool_label: str = None,
                  args_preview: str = None, result_preview: str = None, full_result: str = None,
                  success: bool = True, thinking_content: str = None, session_id: int = None,
                  tool_call_id: str = None, full_args: str = None,
                  generated_files: str = None):
    """Record a task step. generated_files is a JSON array of {path, type} objects,
    where type is 'temp' (will be cleaned up) or 'final' (kept after task completes)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO task_steps (task_id, step_number, tool_name, tool_label, args_preview, "
        "result_preview, full_result, success, thinking_content, session_id, tool_call_id, full_args, generated_files) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, step_number, tool_name, tool_label, args_preview, result_preview, full_result,
         1 if success else 0, thinking_content, session_id, tool_call_id, full_args, generated_files or "")
    )
    conn.commit()
    conn.close()

# ==========================================
# Download record helpers
# ==========================================

def create_download_record(type_: str, label: str, repo_id: str = None,
                           filename: str = None, source: str = "huggingface",
                           url: str = None, target_path: str = "",
                           partial_path: str = "", total_size: int = 0,
                           task_id: int = None) -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        '''INSERT INTO downloads (type, label, repo_id, filename, source, url,
           target_path, partial_path, total_size, downloaded_bytes, status, progress, task_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'downloading', 0.0, ?)''',
        (type_, label, repo_id, filename, source, url, target_path, partial_path, total_size, task_id)
    )
    download_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return download_id


def update_download_progress(download_id: int, progress: float,
                              downloaded_bytes: int = None,
                              status: str = None, error_message: str = None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    fields = ["progress=?", "updated_at=CURRENT_TIMESTAMP"]
    params = [progress]
    if downloaded_bytes is not None:
        fields.append("downloaded_bytes=?")
        params.append(downloaded_bytes)
    if status is not None:
        fields.append("status=?")
        params.append(status)
    if error_message is not None:
        fields.append("error_message=?")
        params.append(error_message)
    params.append(download_id)
    cursor.execute(f"UPDATE downloads SET {', '.join(fields)} WHERE id=?", params)
    conn.commit()

    # Log event for important status transitions
    if status == 'completed':
        log_download_event(download_id, "completed", "下载完成", "")
    elif status == 'failed':
        log_download_event(download_id, "failed", "下载失败", error_message or "未知错误")

    # Notify the linked task (both success and failure)
    if status in ('completed', 'failed'):
        try:
            cursor.execute("SELECT task_id, label, filename, target_path FROM downloads WHERE id=?", (download_id,))
            dl_row = cursor.fetchone()
            if dl_row:
                task_id = dl_row[0]
                label = dl_row[1] or dl_row[2] or f"download #{download_id}"
                save_path = dl_row[3] or ""
                if task_id:
                    cursor.execute(
                        "SELECT session_id FROM task_steps WHERE task_id=? AND session_id IS NOT NULL LIMIT 1",
                        (task_id,))
                    sid_row = cursor.fetchone()
                    session_id = sid_row[0] if sid_row else 1

                    if status == 'completed':
                        path_hint = f"\n保存路径: {save_path}" if save_path else ""
                        save_message("system",
                            f"✅ 下载完成: {label}{path_hint}", session_id)
                        try:
                            cursor.execute("SELECT MAX(step_number) FROM task_steps WHERE task_id=?", (task_id,))
                            max_step = cursor.fetchone()[0] or 0
                            add_task_step(task_id, max_step + 1, "queue_download",
                                tool_label=f"✅ 下载完成: {label}",
                                args_preview=f"filename={label}",
                                result_preview="下载完成",
                                full_result="",
                                success=True, session_id=session_id)
                        except Exception as step_err:
                            print(f"[Download] Failed to add completed step for task {task_id}: {step_err}")

                        # Inject "download done" context for background tasks
                        try:
                            cursor.execute(
                                "SELECT status, user_query FROM tasks WHERE id=?",
                                (task_id,))
                            task_row = cursor.fetchone()
                            if task_row and task_row[0] in ('backgrounded',):
                                ctx = get_task_context(task_id)
                                if ctx:
                                    ctx.append({"role": "user", "content": (
                                        "【系统通知】后台下载任务已完成，文件已就绪。"
                                        + (f"\n文件位置: {save_path}" if save_path else "")
                                        + "请继续执行之前未完成的任务，不要重复下载已有文件。"
                                    )})
                                    save_task_context(task_id, ctx)
                                    # Direct resume — wake task immediately instead of waiting for poll
                                    user_query = task_row[1] or ""
                                    print(f"[Download] Download #{download_id} complete — directly resuming task {task_id}")
                                    _direct_resume_background_task(task_id, user_query, ctx, download_id=download_id)
                        except Exception as e:
                            print(f"[Download] Failed to resume task {task_id} after download complete: {e}")

                        _broadcast_to_websockets({
                            "type": "download_success",
                            "download_id": download_id,
                            "task_id": task_id,
                            "session_id": session_id,
                            "label": label
                        })
                    else:  # failed
                        err = error_message or "未知错误"
                        save_message("system",
                            f"❌ 下载失败: {label}\n错误信息: {err}",
                            session_id)
                        try:
                            cursor.execute("SELECT MAX(step_number) FROM task_steps WHERE task_id=?", (task_id,))
                            max_step = cursor.fetchone()[0] or 0
                            add_task_step(task_id, max_step + 1, "queue_download",
                                tool_label=f"❌ 下载失败: {label}",
                                args_preview=f"filename={label}",
                                result_preview=f"错误: {err}",
                                full_result=f"下载失败: {label}\n错误信息: {err}",
                                success=False, session_id=session_id)

                            # Don't mark as background_failed — keep backgrounded for retry analysis
                            cursor.execute(
                                "SELECT status, user_query FROM tasks WHERE id=?",
                                (task_id,))
                            task_row = cursor.fetchone()
                            if task_row and task_row[0] in ('backgrounded', 'completed'):
                                ctx = get_task_context(task_id)
                                if ctx:
                                    ctx.append({"role": "user", "content": (
                                        f"【系统通知】下载任务失败了。\n文件: {label}\n错误信息: {err}\n"
                                        "请分析失败原因，尝试其他方式重新下载（如换源、换文件名），"
                                        "如果确实无法下载则结束任务。"
                                    )})
                                    save_task_context(task_id, ctx)
                                    # Wake the task so agent can analyze and retry
                                    user_query = task_row[1] or ""
                                    print(f"[Download] Download #{download_id} failed — directly resuming task {task_id} for retry analysis")
                                    _direct_resume_background_task(task_id, user_query, ctx, download_id=download_id)
                        except Exception as step_err:
                            print(f"[Download] Failed to update task {task_id}: {step_err}")

                        _broadcast_to_websockets({
                            "type": "download_failed",
                            "download_id": download_id,
                            "task_id": task_id,
                            "session_id": session_id,
                            "label": label,
                            "error": err
                        })
                else:
                    print(f"[Download] download #{download_id} {status}, task_id=NULL (will check at tool_done)")
            else:
                print(f"[Download] download #{download_id} {status}, but no DB row found!")
        except Exception as notify_err:
            print(f"[Download] NOTIFICATION ERROR for #{download_id}: {notify_err}")

    conn.close()


def _direct_resume_background_task(task_id: int, user_query: str, context: list,
                                     download_id: int = None):
    """Directly resume a backgrounded task (thread-safe, non-blocking).
    If download_id is provided, marks background_resumed=1 inside the thread
    so BgMonitor doesn't double-resume."""
    try:
        def _do_resume():
            # Mark as resumed FIRST so monitor won't also pick it up
            if download_id is not None:
                try:
                    _conn = sqlite3.connect(DB_PATH)
                    _conn.execute("UPDATE downloads SET background_resumed=1 WHERE id=?", (download_id,))
                    _conn.commit()
                    _conn.close()
                except Exception:
                    pass
            update_task_status(task_id, "interrupted",
                "后台下载触发恢复", interruption_reason="background_complete")
            _run_background_task(task_id, user_query, context, True)
        threading.Thread(target=_do_resume, daemon=True).start()
    except Exception as e:
        print(f"[Download] _direct_resume_background_task failed for task {task_id}: {e}")


def log_download_event(download_id: int, event_type: str, message: str = "", details: str = ""):
    """Write a structured event to the download_events log table."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO download_events (download_id, event_type, message, details) VALUES (?, ?, ?, ?)",
            (download_id, event_type, message, details)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[Download] Failed to log event #{download_id} {event_type}: {e}")


def get_download_events(download_id: int) -> list:
    """Return all events for a given download, ordered by creation time."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM download_events WHERE download_id=? ORDER BY id ASC", (download_id,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[Download] Failed to get events for #{download_id}: {e}")
        return []


def get_download_record(download_id: int) -> Optional[Dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM downloads WHERE id=?", (download_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return dict(row)
    return None


def list_download_records(status_filter: str = None) -> list:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if status_filter:
        cursor.execute("SELECT * FROM downloads WHERE status=? ORDER BY created_at DESC",
                       (status_filter,))
    else:
        cursor.execute("SELECT * FROM downloads ORDER BY created_at DESC")
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_download_record(download_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM downloads WHERE id=?", (download_id,))
    conn.commit()
    conn.close()


# ==========================================
# Connected WebSocket clients (for background task push)
# ==========================================
connected_websockets: list = []  # List of active WebSocket connections

_sandbox_waits: dict = {}  # {session_id: {"event": threading.Event, "result": dict}} — sandbox auth waits
_pending_sandbox_approvals: dict = {}  # {session_id: [paths]} — late approvals applied on task resume


def _apply_pending_sandbox_approvals(agent, session_id):
    """Load pending sandbox approvals into agent's session whitelist."""
    paths = _pending_sandbox_approvals.pop(session_id, [])
    for p in paths:
        action = p.get("action", "approve_once")
        path = p.get("path", "")
        if action in ("approve_once", "approve_dir", "approve_always", "approve_session") and path:
            import os as _ap_os
            agent._session_sandbox_whitelist.add(path)
            agent._session_sandbox_whitelist.add(_ap_os.path.dirname(_ap_os.path.abspath(path)))
            print(f"[Sandbox] Loaded pending approval: {path}")

_active_agents: dict = {}  # {session_id: {task_id: OpenAGCAgent}} — multi-task concurrent support
_background_agents: dict = {}  # {task_id: OpenAGCAgent} — background tasks for interrupt

_session_enabled_tools: dict = {}  # {session_id: set(tool_names)} — progressive tool persistence

_guardian_resume_lock = threading.Lock()  # Prevents concurrent Phase 2 executions

def _broadcast_to_websockets(data: dict):
    """Send data to all connected WebSocket clients."""
    import asyncio
    dead = []
    for ws in connected_websockets:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(ws.send_json(data), loop=loop)
        except Exception:
            dead.append(ws)
    for ws in dead:
        try: connected_websockets.remove(ws)
        except ValueError: pass

# ==========================================
# Llama download progress tracking
# ==========================================
_llamacpp_download_state = {
    "active": False,
    "type": "",      # "binary" or "model"
    "label": "",     # human-readable label
    "progress": 0.0, # 0.0 .. 1.0
    "stage": "",     # "downloading", "extracting", "complete", "error"
    "error": "",     # error message if stage == "error"
    "cancelled": False # set to True to stop a running download
}



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

class ConfigUpdate(BaseModel):
    api_keys: Dict[str, str]
    default_model: str
    fallback_models: List[str]
    disabled_skills: List[str]
    sandbox_mode: bool
    sandbox_dir: str
    llamacpp_ctx_size: int = 32768
    browser_headless: bool = False
    http_proxy: str = ""
    heartbeat_enabled: bool
    heartbeat_interval: int
    email_listener_enabled: bool
    email_account: str
    email_password: str
    email_imap_server: str
    email_smtp_server: str
    owner_email: str
    mcp_servers: Optional[Dict[str, Any]] = None
    session_id: Optional[int] = None  # Target session for email config
    tool_permissions: Optional[Dict[str, Any]] = None
    searxng_url: str = ""
    searxng_port: int = 8888
    max_correction_attempts: int = 5

@app.get("/api/settings")
async def get_settings(session_id: int = None):
    """Return current configuration. If session_id provided, include per-session email config."""
    config = load_config()

    # Mask API keys before sending to frontend
    masked_keys = {}
    for k, v in config.get("api_keys", {}).items():
        if v:
            masked_keys[k] = f"{v[:3]}...{v[-3:]}" if len(v) > 6 else "***"
        else:
            masked_keys[k] = ""

    # Fetch per-session email config if session_id given
    sess_email = {}
    if session_id is not None:
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT email_enabled, email_account, email_password, email_imap_server, "
                "email_smtp_server, owner_email FROM sessions WHERE id=?", (session_id,))
            row = cur.fetchone()
            conn.close()
            if row:
                sess_email = dict(row)
                sess_email["email_listener_enabled"] = bool(sess_email.pop("email_enabled", 0))
                if sess_email.get("email_password"):
                    sess_email["email_password"] = "***"
        except Exception as e:
            print(f"[Settings] Session email load error: {e}")

    return {
        "api_keys_masked": masked_keys,
        "default_model": config.get("default_model", "moonshot/kimi-latest"),
        "fallback_models": config.get("fallback_models", []),
        "disabled_skills": config.get("disabled_skills", []),
        "sandbox_mode": config.get("sandbox_mode", True),
        "sandbox_dir": config.get("sandbox_dir", os.path.abspath(os.path.join(os.getcwd(), "workspace"))),
        "llamacpp_ctx_size": config.get("llamacpp_ctx_size", 32768),
        "browser_headless": config.get("browser_headless", False),
        "http_proxy": config.get("http_proxy", ""),
        "heartbeat_enabled": config.get("heartbeat_enabled", False),
        "heartbeat_interval": config.get("heartbeat_interval", 60),
        "email_listener_enabled": sess_email.get("email_listener_enabled", config.get("email_listener_enabled", False)),
        "email_account": sess_email.get("email_account", config.get("email_account", "")),
        "email_password": sess_email.get("email_password", ("***" if config.get("email_password") else "")),
        "email_imap_server": sess_email.get("email_imap_server", config.get("email_imap_server", "")),
        "email_smtp_server": sess_email.get("email_smtp_server", config.get("email_smtp_server", "")),
        "owner_email": sess_email.get("owner_email", config.get("owner_email", "")),
        "allowed_paths": config.get("allowed_paths", []),
        "denied_paths": config.get("denied_paths", []),
        "tool_permissions": config.get("tool_permissions", {}),
        "searxng_url": config.get("searxng_url", ""),
        "searxng_port": config.get("searxng_port", 8888),
        "max_correction_attempts": config.get("max_correction_attempts", 5),
    }

@app.post("/api/settings")
async def update_settings(config_update: ConfigUpdate):
    """Update JSON config and set env vars dynamically."""
    config = load_config()
    env_file = get_data_path(".env")
    if not os.path.exists(env_file):
        open(env_file, 'a').close()

    # Mapping from our internal provider key to litellm's expected env var name
    PROVIDER_ENV_MAP = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "kimi": "MOONSHOT_API_KEY",
        "glm": "ZAI_API_KEY",
        "minimax": "MINIMAX_API_KEY",
        "llamacpp": "LLAMACPP_API_BASE",
        "huggingface": "HF_TOKEN",
        "tavily": "TAVILY_API_KEY",
        "brave_search": "BRAVE_SEARCH_API_KEY",
        "searxng": "SEARXNG_API_KEY"
    }

    try:
        # Update keys
        current_keys = config.get("api_keys", {})
        for provider, new_key in config_update.api_keys.items():
            if new_key and not new_key.endswith("***"):
                current_keys[provider] = new_key
                env_key_name = PROVIDER_ENV_MAP.get(provider, f"{provider.upper()}_API_KEY")
                set_key(env_file, env_key_name, new_key)
                os.environ[env_key_name] = new_key

        # Set China-specific API base URLs for litellm
        if current_keys.get("kimi"):
            os.environ["MOONSHOT_API_BASE"] = "https://api.moonshot.cn/v1"
            set_key(env_file, "MOONSHOT_API_BASE", "https://api.moonshot.cn/v1")
        if current_keys.get("minimax"):
            os.environ["MINIMAX_API_BASE"] = "https://api.minimax.io/v1"
            set_key(env_file, "MINIMAX_API_BASE", "https://api.minimax.io/v1")

        config["api_keys"] = current_keys
        config["default_model"] = config_update.default_model
        config["fallback_models"] = config_update.fallback_models
        config["disabled_skills"] = config_update.disabled_skills
        config["sandbox_mode"] = config_update.sandbox_mode
        config["sandbox_dir"] = os.path.abspath(config_update.sandbox_dir) if config_update.sandbox_dir else os.path.abspath(os.path.join(os.getcwd(), "workspace"))
        config["llamacpp_ctx_size"] = config_update.llamacpp_ctx_size
        config["browser_headless"] = config_update.browser_headless
        config["http_proxy"] = config_update.http_proxy
        config["heartbeat_enabled"] = config_update.heartbeat_enabled
        config["heartbeat_interval"] = config_update.heartbeat_interval
        config["email_listener_enabled"] = config_update.email_listener_enabled
        config["email_account"] = config_update.email_account
        if config_update.email_password != "***":
            config["email_password"] = config_update.email_password
        config["email_imap_server"] = config_update.email_imap_server
        config["email_smtp_server"] = config_update.email_smtp_server
        config["owner_email"] = config_update.owner_email
        if config_update.mcp_servers is not None:
            config["mcp_servers"] = config_update.mcp_servers
        if config_update.tool_permissions is not None:
            config["tool_permissions"] = config_update.tool_permissions
        config["searxng_url"] = config_update.searxng_url
        config["searxng_port"] = config_update.searxng_port
        set_key(env_file, "SEARXNG_URL", config_update.searxng_url)
        config["max_correction_attempts"] = config_update.max_correction_attempts
        os.environ["SEARXNG_URL"] = config_update.searxng_url

        # Save per-session email config when session_id is provided
        if config_update.session_id is not None:
            try:
                db_conn = sqlite3.connect(DB_PATH)
                email_password_val = config_update.email_password
                if email_password_val == "***":
                    # Keep existing password when masked
                    cur = db_conn.execute(
                        "SELECT email_password FROM sessions WHERE id=?",
                        (config_update.session_id,))
                    row = cur.fetchone()
                    email_password_val = row[0] if row and row[0] else ""
                db_conn.execute(
                    "UPDATE sessions SET email_enabled=?, email_account=?, email_password=?, "
                    "email_imap_server=?, email_smtp_server=?, owner_email=?, "
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (1 if config_update.email_listener_enabled else 0,
                     config_update.email_account, email_password_val,
                     config_update.email_imap_server, config_update.email_smtp_server,
                     config_update.owner_email, config_update.session_id))
                db_conn.commit()
                db_conn.close()
            except Exception as e:
                print(f"[Settings] Session email save error: {e}")
        
        set_key(env_file, "DEFAULT_MODEL", config_update.default_model)
        os.environ["DEFAULT_MODEL"] = config_update.default_model

        # Save to JSON
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)
            
        load_dotenv(override=True)
        return {"status": "success", "message": "Settings updated successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

import requests
@app.get("/api/provider-models")
async def get_provider_models(provider: str):
    """Query the actual provider API to get a list of available models, or fallback to defaults."""
    config = load_config()
    api_keys = config.get("api_keys", {})
    models = []
    
    if provider == "gemini":
        key = api_keys.get("gemini")
        if key:
            try:
                res = requests.get(f"https://generativelanguage.googleapis.com/v1beta/models?key={key}", timeout=5)
                if res.status_code == 200:
                    models = [m["name"].replace("models/", "gemini/") for m in res.json().get("models", []) if "gemini" in m["name"] or "pro" in m["name"] or "flash" in m["name"]]
            except Exception: pass
    elif provider == "openai":
        key = api_keys.get("openai")
        if key:
            try:
                headers = {"Authorization": f"Bearer {key}"}
                res = requests.get("https://api.openai.com/v1/models", headers=headers, timeout=5)
                if res.status_code == 200:
                    models = [m["id"] for m in res.json().get("data", []) if "gpt" in m["id"]]
            except Exception: pass
    elif provider == "llamacpp":
        manager = get_llamacpp_manager()
        models = [f"llamacpp/{m}" for m in manager.list_models()]
        if not models:
            models = ["llamacpp/local-model (Not Installed)"]
    elif provider == "deepseek":
        key = api_keys.get("deepseek")
        if key:
            try:
                headers = {"Authorization": f"Bearer {key}"}
                res = requests.get("https://api.deepseek.com/v1/models", headers=headers, timeout=5)
                if res.status_code == 200:
                    models = [f"deepseek/{m['id']}" for m in res.json().get("data", [])]
            except Exception: pass


    # Fallback default models if API call fails or key not set
    # Model names include litellm provider prefix as required by litellm.completion()
    if not models:
        defaults = {
            'openai': ['gpt-4o', 'gpt-4o-mini', 'gpt-4-turbo', 'gpt-3.5-turbo'],
            'anthropic': ['claude-3-5-sonnet-20240620', 'claude-3-opus-20240229', 'claude-3-haiku-20240307'],
            'deepseek': ['deepseek/deepseek-chat', 'deepseek/deepseek-reasoner'],
            'gemini': ['gemini/gemini-1.5-pro', 'gemini/gemini-2.5-pro-preview-05-06'],
            'kimi': ['moonshot/kimi-k2.6', 'moonshot/kimi-k2.5', 'moonshot/kimi-latest', 'moonshot/moonshot-v1-8k', 'moonshot/moonshot-v1-32k', 'moonshot/moonshot-v1-128k'],
            'glm': ['zai/glm-4.7', 'zai/glm-4.5', 'zai/glm-4.5-flash', 'zai/glm-4.5-air'],
            'minimax': ['minimax/MiniMax-M2.1'],
            'llamacpp': ['llamacpp/local-model (需先下载 GGUF 模型)'],
        }
        models = defaults.get(provider, [])
        
    models.sort()
    models.sort()
    return {"models": models}

@app.get("/api/stats/token_usage")
async def get_token_usage_stats(provider: str, days: int = 30):
    """Get historical token usage stats for a specific provider."""
    from core.stats_manager import get_stats_manager
    manager = get_stats_manager(DB_PATH)
    history = manager.get_usage_history(provider, days)
    return {"status": "success", "data": history}

@app.post("/api/sandbox/approve")
async def approve_sandbox_request(body: dict):
    """Approve a pending sandbox path access request — adds path to allowed_paths."""
    path = (body.get("path") or "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="path is required")

    config = load_config()
    allowed = config.get("allowed_paths", [])
    if isinstance(allowed, str):
        try:
            import json as _j
            allowed = _j.loads(allowed)
        except Exception:
            allowed = []

    abs_p = os.path.abspath(os.path.expandvars(path))
    if abs_p not in allowed:
        allowed.append(abs_p)

    config["allowed_paths"] = allowed
    config["pending_path_requests"] = [
        r for r in config.get("pending_path_requests", [])
        if os.path.abspath(os.path.expandvars(r.get("path", ""))) != abs_p
    ]

    import json as _j
    config_path = get_data_path("config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        _j.dump(config, f, ensure_ascii=False, indent=2)

    return {"ok": True, "allowed_paths": allowed}

@app.post("/api/sandbox/remove-path")
async def remove_sandbox_path(body: dict):
    """Remove a path from allowed_paths or denied_paths."""
    path = (body.get("path") or "").strip()
    list_type = body.get("type", "allowed")  # "allowed" or "denied"
    if not path:
        raise HTTPException(status_code=400, detail="path is required")

    config = load_config()
    key = "allowed_paths" if list_type == "allowed" else "denied_paths"
    paths = config.get(key, [])
    if isinstance(paths, str):
        try:
            import json as _j
            paths = _j.loads(paths)
        except Exception:
            paths = []

    abs_p = os.path.abspath(os.path.expandvars(path))
    # Remove by matching absolute path
    paths = [p for p in paths if os.path.abspath(os.path.expandvars(p)) != abs_p]
    config[key] = paths

    import json as _j
    config_path = get_data_path("config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        _j.dump(config, f, ensure_ascii=False, indent=2)

    return {"ok": True, key: paths}

@app.post("/api/sandbox/remove-permission")
async def remove_tool_permission(body: dict):
    """Remove a tool permission entry."""
    category = (body.get("category") or "").strip()
    key_name = body.get("key", "")  # specific key in the category, or empty to remove whole category
    if not category:
        raise HTTPException(status_code=400, detail="category is required")

    config = load_config()
    perms = config.get("tool_permissions", {})
    if isinstance(perms, str):
        try:
            import json as _j
            perms = _j.loads(perms)
        except Exception:
            perms = {}

    if key_name:
        if category in perms and isinstance(perms[category], dict):
            perms[category].pop(key_name, None)
            if not perms[category]:
                perms.pop(category, None)
    else:
        perms.pop(category, None)

    config["tool_permissions"] = perms
    import json as _j
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        _j.dump(config, f, ensure_ascii=False, indent=2)

    return {"ok": True, "tool_permissions": perms}

class PullRequest(BaseModel) :
    model_name: str
    tool: str = "huggingface" # "huggingface" or "modelscope"



@app.get("/api/llamacpp/status")
async def get_llamacpp_status():
    """Get the status of the local llama-server (includes download progress)."""
    manager = get_llamacpp_manager()
    return {
        "installed": manager.is_binary_installed(),
        "running": manager.is_running(),
        "models": manager.list_models(),
        "port": manager.port,
        "download": _llamacpp_download_state
    }

@app.post("/api/llamacpp/setup")
async def setup_llamacpp():
    """Download and install the llama-server binary (runs in background with progress)."""
    global _llamacpp_download_state

    if _llamacpp_download_state["active"]:
        raise HTTPException(status_code=409, detail="下载任务正在进行中")

    _llamacpp_download_state = {
        "active": True,
        "type": "binary",
        "label": "正在下载 llama.cpp 二进制文件...",
        "progress": 0.0,
        "stage": "downloading",
        "error": "",
        "cancelled": False
    }

    manager = get_llamacpp_manager()
    bin_path = manager.exe_path
    db_download_id = create_download_record(
        type_="binary",
        label="llama.cpp 二进制文件",
        source="binary",
        url="https://api.github.com/repos/ggerganov/llama.cpp/releases/latest",
        target_path=bin_path
    )
    _llamacpp_download_state["download_id"] = db_download_id

    def run_download():
        global _llamacpp_download_state
        dl_id = db_download_id
        try:
            def progress_cb(ratio):
                if _llamacpp_download_state.get("cancelled"):
                    return
                _llamacpp_download_state["progress"] = ratio
                _llamacpp_download_state["label"] = "正在下载 llama.cpp 二进制文件..."
                _llamacpp_download_state["stage"] = "downloading"
                update_download_progress(dl_id, ratio)
                _broadcast_to_websockets({
                    "type": "llamacpp_download",
                    "task": "binary",
                    "label": "正在下载 llama.cpp 二进制文件...",
                    "progress": ratio,
                    "stage": "downloading"
                })

            manager2 = get_llamacpp_manager()
            if _llamacpp_download_state.get("cancelled"):
                return
            _llamacpp_download_state["stage"] = "extracting"
            _llamacpp_download_state["label"] = "正在解压..."
            _broadcast_to_websockets({
                "type": "llamacpp_download",
                "task": "binary",
                "label": "正在解压 llama.cpp...",
                "progress": 1.0,
                "stage": "extracting"
            })

            success = manager2.download_binary(progress_callback=progress_cb)
            if success:
                _llamacpp_download_state = {**_llamacpp_download_state, "active": False, "stage": "complete", "progress": 1.0}
                update_download_progress(dl_id, 1.0, status="completed")
                _broadcast_to_websockets({
                    "type": "llamacpp_download",
                    "task": "binary",
                    "label": "llama.cpp 安装完成",
                    "progress": 1.0,
                    "stage": "complete"
                })
            else:
                _llamacpp_download_state = {**_llamacpp_download_state, "active": False, "stage": "error", "error": "下载失败"}
                update_download_progress(dl_id, 0.0, status="failed", error_message="下载失败")
                _broadcast_to_websockets({
                    "type": "llamacpp_download",
                    "task": "binary",
                    "label": "安装失败",
                    "progress": 0.0,
                    "stage": "error",
                    "error": "下载失败"
                })
        except Exception as e:
            _llamacpp_download_state = {**_llamacpp_download_state, "active": False, "stage": "error", "error": str(e)}
            update_download_progress(dl_id, 0.0, status="failed", error_message=str(e))
            _broadcast_to_websockets({
                "type": "llamacpp_download",
                "task": "binary",
                "label": f"安装失败: {e}",
                "progress": 0.0,
                "stage": "error",
                "error": str(e)
            })

    thread = threading.Thread(target=run_download, daemon=True)
    thread.start()
    return {"status": "started", "message": "开始下载安装 llama.cpp"}

class ModelDownloadRequest(BaseModel):
    url: str
    filename: str

@app.post("/api/llamacpp/download-model")
async def download_llamacpp_model(req: ModelDownloadRequest):
    """Download a GGUF model."""
    loop = asyncio.get_event_loop()
    manager = get_llamacpp_manager()
    success = await loop.run_in_executor(None, manager.download_model, req.url, req.filename)
    if success:
        return {"status": "success", "message": f"Model {req.filename} downloaded successfully"}
    else:
        raise HTTPException(status_code=500, detail="Failed to download model")

class ModelSearchRequest(BaseModel):
    query: str
    source: str = "huggingface"  # "huggingface" or "modelscope"

@app.post("/api/llamacpp/search-models")
async def search_llamacpp_models(req: ModelSearchRequest):
    """Search for GGUF models by name from HuggingFace or ModelScope."""
    loop = asyncio.get_event_loop()
    manager = get_llamacpp_manager()
    if req.source == "modelscope":
        results = await loop.run_in_executor(None, manager.search_ms_models, req.query)
    else:
        results = await loop.run_in_executor(None, manager.search_hf_models, req.query)
    return {"status": "success", "models": results}

class ModelFilesRequest(BaseModel):
    repo_id: str
    source: str = "huggingface"

@app.post("/api/llamacpp/model-files")
async def get_llamacpp_model_files(req: ModelFilesRequest):
    """List GGUF files in a model repository (HF or ModelScope)."""
    loop = asyncio.get_event_loop()
    manager = get_llamacpp_manager()
    if req.source == "modelscope":
        files = await loop.run_in_executor(None, manager.get_ms_model_files, req.repo_id)
    else:
        files = await loop.run_in_executor(None, manager.get_hf_model_files, req.repo_id)
    return {"status": "success", "files": files}

class ModelDownloadHFRequest(BaseModel):
    repo_id: str
    filename: str
    source: str = "huggingface"

@app.post("/api/llamacpp/download-from-hf")
async def download_llamacpp_from_hf(req: ModelDownloadHFRequest):
    """Download a GGUF model from HuggingFace or ModelScope (runs in background with progress, supports resume)."""
    global _llamacpp_download_state

    if _llamacpp_download_state["active"]:
        raise HTTPException(status_code=409, detail="下载任务正在进行中")

    short_name = req.filename.split("/")[-1]
    source_label = "ModelScope" if req.source == "modelscope" else "HuggingFace"

    # Check for existing partial file to report initial progress
    manager = get_llamacpp_manager()
    partial_path = os.path.join(manager.models_dir, short_name + ".partial")
    target_path = os.path.join(manager.models_dir, short_name)
    resume_offset = 0
    if os.path.exists(partial_path):
        resume_offset = os.path.getsize(partial_path)
        initial_label = f"续传 {short_name} (已下载 {resume_offset / 1024**2:.0f} MB)..."
    else:
        initial_label = f"正在从 {source_label} 下载 {short_name}..."

    # Try to get total size via HEAD request before starting thread
    total_size = 0
    try:
        if req.source == "modelscope":
            head_url = f"{manager.MS_API_BASE}/models/{req.repo_id}/resolve/master/{req.filename}"
        else:
            head_url = f"https://huggingface.co/{req.repo_id}/resolve/main/{req.filename}"
        head_resp = requests.head(head_url, timeout=10)
        total_size = int(head_resp.headers.get("Content-Length", 0))
    except Exception:
        pass

    # Create persistent DB record
    db_download_id = create_download_record(
        type_="model",
        label=f"{short_name} ({source_label})",
        repo_id=req.repo_id,
        filename=req.filename,
        source=req.source,
        target_path=target_path,
        partial_path=partial_path,
        total_size=total_size
    )

    _llamacpp_download_state = {
        "active": True,
        "type": "model",
        "label": initial_label,
        "progress": 0.0,
        "stage": "downloading",
        "error": "",
        "repo_id": req.repo_id,
        "filename": req.filename,
        "source": req.source,
        "resume_offset": resume_offset,
        "download_id": db_download_id,
        "cancelled": False
    }

    def run_download():
        global _llamacpp_download_state
        dl_id = db_download_id
        try:
            def progress_cb(ratio):
                if _llamacpp_download_state.get("cancelled"):
                    return
                _llamacpp_download_state["progress"] = ratio
                _llamacpp_download_state["label"] = f"正在下载 {short_name}..."
                _llamacpp_download_state["stage"] = "downloading"
                update_download_progress(dl_id, ratio)
                _broadcast_to_websockets({
                    "type": "llamacpp_download",
                    "task": "model",
                    "label": f"正在下载 {short_name}...",
                    "progress": ratio,
                    "stage": "downloading"
                })

            manager2 = get_llamacpp_manager()
            if _llamacpp_download_state.get("cancelled"):
                return
            if req.source == "modelscope":
                success = manager2.download_model_from_ms(req.repo_id, req.filename, progress_callback=progress_cb)
            else:
                success = manager2.download_model_from_hf(req.repo_id, req.filename, progress_callback=progress_cb)

            if success:
                _llamacpp_download_state = {**_llamacpp_download_state, "active": False, "stage": "complete", "progress": 1.0}
                update_download_progress(dl_id, 1.0, status="completed")
                _broadcast_to_websockets({
                    "type": "llamacpp_download",
                    "task": "model",
                    "label": f"{short_name} 下载完成",
                    "progress": 1.0,
                    "stage": "complete"
                })
            else:
                _llamacpp_download_state = {**_llamacpp_download_state, "active": False, "stage": "error", "error": "下载中断，可重新下载自动续传"}
                update_download_progress(dl_id, 0.0, status="failed", error_message="下载中断，可重新下载自动续传")
                _broadcast_to_websockets({
                    "type": "llamacpp_download",
                    "task": "model",
                    "label": f"{short_name} 下载中断 (已保存进度，可重新下载续传)",
                    "progress": 0.0,
                    "stage": "error",
                    "error": "下载中断，可重新下载自动续传"
                })
        except Exception as e:
            _llamacpp_download_state = {**_llamacpp_download_state, "active": False, "stage": "error", "error": str(e)}
            update_download_progress(dl_id, 0.0, status="failed", error_message=str(e))
            _broadcast_to_websockets({
                "type": "llamacpp_download",
                "task": "model",
                "label": f"下载失败: {e}",
                "progress": 0.0,
                "stage": "error",
                "error": str(e)
            })

    thread = threading.Thread(target=run_download, daemon=True)
    thread.start()
    return {"status": "started", "message": f"开始从 {source_label} 下载 {short_name}", "resume_offset": resume_offset}

# ==========================================
# Download History API
# ==========================================

@app.get("/api/downloads")
async def get_downloads(status: str = None):
    """List all download records with optional status filter."""
    records = list_download_records(status_filter=status)
    return {"downloads": records}


@app.get("/api/downloads/{download_id}/events")
async def get_download_events_endpoint(download_id: int):
    """Get the event log for a specific download."""
    record = get_download_record(download_id)
    if not record:
        raise HTTPException(status_code=404, detail="Download record not found")
    events = get_download_events(download_id)
    return {"download_id": download_id, "events": events}


class ResumeDownloadResponse(BaseModel):
    status: str
    message: str
    download_id: int = None


@app.post("/api/downloads/{download_id}/resume")
async def resume_download(download_id: int):
    """Resume a paused or failed download."""
    global _llamacpp_download_state

    if _llamacpp_download_state["active"]:
        raise HTTPException(status_code=409, detail="下载任务正在进行中")

    record = get_download_record(download_id)
    if not record:
        raise HTTPException(status_code=404, detail="Download record not found")
    if record["status"] not in ("paused", "failed"):
        raise HTTPException(status_code=400, detail=f"Cannot resume download in status '{record['status']}'")

    partial_path = record["partial_path"]
    resume_offset = os.path.getsize(partial_path) if partial_path and os.path.exists(partial_path) else 0
    short_name = (record["filename"] or record["label"] or "file").split("/")[-1]

    _llamacpp_download_state = {
        "active": True,
        "type": record["type"],
        "label": f"续传 {short_name}",
        "progress": record["progress"] or 0.0,
        "stage": "downloading",
        "error": "",
        "repo_id": record["repo_id"],
        "filename": record["filename"],
        "source": record["source"],
        "resume_offset": resume_offset,
        "download_id": download_id,
        "cancelled": False
    }

    update_download_progress(download_id, record["progress"] or 0.0,
                             downloaded_bytes=resume_offset,
                             status="downloading", error_message="")

    def run_resume():
        global _llamacpp_download_state
        dl_id = download_id
        try:
            log_download_event(dl_id, "resumed", f"续传: {short_name}",
                               f"resume_offset={resume_offset}")
            def progress_cb(ratio):
                if _llamacpp_download_state.get("cancelled"):
                    return
                _llamacpp_download_state["progress"] = ratio
                _llamacpp_download_state["label"] = f"续传 {short_name}..."
                _llamacpp_download_state["stage"] = "downloading"
                update_download_progress(dl_id, ratio)
                _broadcast_to_websockets({
                    "type": "llamacpp_download",
                    "task": record["type"],
                    "label": f"续传 {short_name}...",
                    "progress": ratio,
                    "stage": "downloading"
                })

            manager = get_llamacpp_manager()
            if _llamacpp_download_state.get("cancelled"):
                return
            if record["type"] == "binary":
                success = manager.download_binary(progress_callback=progress_cb)
            elif record["source"] == "modelscope":
                success = manager.download_model_from_ms(
                    record["repo_id"], record["filename"], progress_callback=progress_cb
                )
            elif record["url"] and not record["repo_id"]:
                # Direct URL download (use url from record)
                from urllib.parse import urlparse
                fname = record["filename"] or os.path.basename(urlparse(record["url"]).path) or "download"
                success = manager.download_model(
                    record["url"], fname, progress_callback=progress_cb, resume=True
                )
            else:
                success = manager.download_model_from_hf(
                    record["repo_id"], record["filename"], progress_callback=progress_cb
                )

            if success:
                _llamacpp_download_state = {**_llamacpp_download_state, "active": False, "stage": "complete", "progress": 1.0}
                update_download_progress(dl_id, 1.0, status="completed")
                _broadcast_to_websockets({
                    "type": "llamacpp_download",
                    "task": record["type"],
                    "label": f"{short_name} 下载完成",
                    "progress": 1.0,
                    "stage": "complete"
                })
            else:
                _llamacpp_download_state = {**_llamacpp_download_state, "active": False, "stage": "error", "error": "下载失败"}
                update_download_progress(dl_id, 0.0, status="failed", error_message="下载失败")
                _broadcast_to_websockets({
                    "type": "llamacpp_download",
                    "task": record["type"],
                    "label": f"{short_name} 下载失败",
                    "progress": 0.0,
                    "stage": "error",
                    "error": "下载失败"
                })
        except Exception as e:
            _llamacpp_download_state = {**_llamacpp_download_state, "active": False, "stage": "error", "error": str(e)}
            update_download_progress(dl_id, 0.0, status="failed", error_message=str(e))
            _broadcast_to_websockets({
                "type": "llamacpp_download",
                "task": record["type"],
                "label": f"续传失败: {e}",
                "progress": 0.0,
                "stage": "error",
                "error": str(e)
            })

    thread = threading.Thread(target=run_resume, daemon=True)
    thread.start()
    return {"status": "started", "message": f"Resuming download of {short_name}", "download_id": download_id}


@app.delete("/api/downloads/{download_id}")
async def delete_download(download_id: int):
    """Delete a download record and its associated .partial file."""
    record = get_download_record(download_id)
    if not record:
        raise HTTPException(status_code=404, detail="Download record not found")

    partial_path = record.get("partial_path", "")
    if partial_path and os.path.exists(partial_path):
        try:
            os.remove(partial_path)
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"Failed to delete partial file: {e}")

    delete_download_record(download_id)
    return {"status": "success", "message": "Download record deleted"}



# ── Plugin Management Endpoints ──
@app.get("/api/plugins")
async def get_plugins():
    return {"plugins": list_all_plugins(_plugins_dir), "plugins_dir": os.path.abspath(_plugins_dir)}

@app.post("/api/plugins/scan")
async def scan_plugins():
    global _plugins
    _plugins = discover_plugins(plugins_dir=_plugins_dir, broadcast_fn=_broadcast_to_websockets if "_broadcast_to_websockets" in dir() else None, server_config=load_config())
    _mount_plugins(app, _plugins)
    return {"status": "ok", "count": len(_plugins), "plugins": list_plugins()}

@app.post("/api/plugins/{name}/toggle")
async def plugin_toggle(name: str):
    new_state = toggle_plugin(name, _plugins_dir)
    return {"status": "ok", "enabled": new_state.get("enabled", True)}

@app.post("/api/plugins/install")
async def plugin_install(req: Request):
    import json as _json
    body = await req.json()
    name, url = body.get("name", ""), body.get("url", "")
    if not name or not url: raise HTTPException(status_code=400, detail="name and url required")
    ok = install_from_git(name, url, _plugins_dir)
    if not ok: raise HTTPException(status_code=500, detail="Install failed")
    return {"status": "ok", "message": f"Plugin {name} installed"}

@app.delete("/api/plugins/{name}")
async def plugin_delete(name: str):
    import shutil
    d = os.path.join(_plugins_dir, name)
    if not os.path.isdir(d): raise HTTPException(status_code=404, detail=f"Plugin not found: {name}")
    unload_plugin(name); shutil.rmtree(d)
    return {"status": "ok", "message": f"Plugin {name} deleted"}

@app.get("/api/marketplace")
async def get_marketplace():
    data = fetch_marketplace()
    return {"marketplace": data}





class LlamaControlRequest(BaseModel):
    action: str
    model: Optional[str] = None

@app.post("/api/llamacpp/control")
async def control_llamacpp(req: LlamaControlRequest):
    """Control the llama-server process."""
    import time
    manager = get_llamacpp_manager()
    if req.action == "start":
        if not req.model:
            raise HTTPException(status_code=400, detail="Model filename required to start")
        success = manager.start(req.model)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to start llama-server process")
        return {"status": "success", "message": "Server start command issued"}
    elif req.action == "stop":
        manager.stop()
        return {"status": "success", "message": "Server stop command issued"}
    elif req.action == "restart":
        manager.stop()
        time.sleep(1)
        if req.model:
            manager.start(req.model)
        return {"status": "success", "message": "Server restart command issued"}
    else:
        raise HTTPException(status_code=400, detail="Invalid action")

@app.get("/api/skills")
async def get_skills():
    """List available skills with details."""
    from core.skill_manager import SkillManager
    manager = SkillManager()
    skills = manager.list_skills()
    
    config = load_config()
    disabled = config.get("disabled_skills", [])
    
    for s in skills:
        s["enabled"] = s.get("filename", "") not in disabled
    
    return {"skills": skills}


@app.post("/api/skills/import")
async def import_skill(data: dict):
    """Import a skill file with security validation."""
    from core.skill_manager import SkillManager
    manager = SkillManager()
    
    filename = data.get("filename", "")
    content = data.get("content", "")
    force = data.get("force", False)
    
    if not filename or not content:
        raise HTTPException(status_code=400, detail="filename and content are required")
    
    result = manager.import_skill(filename, content, force=force)
    return result


@app.post("/api/skills/validate")
async def validate_skill(data: dict):
    """Validate a skill for security without importing."""
    from core.memory_store import get_memory_store, LongTermMemory
    from core.skill_manager import get_skill_manager
    from core.llamacpp_manager import get_llamacpp_manager
    manager = SkillManager()
    content = data.get("content", "")
    return manager.validate_skill(content)


@app.get("/api/skills/{filename}")
async def get_skill_content(filename: str):
    """Get the content of a specific skill."""
    filepath = os.path.join(get_skills_dir(), filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Skill not found")
    with open(filepath, 'r', encoding='utf-8') as f:
        return {"filename": filename, "content": f.read()}


@app.delete("/api/skills/{filename}")
async def delete_skill(filename: str):
    """Delete a skill file."""
    from core.skill_manager import SkillManager
    manager = SkillManager()
    if manager.delete_skill(filename):
        # Rebuild SkillStore index so deleted skill no longer appears in retrieval
        try:
            from core.skill_store import SkillStore
            SkillStore().build_index()
        except Exception as e:
            print(f"[API] SkillStore index rebuild after deletion failed: {e}")
        return {"success": True, "message": f"Skill '{filename}' deleted."}
    raise HTTPException(status_code=404, detail="Skill not found")


@app.get("/api/memories")
async def get_memories(category: str = None, query: str = None):
    """Search or list memories."""
    from core.memory_store import MemoryStore
    store = MemoryStore(db_path=get_data_path("memory.db"))
    
    if query:
        results = store.search_memories(query, top_k=10, category=category)
        return {"memories": results, "type": "search"}
    else:
        results = store.get_all_memories(category=category, limit=50)
        return {"memories": results, "type": "all"}


@app.get("/api/memories/categories")
async def get_memory_categories():
    """Get memory category summary."""
    from core.memory_store import MemoryStore
    store = MemoryStore(db_path=get_data_path("memory.db"))
    return {"categories": store.get_categories_summary()}

@app.get("/api/history")
async def get_history(session_id: int = None, before_id: int = 0, limit: int = 100):
    """Retrieve chat history. Keeps last 5 tool_step messages for context.
    Supports pagination: pass before_id to load older messages, limit to control page size."""
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


# ==========================================
# Session Management API
# ==========================================

@app.get("/api/sessions")
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
        # Mask email password in response
        if s.get("email_password"):
            s["email_password"] = "***"
        sessions.append(s)
    return {"sessions": sessions}

@app.post("/api/sessions")
async def create_session(body: dict = {}):
    """Create a new session, optionally with email config."""
    name = body.get("name", None)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    if not name:
        cursor.execute("SELECT COUNT(*) FROM sessions")
        count = cursor.fetchone()[0] + 1
        name = f"会话 {count}"
    # Build INSERT with optional email fields
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


@app.delete("/api/sessions/{session_id}")
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


@app.post("/api/sessions/{session_id}/clear")
async def clear_session(session_id: int):
    """Clear all data for a session without deleting the session itself."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
    conn.execute("UPDATE sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (session_id,))
    conn.commit()
    conn.close()

    _cascade_cleanup_session(session_id)
    return {"ok": True}

@app.put("/api/sessions/{session_id}")
async def update_session(session_id: int, body: dict = {}):
    """Update a session's name and/or email config."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Build dynamic UPDATE SET clause
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
    return {"ok": True}


# ==========================================
# Agent & Models API
# ==========================================

def _load_agents():
    """Load agent profiles from config.json."""
    config = load_config()
    raw = config.get("agent_profiles", [])
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return []
    return list(raw) if isinstance(raw, list) else []

def _save_agents(agents: list):
    """Save agent profiles to config.json."""
    config = load_config()
    config["agent_profiles"] = agents
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

@app.get("/api/agents")
async def get_agents():
    """List all agent profiles."""
    return {"agents": _load_agents()}

@app.post("/api/agents")
async def create_agent(body: dict):
    """Create a new agent profile."""
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
    return {"ok": True, "agents": agents}

@app.put("/api/agents/{agent_name}")
async def update_agent(agent_name: str, body: dict):
    """Update an existing agent profile."""
    agents = _load_agents()
    for a in agents:
        if a.get("name") == agent_name:
            if "name" in body and body["name"].strip():
                a["name"] = body["name"].strip()
            if "prompt" in body:
                a["prompt"] = body["prompt"]
            if "model" in body:
                a["model"] = body["model"]
            if "temperature" in body:
                a["temperature"] = body["temperature"]
            if "max_tokens" in body:
                a["max_tokens"] = body["max_tokens"]
            _save_agents(agents)
            return {"ok": True, "agents": agents}
    raise HTTPException(status_code=404, detail="Agent not found")

@app.delete("/api/agents/{agent_name}")
async def delete_agent(agent_name: str):
    """Delete an agent profile."""
    agents = _load_agents()
    agents = [a for a in agents if a.get("name") != agent_name]
    _save_agents(agents)
    return {"ok": True, "agents": agents}

@app.get("/api/models/available")
async def get_available_models():
    """Return available models from config + local inference servers."""
    config = load_config()
    models = []
    if config.get("default_model"):
        models.append(config["default_model"])
    for fb in config.get("fallback_models", []):
        if fb not in models:
            models.append(fb)
    # Add local models from LlamaCpp
    try:
        from core.llamacpp_manager import get_llamacpp_manager
        lm = get_llamacpp_manager()
        for m in lm.list_models():
            mname = f"llamacpp/{m}" if not m.startswith("llamacpp/") else m
            if mname not in models:
                models.append(mname)
    except Exception:
        pass
    # Add config custom models
    for cm in config.get("models", []):
        if cm not in models:
            models.append(cm)
    return {"models": models}


# ==========================================
# AI Model Designer API
# ==========================================

DESIGN_PROMPT_TEMPLATE = """You are a model architecture expert. Based on the user's requirements below, design a neural network model architecture and provide the hyperparameters.

Requirements: {requirements}

Output ONLY valid JSON (no markdown, no explanation) with this exact structure:
{{
  "architecture": "gpt_decoder|llama|bert_encoder|moe|diffusion_dit|mamba_ssm",
  "params": {{
    "num_layers": <int>,
    "hidden_dim": <int>,
    "num_attn_heads": <int>,
    "intermediate_dim": <int>,
    "vocab_size": <int>,
    "max_seq_len": <int>,
    "attn_type": "scaled_dot|flash_attn|mqa|gqa",
    "norm_position": "pre|post|sandwich",
    "norm_type": "rms|l|batch",
    "pos_encoding": "rope|alibi|learned|none",
    "activation": "gelu|swiglu|relu|silu",
    "dropout": <float 0-1>,
    "head_dim": <int>,
    "rope_theta": <float>,
    "use_bias": <bool>,
    "init_range": <float>
  }},
  "explanation": "<brief explanation of design choices>"
}}

Choose reasonable defaults for any unspecified parameters. Match the architecture to the use case."""

@app.post("/api/agent-design")
async def agent_design(body: dict = {}):
    """Use an agent to generate model design parameters from a natural language requirement."""
    agent_name = body.get("agent_name", "default")
    requirements = body.get("requirements", "").strip()
    if not requirements:
        raise HTTPException(status_code=400, detail="Requirements cannot be empty")

    # Load agent profile
    config = load_config()
    profile_model = None
    profile_prompt = ""
    if agent_name != "default":
        agents_raw = config.get("agent_profiles", [])
        agents = json.loads(agents_raw) if isinstance(agents_raw, str) else agents_raw
        for a in agents:
            if isinstance(a, dict) and a.get("name") == agent_name:
                profile_prompt = a.get("prompt", "")
                profile_model = a.get("model", None)
                break

    # Determine which model to use
    model = profile_model or config.get("default_model", "moonshot/kimi-latest")

    # Build the design prompt
    system_prompt = profile_prompt + "\n\n" + DESIGN_PROMPT_TEMPLATE.format(requirements=requirements) if profile_prompt else DESIGN_PROMPT_TEMPLATE.format(requirements=requirements)

    # Make a simple LLM call
    try:
        from core.llm_client import LLMClient
        client = LLMClient(default_model=model)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Please design a model for this requirement: {requirements}"}
        ]
        response, actual_model = client.chat(messages=messages, tools=None)
        reply = response.choices[0].message.content or ""
        return {"response": reply, "model_used": actual_model}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI design failed: {str(e)}")


# ==========================================
# Task Management API
# ==========================================

@app.get("/api/tasks")
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
    cursor = conn.cursor()
    # Explicitly set WAL mode on this connection
    cur = conn.execute("PRAGMA journal_mode")
    row = cur.fetchone()
    jm = row[0] if row else '?'
    if jm != 'wal':
        print(f"[DB] get_tasks: journal_mode={jm}, attempting WAL...")
        r2 = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        print(f"[DB] get_tasks: after PRAGMA journal_mode=WAL -> {r2[0] if r2 else '?'}")
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

    where_clause = ""
    if conditions:
        where_clause = " WHERE " + " AND ".join(conditions)

    # Count total matching rows
    t0 = _time.time()
    count_query = "SELECT COUNT(*) FROM tasks t" + where_clause
    cursor.execute(count_query, params)
    total_count = cursor.fetchone()[0]
    t1 = _time.time()

    # Fetch page
    query = ("SELECT " + columns + ", sess.name as session_name, "
             "(SELECT COUNT(*) FROM task_steps WHERE task_id = t.id) as step_count "
             "FROM tasks t LEFT JOIN sessions sess ON sess.id = t.session_id" +
             where_clause +
             " ORDER BY t.created_at DESC LIMIT ? OFFSET ?")
    cursor.execute(query, params + [page_size, offset])
    rows = cursor.fetchall()
    conn.close()
    t2 = _time.time()

    tasks = []
    for row in rows:
        sid = row["session_id"] if "session_id" in row.keys() else None
        tasks.append({
            "id": row["id"],
            "title": row["title"],
            "user_query": row["user_query"],
            "status": row["status"],
            "task_type": row["task_type"] if "task_type" in row.keys() else "oneshot",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "result_summary": row["result_summary"],
            "step_count": row["step_count"],
            "session_id": sid,
            "session_name": row["session_name"] if "session_name" in row.keys() else None,
            "schedule_cron": row["schedule_cron"] if "schedule_cron" in row.keys() else None,
            "schedule_enabled": bool(row["schedule_enabled"]) if "schedule_enabled" in row.keys() else False,
            "next_run_at": row["next_run_at"] if "next_run_at" in row.keys() else None,
            "resume_count": row["resume_count"] if "resume_count" in row.keys() else 0,
            "total_tokens": row["total_tokens"] if "total_tokens" in row.keys() else 0,
            "total_cost": row["total_cost"] if "total_cost" in row.keys() else 0.0,
            "prompt_tokens": row["prompt_tokens"] if "prompt_tokens" in row.keys() else 0,
            "completion_tokens": row["completion_tokens"] if "completion_tokens" in row.keys() else 0,
            "cached_tokens": row["cached_tokens"] if "cached_tokens" in row.keys() else 0
        })
    return {"tasks": tasks, "total_count": total_count, "page": page, "page_size": page_size,
            "_dbg": {"jm": jm, "t_count": round(t1-t0, 3), "t_query": round(t2-t1, 3), "t_total": round(t2-t0, 3),
                     "server_ts": _time.time()}}

@app.get("/api/tasks/{task_id}")
async def get_task_detail(task_id: int):
    """Get task detail with all steps."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT t.*, sess.name as session_name
        FROM tasks t
        LEFT JOIN sessions sess ON sess.id = t.session_id
        WHERE t.id = ?
    """, (task_id,))
    task_row = cursor.fetchone()
    if not task_row:
        raise HTTPException(status_code=404, detail="Task not found")
    
    cursor.execute("SELECT * FROM task_steps WHERE task_id = ? ORDER BY created_at ASC", (task_id,))
    step_rows = cursor.fetchall()
    conn.close()
    
    output_files = []
    try:
        output_files = json.loads(task_row["output_files"] or "[]")
    except Exception:
        pass
    
    steps = []
    for s in step_rows:
        steps.append({
            "step_number": s["step_number"],
            "tool_name": s["tool_name"],
            "tool_label": s["tool_label"],
            "args_preview": s["args_preview"],
            "result_preview": s["result_preview"],
            "full_result": s["full_result"],
            "full_args": s["full_args"],
            "success": bool(s["success"]),
            "thinking_content": s["thinking_content"],
            "created_at": s["created_at"]
        })
    
    task_type = "oneshot"
    try:
        task_type = task_row["task_type"] or "oneshot"
    except Exception:
        pass

    return {
        "task": {
            "id": task_row["id"],
            "title": task_row["title"],
            "user_query": task_row["user_query"],
            "status": task_row["status"],
            "task_type": task_type,
            "created_at": task_row["created_at"],
            "updated_at": task_row["updated_at"],
            "result_summary": task_row["result_summary"],
            "output_files": output_files,
            "steps": steps,
            "schedule_cron": task_row["schedule_cron"] if "schedule_cron" in task_row.keys() else None,
            "schedule_enabled": bool(task_row["schedule_enabled"]) if "schedule_enabled" in task_row.keys() else False,
            "next_run_at": task_row["next_run_at"] if "next_run_at" in task_row.keys() else None,
            "last_run_at": task_row["last_run_at"] if "last_run_at" in task_row.keys() else None,
            "run_count": task_row["run_count"] if "run_count" in task_row.keys() else 0,
            "resume_count": task_row["resume_count"] if "resume_count" in task_row.keys() else 0,
            "max_resume_count": task_row["max_resume_count"] if "max_resume_count" in task_row.keys() else 10,
            "interruption_reason": task_row["interruption_reason"] if "interruption_reason" in task_row.keys() else None,
            "session_id": task_row["session_id"] if "session_id" in task_row.keys() else None,
            "session_name": task_row["session_name"] if "session_name" in task_row.keys() else None,
            "total_tokens": task_row["total_tokens"] if "total_tokens" in task_row.keys() else 0,
            "total_cost": task_row["total_cost"] if "total_cost" in task_row.keys() else 0.0,
            "prompt_tokens": task_row["prompt_tokens"] if "prompt_tokens" in task_row.keys() else 0,
            "completion_tokens": task_row["completion_tokens"] if "completion_tokens" in task_row.keys() else 0,
            "cached_tokens": task_row["cached_tokens"] if "cached_tokens" in task_row.keys() else 0
        }
    }

@app.get("/api/tasks/{task_id}/steps")
async def get_task_steps(task_id: int, page: int = 1, page_size: int = 50):
    """Get paginated steps for a task."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) as cnt FROM task_steps WHERE task_id=?", (task_id,))
    total = cursor.fetchone()["cnt"]

    offset = (page - 1) * page_size
    cursor.execute(
        "SELECT step_number, tool_name, tool_label, args_preview, result_preview, "
        "full_result, full_args, success, thinking_content, created_at "
        "FROM task_steps WHERE task_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (task_id, page_size, offset)
    )
    step_rows = cursor.fetchall()
    conn.close()

    steps = []
    for s in step_rows:
        steps.append({
            "step_number": s["step_number"],
            "tool_name": s["tool_name"],
            "tool_label": s["tool_label"],
            "args_preview": s["args_preview"],
            "result_preview": s["result_preview"],
            "full_result": s["full_result"],
            "full_args": s["full_args"],
            "success": bool(s["success"]),
            "thinking_content": s["thinking_content"],
            "created_at": s["created_at"]
        })

    return {"steps": steps, "total": total, "page": page, "page_size": page_size,
            "total_pages": max(1, (total + page_size - 1) // page_size)}

@app.post("/api/tasks/{task_id}/interrupt")
async def interrupt_task(task_id: int):
    """Mark a task as interrupted by user and stop its agent."""
    # 1. Stop background agent if running
    bg_agent = _background_agents.get(task_id)
    if bg_agent:
        bg_agent.is_interrupted = True

    # 2. Find foreground agent via task's session_id
    try:
        conn_i = sqlite3.connect(DB_PATH)
        row_i = conn_i.execute("SELECT session_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        conn_i.close()
        if row_i:
            sid = row_i[0]
            fg_agents = _active_agents.get(sid, {})
            fg_agent = next(iter(fg_agents.values())) if fg_agents else None
            if fg_agent:
                fg_agent.is_interrupted = True
    except Exception:
        pass

    interrupt_shell()

    # Cancel any active download associated with this task
    if _llamacpp_download_state.get("active"):
        _llamacpp_download_state["cancelled"] = True
        _llamacpp_download_state["active"] = False
        _broadcast_to_websockets({
            "type": "llamacpp_download",
            "task": _llamacpp_download_state.get("type", ""),
            "label": "下载已取消",
            "progress": 0.0,
            "stage": "error",
            "error": "用户中断"
        })

    update_task_status(task_id, "interrupted", interruption_reason="user")
    return {"status": "success", "message": "Task marked as interrupted"}

@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int):
    """Delete a task and its steps, cleaning up runtime state."""
    # Interrupt running agent (both background and active WebSocket sessions) and processes
    bg_agent = _background_agents.pop(str(task_id), None)
    if bg_agent:
        try: bg_agent.set_interrupt_flag()
        except Exception: pass
    for _sid, _agents in list(_active_agents.items()):
        _agent = _agents.pop(str(task_id), None)
        if _agent:
            try: _agent.set_interrupt_flag()
            except Exception: pass
    from tools.shell import cleanup_background_process as _cleanup
    _cleanup(str(task_id))
    # Collect and clean up temp files before deleting steps
    _temp_files = []
    try:
        _conn2 = sqlite3.connect(DB_PATH)
        _conn2.row_factory = sqlite3.Row
        for _row in _conn2.execute("SELECT generated_files FROM task_steps WHERE task_id=?", (task_id,)).fetchall():
            _gf = _row[0] or ""
            if _gf.startswith("["):
                try:
                    _files = json.loads(_gf)
                    for _f in _files:
                        if isinstance(_f, dict) and _f.get("type") == "temp" and _f.get("path"):
                            _temp_files.append(_f["path"])
                except Exception:
                    pass
        _conn2.close()
        for _fp in _temp_files:
            try:
                if os.path.exists(_fp):
                    os.remove(_fp)
                    print(f"[Task] Cleaned up temp file: {_fp}")
            except Exception:
                pass
    except Exception:
        pass

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM task_steps WHERE task_id = ?", (task_id,))
    cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    # Clean up associated todo items
    try:
        from tools.task_plan import load_todos as _lt, save_todos as _st
        _todos = _lt()
        _changed = False
        for item in list(_todos.get("items", [])):
            if item.get("task_id") == task_id:
                _todos["items"].remove(item)
                _changed = True
        if _changed:
            _st(_todos)
    except Exception:
        pass
    return {"status": "success", "message": "Task deleted"}

# ==========================================
# Scheduled Task API
# ==========================================

class ScheduleTaskRequest(BaseModel):
    title: str
    user_query: str
    schedule_cron: str
    enabled: bool = True

@app.post("/api/tasks/schedule")
async def create_scheduled_task(req: ScheduleTaskRequest):
    """Create a new scheduled task with cron expression."""
    # Validate cron expression
    try:
        from croniter import croniter
        if not croniter.is_valid(req.schedule_cron):
            raise HTTPException(status_code=400, detail="Invalid cron expression")
    except ImportError:
        raise HTTPException(status_code=500, detail="croniter not installed")

    task_id = create_task(
        title=req.title,
        user_query=req.user_query,
        task_type='scheduled',
        schedule_cron=req.schedule_cron,
        schedule_enabled=req.enabled
    )
    # Set initial status to 'scheduled' instead of 'running'
    update_task_status(task_id, 'scheduled')
    return {"status": "success", "task_id": task_id}

@app.post("/api/tasks/{task_id}/toggle-schedule")
async def toggle_schedule(task_id: int):
    """Toggle schedule enabled/disabled."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT schedule_enabled, schedule_cron FROM tasks WHERE id=?", (task_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Task not found")
    new_state = 0 if row["schedule_enabled"] else 1
    next_run = None
    if new_state and row["schedule_cron"]:
        try:
            from croniter import croniter
            next_run = croniter(row["schedule_cron"], datetime.now(timezone.utc)).get_next(datetime).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            pass
    cursor.execute("UPDATE tasks SET schedule_enabled=?, next_run_at=?, status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (new_state, next_run, 'scheduled' if new_state else 'paused', task_id))
    conn.commit()
    conn.close()
    return {"status": "success", "enabled": bool(new_state)}

@app.put("/api/tasks/{task_id}/schedule")
async def update_schedule(task_id: int, req: ScheduleTaskRequest):
    """Update schedule configuration."""
    try:
        from croniter import croniter
        if not croniter.is_valid(req.schedule_cron):
            raise HTTPException(status_code=400, detail="Invalid cron expression")
    except ImportError:
        raise HTTPException(status_code=500, detail="croniter not installed")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    next_run = None
    if req.enabled:
        try:
            from croniter import croniter as ci
            next_run = ci(req.schedule_cron, datetime.now(timezone.utc)).get_next(datetime).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            pass
    cursor.execute(
        "UPDATE tasks SET title=?, user_query=?, schedule_cron=?, schedule_enabled=?, next_run_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (req.title, req.user_query, req.schedule_cron, 1 if req.enabled else 0, next_run, task_id)
    )
    conn.commit()
    conn.close()
    return {"status": "success"}


# ── Process Management (Background/Server Processes) ──

@app.get("/api/processes")
async def list_processes():
    """List all active background/server processes."""
    from tools.shell import get_background_processes, get_orphan_processes
    procs = {}
    for tid, info in get_background_processes().items():
        pinfo = dict(info)
        pid = pinfo.get("pid")
        pinfo["alive"] = _is_pid_alive(pid) if pid else False
        pinfo["uptime"] = _time.time() - pinfo.get("started_at", _time.time()) if pinfo.get("started_at") else 0
        procs[tid] = pinfo
    orphans = {}
    for oid, info in get_orphan_processes().items():
        pinfo = dict(info)
        pid = pinfo.get("pid")
        pinfo["alive"] = _is_pid_alive(pid) if pid else False
        pinfo["uptime"] = _time.time() - pinfo.get("started_at", _time.time()) if pinfo.get("started_at") else 0
        orphans[oid] = pinfo
    return {"processes": procs, "orphans": orphans}


@app.get("/api/tasks/{task_id}/process")
async def get_task_process(task_id: int):
    """Get process info for a specific task."""
    from tools.shell import get_background_processes
    pinfo = get_background_processes().get(str(task_id))
    if not pinfo:
        return {"alive": False, "pid": None, "command": "", "uptime": 0}
    result = dict(pinfo)
    pid = result.get("pid")
    result["alive"] = _is_pid_alive(pid) if pid else False
    result["uptime"] = _time.time() - result.get("started_at", _time.time()) if result.get("started_at") else 0
    return result


@app.get("/api/tasks/{task_id}/logs")
async def get_task_logs(task_id: int, lines: int = 50):
    """Read tail of a task's process output file."""
    from tools.shell import get_background_processes
    pinfo = get_background_processes().get(str(task_id))
    out_file = pinfo.get("output_file", "") if pinfo else ""
    if not out_file or not os.path.exists(out_file):
        # Try reading from task's stored output_files
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT output_files FROM tasks WHERE id=?", (task_id,)).fetchone()
        conn.close()
        if row and row["output_files"]:
            files = json.loads(row["output_files"])
            if files:
                out_file = files[0]
    if not out_file or not os.path.exists(out_file):
        raise HTTPException(status_code=404, detail="No log file found")
    try:
        with open(out_file, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        tail_lines = content.split("\n")[-lines:]
        return {
            "file": out_file,
            "total_lines": content.count("\n") + 1,
            "lines": tail_lines,
            "size": len(content),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read log file: {e}")


def _kill_process_on_platform(pid: int) -> str:
    """Kill a process cross-platform. Returns status message."""
    try:
        os.kill(pid, 0)
    except OSError:
        return f"Process {pid} is not running."
    try:
        if sys.platform == "win32":
            import subprocess
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=5)
            except Exception:
                os.kill(pid, getattr(signal, "CTRL_BREAK_EVENT", 9))
        else:
            os.kill(pid, getattr(signal, "SIGTERM", 15))
            import time as _t
            _t.sleep(2)
            try:
                os.kill(pid, 0)
                os.kill(pid, getattr(signal, "SIGKILL", 9))
            except OSError:
                pass
        return f"Process {pid} terminated."
    except Exception as e:
        return f"Failed to kill process {pid}: {e}"


def _is_pid_alive(pid: int) -> bool:
    """Check if a PID is alive."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


@app.post("/api/tasks/{task_id}/kill")
async def kill_task_process(task_id: int):
    """Kill a task's background/server process."""
    from tools.shell import get_background_processes, cleanup_background_process
    pinfo = get_background_processes().get(str(task_id))
    if not pinfo:
        raise HTTPException(status_code=404, detail="No tracked process found for this task")
    pid = pinfo.get("pid")
    if not pid:
        raise HTTPException(status_code=404, detail="No PID found")
    command = pinfo.get("command", "")[:200]
    out_file = pinfo.get("output_file", "")

    # Read the output file first — pass results to the agent before killing
    full_out = ""
    if out_file and os.path.exists(out_file):
        try:
            with open(out_file, "r", encoding="utf-8", errors="replace") as _rf:
                full_out = _rf.read()[-5000:]
        except Exception:
            pass

    result = _kill_process_on_platform(pid)
    cleanup_background_process(str(task_id))
    update_task_status(task_id, "interrupted",
                       f"进程 (PID {pid}) 已被用户手动终止。",
                       interruption_reason="user")

    # Notify the agent to resume if the task was backgrounded (waiting on this process)
    try:
        bg_conn = sqlite3.connect(DB_PATH)
        bg_conn.row_factory = sqlite3.Row
        task_row = bg_conn.execute(
            "SELECT status, task_type FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        bg_conn.close()
        if task_row and task_row["status"] in ("backgrounded", "running"):
            ctx = get_task_context(task_id)
            if ctx:
                msg = f"【系统通知】你手动终止了后台进程 (PID {pid})。\n命令: {command}\n"
                if full_out:
                    msg += f"进程的已有输出:\n```\n{full_out[:2000]}\n```\n"
                msg += "该进程已被终止，请根据已有结果继续执行任务。"
                ctx.append({"role": "user", "content": msg})
                save_task_context(task_id, ctx)
                print(f"[Process] Task {task_id} process (PID {pid}) killed by user — resuming agent")
                # Resume via background task directly (not _direct_resume_background_task
                # which overwrites the status message for download-specific flow)
                threading.Thread(
                    target=_run_background_task,
                    args=(task_id, "", ctx, True),
                    daemon=True
                ).start()
    except Exception as resume_err:
        print(f"[Process] Failed to resume task {task_id} after kill: {resume_err}")

    return {"status": "success", "message": result}


# ── SearXNG Management ──

class SearXNGControlRequest(BaseModel):
    action: str  # "install", "start", "stop"


@app.get("/api/searxng/status")
async def get_searxng_status():
    """Return SearXNG Docker and runtime status."""
    from core.searxng_manager import get_searxng_manager
    config = load_config()
    manager = get_searxng_manager()
    manager.external_url = config.get("searxng_url", "")
    manager.port = config.get("searxng_port", 8888)
    status = manager.get_status()
    if config.get("searxng_url"):
        status["running"] = manager.is_running()
        status["url"] = config["searxng_url"]
    return status


@app.post("/api/searxng/install")
async def install_searxng():
    """One-click install: generate configs and start SearXNG via Docker."""
    from core.searxng_manager import get_searxng_manager
    manager = get_searxng_manager()
    if not manager.is_docker_available():
        raise HTTPException(status_code=400, detail="Docker is not available on this system. Please install Docker Desktop first.")
    if manager.is_running():
        return {"status": "success", "message": "SearXNG is already running"}
    success = manager.install()
    if not success:
        raise HTTPException(status_code=500, detail="Failed to install or start SearXNG. Check Docker logs for details.")
    return {"status": "success", "message": "SearXNG installed and started successfully"}


@app.post("/api/searxng/control")
async def control_searxng(req: SearXNGControlRequest):
    """Start or stop SearXNG container."""
    from core.searxng_manager import get_searxng_manager
    manager = get_searxng_manager()
    if req.action == "start":
        success = manager.start()
        if not success:
            raise HTTPException(status_code=500, detail="Failed to start SearXNG")
        return {"status": "success", "message": "SearXNG started"}
    elif req.action == "stop":
        manager.stop()
        return {"status": "success", "message": "SearXNG stopped"}
    else:
        raise HTTPException(status_code=400, detail="Invalid action")


# ── Version & Upgrade ──

@app.get("/api/version")
async def get_version_info():
    """Return current version + latest release from GitHub."""
    from core.version import get_version
    current = get_version()
    latest = None
    try:
        resp = requests.get(
            "https://api.github.com/repos/deanwinchester/open-agc/releases/latest",
            timeout=10,
            headers={"Accept": "application/vnd.github.v3+json"},
        )
        if resp.status_code == 200:
            latest = resp.json().get("tag_name", "").lstrip("v")
    except Exception:
        pass
    return {
        "current": current,
        "latest": latest,
        "upgrade_available": bool(latest and latest != current),
    }


class UpgradeRequest(BaseModel):
    confirm: bool = True

@app.post("/api/upgrade")
async def trigger_upgrade(req: UpgradeRequest = None):
    """Trigger a source-code upgrade. Downloads and applies latest code in-place."""
    try:
        from core.auto_upgrade import AutoUpgrader
        upgrader = AutoUpgrader()
        if not upgrader.fetch_latest_release():
            raise HTTPException(status_code=502, detail="无法连接到 GitHub，请检查网络")
        if not upgrader.is_upgrade_available():
            return {"status": "up_to_date", "message": f"已是最新版本 v{upgrader.current_version}"}
        success = upgrader.perform_upgrade()
        if success:
            return {"status": "upgraded", "message": f"已升级到 v{upgrader.latest_version}，无需重启"}
        else:
            raise HTTPException(status_code=500, detail="升级失败，请查看日志")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"升级异常: {str(e)}")


# ── Log Viewer ──

_AGENT_LOG_FILE = None

def _ensure_agent_log() -> str:
    global _AGENT_LOG_FILE
    if _AGENT_LOG_FILE is None:
        from core.paths import get_data_dir
        _AGENT_LOG_FILE = os.path.join(get_data_dir(), "logs", "agent.log")
        os.makedirs(os.path.dirname(_AGENT_LOG_FILE), exist_ok=True)
    return _AGENT_LOG_FILE

def log_agent_error(msg: str):
    """Write an agent error to the persistent log file and rotate at 1MB."""
    log_path = _ensure_agent_log()
    try:
        if os.path.exists(log_path) and os.path.getsize(log_path) > 1024 * 1024:
            os.rename(log_path, log_path + ".old")
        with open(log_path, "a", encoding="utf-8") as f:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass

@app.get("/api/logs")
async def get_logs(lines: int = 200):
    """Return the last N lines of the agent log file."""
    log_path = _ensure_agent_log()
    if not os.path.exists(log_path):
        return {"lines": [], "total": 0}
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        all_lines = content.split("\n")
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {"lines": tail, "total": len(all_lines)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Model Call Logs ──

@app.get("/api/model-logs/status")
async def get_model_log_status():
    """Return whether model logging is currently enabled."""
    from core.llm_client import is_model_logging_enabled
    return {"enabled": is_model_logging_enabled()}

@app.post("/api/model-logs/toggle")
async def toggle_model_logging(body: dict):
    """Enable or disable model call logging."""
    enabled = body.get("enabled", True)
    from core.llm_client import set_model_logging
    set_model_logging(enabled)
    return {"enabled": enabled}

@app.post("/api/model-logs/clear")
async def clear_model_logs():
    """Delete all model call logs."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM model_call_logs")
        conn.commit()
        conn.close()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/model-logs/filters")
async def get_model_log_filters():
    """Return distinct providers and models for filter dropdowns."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        providers = [r["provider"] for r in conn.execute(
            "SELECT DISTINCT provider FROM model_call_logs ORDER BY provider").fetchall()]
        models = [r["model"] for r in conn.execute(
            "SELECT DISTINCT model FROM model_call_logs ORDER BY model").fetchall()]
        return {"providers": providers, "models": models}
    except Exception as e:
        return {"providers": [], "models": []}
    finally:
        conn.close()


@app.get("/api/model-logs")
async def get_model_logs(
    provider: str = "",
    model: str = "",
    start_date: str = "",
    end_date: str = "",
    cache_hit: str = "",
    page: int = 1,
    page_size: int = 50,
):
    """Query model call logs with filters and pagination."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        where = []
        params = []
        if provider:
            where.append("provider = ?")
            params.append(provider)
        if model:
            where.append("model = ?")
            params.append(model)
        if start_date:
            where.append("timestamp >= ?")
            params.append(start_date)
        if end_date:
            where.append("timestamp <= ?")
            params.append(end_date + " 23:59:59")
        if cache_hit:
            where.append("cache_hit = ?")
            params.append(cache_hit)

        sql_where = (" WHERE " + " AND ".join(where)) if where else ""

        # Count total
        total = conn.execute(
            "SELECT COUNT(*) as cnt FROM model_call_logs" + sql_where, params
        ).fetchone()["cnt"]

        # Fetch page
        offset = (page - 1) * page_size
        rows = conn.execute(
            "SELECT id, timestamp, provider, model, prompt_tokens, completion_tokens, "
            "total_tokens, cache_hit, latency_ms, cost_estimate, cached_tokens "
            "FROM model_call_logs" + sql_where +
            " ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        ).fetchall()

        logs = [dict(r) for r in rows]
        return {"logs": logs, "total": total, "page": page, "page_size": page_size}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@app.get("/api/model-logs/{log_id}")
async def get_model_log_detail(log_id: int):
    """Return full detail for a single model call log."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM model_call_logs WHERE id = ?", (log_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Log not found")
        return dict(row)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


# ── SPA fallback (must be the LAST route; all API routes defined above) ──

@app.get("/{full_path:path}")
async def spa_fallback(full_path: str):
    """Catch-all for frontend History API routes. All API/static/ws paths
    are matched by earlier routes/mounts, so this only fires for unknown paths."""
    if full_path.startswith(("api/", "static/", "ws")):
        raise HTTPException(status_code=404)
    return FileResponse("static/index.html")


# Initialize a global agent instance
# In a real multi-user system, this would be per-session
# We'll instantiate per connection for simplicity and state isolation in this demo

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_websockets.append(websocket)

    # Read session_id from query parameter, default to 1
    ws_session_id = int(websocket.query_params.get("session_id", "1"))

    # Push current download state to the newly connected client
    if _llamacpp_download_state.get("active"):
        await websocket.send_json({
            "type": "llamacpp_download",
            "task": _llamacpp_download_state.get("type", ""),
            "label": _llamacpp_download_state.get("label", ""),
            "progress": _llamacpp_download_state.get("progress", 0.0),
            "stage": _llamacpp_download_state.get("stage", ""),
            "error": _llamacpp_download_state.get("error", "")
        })

    # Broadcast history_steps if this session has a recent interrupted/completed task
    try:
        _hb_conn = sqlite3.connect(DB_PATH)
        _hb_row = _hb_conn.execute(
            "SELECT id, status FROM tasks WHERE session_id=? AND status IN ('interrupted','completed') ORDER BY updated_at DESC LIMIT 1",
            (ws_session_id,)
        ).fetchone()
        _hb_conn.close()
        if _hb_row:
            _broadcast_task_history(_hb_row[0], ws_session_id, _hb_row[1])
    except Exception:
        pass

    # Flag to track whether this connection is still alive
    ws_alive = True

    async def _safe_send(data: dict):
        """Send JSON via WebSocket, silently ignore if connection is dead."""
        nonlocal ws_alive
        if not ws_alive:
            return
        try:
            await websocket.send_json(data)
        except Exception:
            ws_alive = False

    # We will maintain conversation history for this session here
    # Load recent chat history from DB instead of starting empty
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        # Load the last 20 messages for the current session
        cursor.execute("SELECT role, content FROM (SELECT * FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 20) ORDER BY id ASC", (ws_session_id,))
        rows = cursor.fetchall()
        conn.close()

        # LLMs strict require 'assistant' not 'agent'
        session_history = []
        for row in rows:
            role = row["role"]
            if role in ("tool_step",):  # skip internal display messages
                continue
            if role == "agent":
                role = "assistant"
            session_history.append({"role": role, "content": row["content"]})
    except Exception as e:
        print(f"Failed to load chat history: {e}")
        session_history = []
    last_query = ""  # Track last query for retry
    agent_is_running = False
    receive_task = None # Persistent receive_task to avoid concurrency issues

    # Replay the most recent task's steps for this session
    # Only replay if the user hasn't sent new messages after the task completed
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT t.id, t.status, t.created_at, t.updated_at FROM tasks t "
            "WHERE t.id IN (SELECT DISTINCT task_id FROM task_steps WHERE session_id=?) "
            "ORDER BY t.created_at DESC LIMIT 1",
            (ws_session_id,))
        last_task = cursor.fetchone()
        if last_task:
            # Only replay if no newer user messages exist after the task completed
            check_time = last_task["updated_at"] or last_task["created_at"]
            newer_msgs = cursor.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id=? AND role='user' AND timestamp > ?",
                (ws_session_id, check_time)).fetchone()[0]
            if newer_msgs == 0:
                steps = cursor.execute(
                    "SELECT step_number, tool_name, tool_label, args_preview, "
                    "result_preview, success FROM task_steps "
                    "WHERE task_id=? ORDER BY created_at",
                    (last_task["id"],)).fetchall()
                if steps:
                    await _safe_send({
                        "type": "history_steps",
                        "task_id": last_task["id"],
                        "task_status": last_task["status"],
                        "steps": [dict(s) for s in steps],
                        "session_id": ws_session_id
                    })
        conn.close()
    except Exception as e:
        print(f"[WS] Task replay error: {e}")
    
    async def run_agent_with_progress(query: str, model: str = None, agent_profile_name: str = None, images: list = None, resume_task_id: int = None):
        """Run agent in a thread and push progress to WebSocket via a Queue.

        If resume_task_id is set, steps are appended to the existing task instead of creating a new one.
        """
        nonlocal session_history, last_query, agent_is_running, receive_task, ws_alive, ws_session_id

        if agent_is_running:
            return "BUSY"

        agent_is_running = True
        # Pre-resolve task_id BEFORE agent execution so tools always get a valid _task_id.
        # resume_task_id is used when explicitly resuming; otherwise detect new vs continuation.
        if resume_task_id:
            ws_task_id = resume_task_id
        else:
            ws_task_id = _resolve_task_for_query(ws_session_id, query)
        step_offset = 0

        # Compute step offset for resume
        if resume_task_id:
            try:
                db_conn = sqlite3.connect(DB_PATH)
                max_step = db_conn.execute(
                    "SELECT COALESCE(MAX(step_number), -1) FROM task_steps WHERE task_id=?",
                    (resume_task_id,)).fetchone()[0]
                db_conn.close()
                step_offset = max_step + 1
                update_task_status(resume_task_id, "running")
            except Exception as e:
                print(f"[Task] Resume offset error: {e}")

        try:
            import queue as thread_queue
            progress_queue = thread_queue.Queue()
            has_taken_action = False
            agent = None
            _bg_pid = None

            # Accumulate shell output per step for tool_step message persistence
            _step_outputs: dict = {}
            def progress_callback(event: dict):
                nonlocal has_taken_action, ws_task_id, _bg_pid
                """Thread-safe: push progress events from thread pool into queue."""
                # Handle sandbox_approved: persist to pending approvals so it
                # survives agent recreation on task resume
                if event.get("event") == "sandbox_approved":
                    _path = event.get("path", "")
                    _sid = event.get("session_id") or ws_session_id
                    if _path:
                        _pending_sandbox_approvals.setdefault(_sid, []).append({
                            "action": "approve_once", "path": _path
                        })
                        print(f"[Sandbox] Persisted approval: {_path} for session {_sid}")
                    return  # Don't queue this event to frontend

                # Record task steps (offset on resume to continue numbering)
                adjusted_step = event.get("step", 0) + step_offset
                event["step"] = adjusted_step

                # Accumulate shell output per step for tool_step persistence
                # Capture PID from pause_and_wait for BgMonitor tracking
                if event.get("event") == "task_backgrounded":
                    _bg_pid = event.get("pid")

                if event.get("event") == "shell_output":
                    text = event.get("text", "")
                    if text:
                        prev = _step_outputs.get(adjusted_step, "")
                        _step_outputs[adjusted_step] = (prev + text)[-8000:]  # cap at 8K chars

                if ws_task_id and event.get("event") == "tool_start":
                    try:
                        add_task_step(
                            task_id=ws_task_id,
                            step_number=adjusted_step,
                            tool_name=event.get("tool", ""),
                            tool_label=event.get("tool_label", ""),
                            args_preview=event.get("args_preview", ""),
                            session_id=ws_session_id,
                            tool_call_id=event.get("tool_call_id"),
                            full_args=event.get("tool_args")
                        )
                    except Exception as e:
                        print(f"[Task] Failed to add step: {e}")

                if ws_task_id and event.get("event") == "tool_done":
                    # Link any pending downloads to this task (downloads run AFTER tool execution)
                    try:
                        import tools.download as _dl
                        pending = getattr(_dl, '_pending_task_links', {})
                        dl_ids = pending.pop(ws_session_id, [])
                        if dl_ids:
                            print(f"[Task] tool_done: linking {len(dl_ids)} download(s) to task {ws_task_id}")
                            dl_conn = sqlite3.connect(DB_PATH)
                            for dl_id in dl_ids:
                                dl_conn.execute(
                                    "UPDATE downloads SET task_id=? WHERE id=? AND task_id IS NULL",
                                    (ws_task_id, dl_id))
                                # Check if this download already failed before linking
                                already_failed = dl_conn.execute(
                                    "SELECT status, label, filename, error_message FROM downloads WHERE id=? AND status='failed'",
                                    (dl_id,)).fetchone()
                                if already_failed:
                                    err = already_failed[3] or "未知错误"
                                    label = already_failed[1] or already_failed[2] or f"download #{dl_id}"
                                    save_message("system",
                                        f"❌ 下载失败: {label}\n错误信息: {err}",
                                        ws_session_id)
                                    _broadcast_to_websockets({
                                        "type": "download_failed",
                                        "download_id": dl_id,
                                        "task_id": ws_task_id,
                                        "session_id": ws_session_id,
                                        "label": label,
                                        "error": err
                                    })
                                    # Inject failure info into the running agent so it can retry
                                    try:
                                        _aa_dict = _active_agents.get(ws_session_id, {})
                                        agent_ref = next(iter(_aa_dict.values())) if _aa_dict else None
                                        if agent_ref:
                                            agent_ref.pending_messages.append(
                                                f"【系统通知】下载失败了。\n文件: {label}\n错误: {err}\n"
                                                f"download_id: {dl_id}\n"
                                                f"请尝试其他方式重新下载（如换源），如果确实无法下载则结束任务。"
                                            )
                                            print(f"[Task] Injected download failure into agent for session {ws_session_id}")
                                    except Exception as inject_err:
                                        print(f"[Task] Failed to inject failure into agent: {inject_err}")
                                    print(f"[Task] tool_done: download #{dl_id} already failed — notified session {ws_session_id}")
                                # Also check if download already completed before linking
                                already_done = dl_conn.execute(
                                    "SELECT status, label, filename FROM downloads WHERE id=? AND status='completed'",
                                    (dl_id,)).fetchone()
                                if already_done:
                                    label = already_done[1] or already_done[2] or f"download #{dl_id}"
                                    try:
                                        _aa_dict = _active_agents.get(ws_session_id, {})
                                        agent_ref = next(iter(_aa_dict.values())) if _aa_dict else None
                                        if agent_ref:
                                            agent_ref.pending_messages.append(
                                                f"【系统通知】后台下载已完成。\n文件: {label}\n"
                                                f"请继续执行之前的任务。"
                                            )
                                            print(f"[Task] Injected download completion into agent for session {ws_session_id}")
                                    except Exception as inject_err:
                                        print(f"[Task] Failed to inject download completion: {inject_err}")
                            dl_conn.commit()
                            dl_conn.close()
                    except Exception as link_err:
                        print(f"[Task] tool_done link error: {link_err}")
                    try:
                        # Update the step with result and tool_call_id
                        conn = sqlite3.connect(DB_PATH)
                        cursor = conn.cursor()
                        cursor.execute(
                            "UPDATE task_steps SET result_preview=?, full_result=?, success=?, tool_call_id=COALESCE(?, tool_call_id) WHERE task_id=? AND step_number=?",
                            (event.get("result_preview", ""),
                             event.get("full_result", event.get("result_preview", "")),
                             1 if event.get("success") else 0,
                             event.get("tool_call_id"),
                             ws_task_id, adjusted_step)
                        )
                        conn.commit()
                        conn.close()
                    except Exception as e:
                        print(f"[Task] Failed to update step: {e}")

                # Update task_steps with tool result (result_preview only set on tool_done)
                if ws_task_id and event.get("event") == "tool_done":
                    try:
                        _rpreview = event.get("result_preview", "")
                        _success = 1 if event.get("success") else 0
                        _conn_step = sqlite3.connect(DB_PATH)
                        _conn_step.execute(
                            "UPDATE task_steps SET result_preview=?, success=? WHERE task_id=? AND step_number=?",
                            (_rpreview, _success, ws_task_id, adjusted_step)
                        )
                        _conn_step.commit()
                        _conn_step.close()
                    except Exception:
                        pass

                # Save tool_step as a message in the chat flow (skip for heartbeats)
                if ws_session_id and event.get("event") == "tool_done":
                    try:
                        import json as _js
                        step_output = _step_outputs.pop(adjusted_step, "")
                        ts_content = _js.dumps({
                            "step": adjusted_step,
                            "tool": event.get("tool", ""),
                            "tool_label": event.get("tool_label", ""),
                            "args_preview": event.get("args_preview", ""),
                            "result_preview": event.get("result_preview", ""),
                            "success": event.get("success", True),
                            "output": step_output[:5000],
                        }, ensure_ascii=False)
                        save_message("tool_step", ts_content, ws_session_id)
                    except Exception as e:
                        print(f"[Task] Failed to save tool_step message: {e}")

                # Attach task_id to the event so frontend can track it
                if ws_task_id:
                    event["task_id"] = ws_task_id
                # Adjust step number for resumed tasks
                if step_offset:
                    event["step"] = event.get("step", 0) + step_offset

                progress_queue.put(event)
            
            current_model = model or os.getenv("DEFAULT_MODEL", "moonshot/kimi-latest")

            # Auto-start llama-server if using a llamacpp model
            if "llamacpp/" in current_model:
                lm = get_llamacpp_manager()
                if not lm.is_running():
                    model_filename = current_model.replace("llamacpp/", "")
                    await _safe_send({
                        "type": "status",
                        "message": f"正在启动 llama-server 并加载 {model_filename}..."
                    })
                    lm.start(model_filename)
                    for i in range(120):
                        await asyncio.sleep(0.5)
                        if lm.is_running():
                            await _safe_send({
                                "type": "status",
                                "message": "llama-server 就绪，开始处理..."
                            })
                            break
                    else:
                        _broadcast_to_websockets({
                            "type": "llamacpp_download",
                            "task": "binary",
                            "label": "llama-server 启动失败",
                            "progress": 0.0,
                            "stage": "error",
                            "error": "模型文件可能不兼容或损坏，请尝试下载其他 GGUF 模型"
                        })
                        await _safe_send({
                            "type": "system_message",
                            "message": "❌ **llama-server 启动失败**\n\n模型文件可能不兼容或损坏，请尝试下载其他 GGUF 模型。\n可在「设置 → 模型管理」中更换模型。"
                        })
                        save_message("system",
                            "❌ llama-server 启动失败，模型文件可能不兼容或损坏，请在设置中更换模型。",
                            ws_session_id)
                        agent_is_running = False
                        return

            from core.logger import SessionLogger
            session_logger = SessionLogger(
                log_dir=get_data_path("logs"),
                session_id=ws_session_id
            )
            agent = OpenAGCAgent(model=current_model, session_id=ws_session_id,
                                 logger=session_logger,
                                 pre_enabled_tools=_session_enabled_tools.get(ws_session_id))
            _apply_pending_sandbox_approvals(agent, ws_session_id)
            _active_agents.setdefault(ws_session_id, {})[ws_task_id or 0] = agent
            
            # Inject custom agent profile prompt if specified
            if agent_profile_name and agent_profile_name != "default":
                config = load_config()
                profiles_raw = config.get("agent_profiles", [])
                try:
                    profiles = json.loads(profiles_raw) if isinstance(profiles_raw, str) else profiles_raw
                    for p in profiles:
                        if isinstance(p, dict) and p.get("name") == agent_profile_name and p.get("prompt"):
                            agent.system_prompt_base = f"【角色设定: {p['name']}】\n{p['prompt']}\n\n---\n" + agent.system_prompt_base
                            if p.get("model"):
                                agent.llm.default_model = p["model"]
                            break
                except Exception as e:
                    print(f"Failed to load agent profile {agent_profile_name}: {e}")
            
            # Inject previous session history
            if session_history:
                agent.messages.extend(session_history)
            
            loop = asyncio.get_event_loop()
            
            import concurrent.futures
            agent_future = loop.run_in_executor(
                None, 
                lambda: agent.run_turn(query, False, progress_callback, images=images, task_id=ws_task_id, skip_rag=bool(resume_task_id))
            )
            
            # Handle agent progress and check for interruption
            while not agent_future.done() and ws_alive:
                if receive_task is None:
                    receive_task = asyncio.create_task(websocket.receive_text())

                done, pending = await asyncio.wait(
                    [receive_task],
                    timeout=0.15,
                    return_when=asyncio.FIRST_COMPLETED
                )

                if receive_task in done:
                    try:
                        data = receive_task.result()
                        user_msg = json.loads(data)
                        if user_msg.get("type") == "interrupt":
                            agent.is_interrupted = True
                            interrupt_shell()
                            if ws_task_id:
                                update_task_status(ws_task_id, "interrupted", interruption_reason="user")
                            # Also interrupt any background agents for this session
                            for tid, bg_agent in list(_background_agents.items()):
                                bg_agent.is_interrupted = True
                            interrupt_shell()
                            # Cancel any active download
                            if _llamacpp_download_state.get("active"):
                                _llamacpp_download_state["cancelled"] = True
                                _llamacpp_download_state["active"] = False
                                _broadcast_to_websockets({
                                    "type": "llamacpp_download",
                                    "task": _llamacpp_download_state.get("type", ""),
                                    "label": "下载已取消",
                                    "progress": 0.0,
                                    "stage": "error",
                                    "error": "用户中断"
                                })
                        elif user_msg.get("type") == "tool_reply":
                            agent.user_input_queue.put(user_msg.get("answer"))
                        elif user_msg.get("type") == "sandbox_response":
                            sid = user_msg.get("session_id", ws_session_id)
                            action = user_msg.get("action", "deny_once")
                            wait = _sandbox_waits.get(sid)
                            if wait:
                                wait["result"]["action"] = action
                                wait["result"]["path"] = user_msg.get("path", "")
                                wait["event"].set()
                                print(f"[WS] Sandbox response: {action} for {sid}")
                            elif action in ("approve_once", "approve_dir", "approve_always", "approve_session"):
                                # Late approval: apply to running agent's whitelist directly
                                _path = user_msg.get("path", "")
                                if _path and hasattr(agent, '_session_sandbox_whitelist'):
                                    import os as _ws_os
                                    _ws_dir = _ws_os.path.dirname(_ws_os.path.abspath(_path))
                                    agent._session_sandbox_whitelist.add(_ws_dir)
                                    agent._session_sandbox_whitelist.add(_path)
                                    print(f"[WS] Late sandbox approval applied to running agent: {_path}")
                        else:
                            # Non-blocking input: queue message to agent
                            q = user_msg.get("query", user_msg.get("text", ""))
                            if q.strip():
                                # Get the most recent agent for this session
                                _aa_sess = _active_agents.get(ws_session_id, {})
                                a = next(iter(_aa_sess.values())) if _aa_sess else None
                                if a:
                                    a.queue_message(q)
                                    save_message("user", q, ws_session_id)
                                    # Save as tool_step in the task flow
                                    if ws_task_id:
                                        import json as _jj
                                        _interject_data = {
                                            "step": -1,
                                            "tool": "user_interjection",
                                            "tool_label": "用户插入",
                                            "args_preview": q[:200],
                                            "success": True,
                                            "output": ""
                                        }
                                        save_message("tool_step", _jj.dumps(_interject_data, ensure_ascii=False), ws_session_id)
                                        # Also broadcast as a progress event so frontend shows it live
                                        await _safe_send({
                                            "type": "progress",
                                            "event": "tool_start",
                                            "step": -1,
                                            "tool": "user_interjection",
                                            "tool_label": "用户插入",
                                            "args_preview": q[:200],
                                            "task_id": ws_task_id,
                                            "session_id": ws_session_id,
                                            "background": False
                                        })
                                        await _safe_send({
                                            "type": "progress",
                                            "event": "tool_done",
                                            "step": -1,
                                            "tool": "user_interjection",
                                            "tool_label": "用户插入",
                                            "result_preview": q[:200],
                                            "success": True,
                                            "task_id": ws_task_id,
                                            "session_id": ws_session_id,
                                            "background": False
                                        })
                                    print(f"[WS] Queued message to agent session {ws_session_id}")
                        receive_task = None
                    except (WebSocketDisconnect, RuntimeError):
                        ws_alive = False
                        if websocket in connected_websockets:
                            connected_websockets.remove(websocket)
                        _active_agents.pop(ws_session_id, None)
                        receive_task = None
                        # Don't raise — ws_alive=False will let outer loop break
                    except Exception:
                        receive_task = None

                # Drain the thread-safe queue (no cross-thread race)
                while True:
                    try:
                        event = progress_queue.get_nowait()
                        event["session_id"] = ws_session_id
                        await _safe_send({
                                "type": "progress",
                                **event
                            })
                    except thread_queue.Empty:
                        break

            while not progress_queue.empty():
                try:
                    event = progress_queue.get_nowait()
                    event["session_id"] = ws_session_id
                    await _safe_send({
                        "type": "progress",
                        **event
                    })
                except Exception:
                    break
            
            response = await agent_future
            session_history = agent.messages[1:]
            # Persist enabled tools for next turn (avoid re-discovering)
            _session_enabled_tools[ws_session_id] = getattr(agent, 'active_tool_names', set())

            # Detect max_iterations hit for longrun auto-resume
            is_max_iter = response and response.startswith("[MAX_ITERATIONS_REACHED]")
            is_backgrounded = response and response.startswith("[TASK_BACKGROUNDED]")

            if ws_task_id and is_backgrounded:
                # Agent voluntarily paused — save context, mark backgrounded
                save_task_context(ws_task_id, agent.messages[1:])
                update_task_status(ws_task_id, "backgrounded",
                    response[len("[TASK_BACKGROUNDED] "):].strip() or "任务进入后台",
                    interruption_reason="backgrounded")
                # Register PID for BgMonitor tracking if available
                if _bg_pid:
                    try:
                        from tools.shell import get_background_processes
                        _bg_procs = get_background_processes()
                        if str(ws_task_id) not in _bg_procs:
                            from tools.shell import _background_process_info, _background_process_lock
                            with _background_process_lock:
                                _background_process_info[str(ws_task_id)] = {"pid": _bg_pid, "command": "", "started_at": _time.time()}
                            print(f"[Task] Registered PID {_bg_pid} for BgMonitor tracking (task {ws_task_id})")
                    except Exception:
                        pass
                # Send notification to frontend
                await _safe_send({
                    "type": "task_backgrounded",
                    "task_id": ws_task_id,
                    "message": "任务已进入后台，完成后自动恢复",
                    "session_id": ws_session_id
                })
                agent_is_running = False
                return response

            if ws_task_id:
                summary = response[:200] if response else ""
                # Update task title from agent's first response line
                if response and not response.startswith("[MAX_ITERATIONS_REACHED]") and not is_backgrounded:
                    title = _extract_task_title(response)
                    if title:
                        try:
                            tconn = sqlite3.connect(DB_PATH)
                            tconn.execute("UPDATE tasks SET title=? WHERE id=?", (title, ws_task_id))
                            tconn.commit()
                            tconn.close()
                        except Exception:
                            pass
                if is_max_iter:
                    # Save context for potential resume
                    save_task_context(ws_task_id, agent.messages[1:])
                    update_task_status(ws_task_id, "interrupted", summary, interruption_reason="max_iterations")
                    # Auto-detect as longrun if not already
                    try:
                        conn_tmp = sqlite3.connect(DB_PATH)
                        cur_tmp = conn_tmp.cursor()
                        cur_tmp.execute("SELECT task_type FROM tasks WHERE id=?", (ws_task_id,))
                        row_tmp = cur_tmp.fetchone()
                        conn_tmp.close()
                        if row_tmp and row_tmp[0] == 'oneshot':
                            update_task_type(ws_task_id, 'longrun')
                    except Exception:
                        pass
                elif response and ("interrupted by user" in response.lower() or "interrupted" in response.lower()):
                    save_task_context(ws_task_id, agent.messages[1:])
                    update_task_status(ws_task_id, "interrupted", summary, interruption_reason="user")
                else:
                    save_task_context(ws_task_id, agent.messages[1:])
                    _record_task_deliverables(ws_task_id)
                    update_task_status(ws_task_id, "completed", summary)
                
                # Update total tokens in tasks table from stats
                try:
                    stats = get_stats_manager().get_task_usage(ws_task_id)
                    if stats:
                        conn_tmp = sqlite3.connect(DB_PATH)
                        conn_tmp.execute("UPDATE tasks SET total_tokens = ?, total_cost = ?, prompt_tokens = ?, completion_tokens = ?, cached_tokens = ? WHERE id = ?",
                                         (stats["total"], stats.get("cost", 0.0), stats.get("prompt", 0), stats.get("completion", 0), stats.get("cached", 0), ws_task_id))
                        conn_tmp.commit()
                        conn_tmp.close()
                except Exception:
                    pass
            
            return response
        except Exception as e:
            if ws_task_id:
                # Save context so failed tasks can also be resumed
                if agent:
                    try:
                        save_task_context(ws_task_id, agent.messages[1:])
                    except Exception:
                        pass
                update_task_status(ws_task_id, "failed", str(e)[:200], interruption_reason="error")
            raise
        finally:
            agent_is_running = False

    try:
        while True:
            if not ws_alive:
                print("[WS] Not alive, exiting main loop")
                break
            config = load_config()
            heartbeat_enabled = config.get("heartbeat_enabled", False)
            heartbeat_interval = config.get("heartbeat_interval", 180)

            try:
                # Wait for user message with timeout for heartbeat
                if receive_task is None:
                    receive_task = asyncio.create_task(websocket.receive_text())
                
                timeout = heartbeat_interval if heartbeat_enabled else None
                
                # Check if we already have a finished receive_task result from a previous agent run_turn interrupt
                if receive_task.done():
                    data = receive_task.result()
                    receive_task = None 
                else:
                    done, pending = await asyncio.wait([receive_task], timeout=timeout)
                    if receive_task in done:
                        data = receive_task.result()
                        receive_task = None
                    else:
                        # ── Timeout: no user message received ──
                        # Recovery is handled by the background guardian.
                        continue
                        if not is_heartbeat:
                            continue

                user_msg = json.loads(data)
                msg_type = user_msg.get("type", "query")
                resume_id_for_run = None

                if msg_type == "sandbox_response":
                    # Resolve a pending sandbox auth wait
                    sid = user_msg.get("session_id", ws_session_id)
                    action = user_msg.get("action", "deny_once")
                    wait = _sandbox_waits.get(sid)
                    if wait:
                        wait["result"]["action"] = action
                        wait["result"]["path"] = user_msg.get("path", "")
                        wait["event"].set()
                        print(f"[WS] Sandbox response: {action} for session {sid}")
                    elif action in ("approve_once", "approve_dir", "approve_always", "approve_session"):
                        # Late approval after wait timed out — save and resume task
                        _path = user_msg.get("path", "")
                        if _path:
                            print(f"[WS] Late sandbox approval: {action} for {_path}")
                            _pending_sandbox_approvals.setdefault(sid, []).append({
                                "action": action, "path": _path
                            })
                            # Find backgrounded/interrupted task for this session and resume
                            try:
                                _late = sqlite3.connect(DB_PATH)
                                _late_t = _late.execute(
                                    "SELECT id, user_query FROM tasks WHERE session_id=? AND status IN ('backgrounded','interrupted') ORDER BY id DESC LIMIT 1",
                                    (sid,)).fetchone()
                                _late.close()
                                if _late_t:
                                    _tid2 = _late_t[0]
                                    _uq2 = _late_t[1] or ""
                                    _ctx2 = get_task_context(_tid2)
                                    if _ctx2 is None:
                                        _ctx2 = []
                                    _ctx2.append({"role": "user", "content":
                                        f"【系统通知】你之前因沙箱权限等待超时而中断。路径 {_path} 已获得用户授权，"
                                        f"请重新尝试之前被阻止的操作。"})
                                    save_task_context(_tid2, _ctx2)
                                    update_task_status(_tid2, "interrupted",
                                        "延迟授权触发恢复", interruption_reason="background_complete")
                                    print(f"[WS] Resuming task #{_tid2} after late sandbox approval")
                                    import threading as _thr
                                    _thr.Thread(
                                        target=_run_background_task,
                                        args=(_tid2, _uq2, _ctx2, True),
                                        daemon=True
                                    ).start()
                            except Exception as _late_err:
                                print(f"[WS] Late sandbox resume error: {_late_err}")
                    continue

                if msg_type == "resume":
                    # Resume an interrupted task
                    task_id = user_msg.get("task_id")
                    if task_id and not agent_is_running:
                        resume_id_for_run = task_id
                        try:
                            ctx = get_task_context(task_id)
                            # Always load steps for replay and context
                            conn2 = sqlite3.connect(DB_PATH)
                            conn2.row_factory = sqlite3.Row
                            steps = conn2.execute(
                                "SELECT step_number, tool_name, tool_label, args_preview, "
                                "result_preview, full_result, full_args, success FROM task_steps "
                                "WHERE task_id=? ORDER BY created_at", (task_id,)).fetchall()
                            # Also fetch the original task goal
                            task_row = conn2.execute(
                                "SELECT user_query FROM tasks WHERE id=?", (task_id,)).fetchone()
                            conn2.close()
                            await _safe_send({
                                "type": "history_steps",
                                "task_id": task_id,
                                "task_status": "resuming",
                                "steps": [dict(s) for s in steps]
                            })
                            if ctx:
                                session_history = ctx
                            original_goal = (task_row["user_query"] if task_row else "")
                            query = "【系统提示】任务已恢复，请根据历史上下文，从上次中断的地方继续执行任务。"
                            # Append extra instruction if provided by user
                            extra = user_msg.get("extra_instruction", "").strip()
                            if extra:
                                query += f"\n\n用户附加指令：{extra}"
                        except Exception as e:
                            print(f"[WS] Resume error: {e}")
                            query = "继续执行未完成的任务。"
                            extra = user_msg.get("extra_instruction", "").strip()
                            if extra:
                                query += f"\n\n用户附加指令：{extra}"
                        retry_model = None
                        agent_profile_name = None
                        ws_images = None
                    else:
                        continue
                elif msg_type == "retry":
                    query = user_msg.get("query", last_query)
                    retry_model = user_msg.get("model", None)
                    agent_profile_name = user_msg.get("agent_name", None)
                    ws_images = user_msg.get("images", None)
                    if not query.strip():
                        continue
                else:
                    query = user_msg.get("query", "")
                    retry_model = None
                    agent_profile_name = user_msg.get("agent_name", None)
                    ws_images = user_msg.get("images", None)
                    if not query.strip():
                        continue

                    # Save user message to DB
                    save_message("user", query, ws_session_id)

                    # Auto-reconstruct context for continuation queries
                    _resolved_todo = _resolve_todo_for_query(query)
                    if _resolved_todo > 0:
                        try:
                            from tools.task_plan import load_todos as _ct_load
                            _ct_todos = _ct_load()
                            for _ct_item in _ct_todos.get("items", []):
                                if _ct_item["id"] == _resolved_todo:
                                    query += f"\n\n[关联待办 #{_resolved_todo}] {_ct_item['desc']}"
                                    break
                        except Exception:
                            pass

                        if not resume_id_for_run:
                            try:
                                conn_cont = sqlite3.connect(DB_PATH)
                                conn_cont.row_factory = sqlite3.Row
                                latest_task = conn_cont.execute(
                                    "SELECT id FROM tasks "
                                    "WHERE session_id=? AND status IN ('interrupted','backgrounded','background_failed')"
                                    "ORDER BY id DESC LIMIT 1",
                                    (ws_session_id,)
                                ).fetchone()
                                if latest_task:
                                    resume_id_for_run = latest_task["id"]
                                    session_history = get_task_context(resume_id_for_run)
                                conn_cont.close()
                            except Exception as e:
                                print(f"[WS] Continuation context error: {e}")


                # Send thinking status
                await _safe_send({
                    "type": "status",
                    "message": "Agent is thinking...",
                    "session_id": ws_session_id
                })

                # Run the agent
                response = await run_agent_with_progress(query, retry_model, agent_profile_name, images=ws_images, resume_task_id=resume_id_for_run)

                # Save and send the response
                save_message("agent", response, ws_session_id)


                # Send the final response
                await _safe_send({
                    "type": "message",
                    "role": "agent",
                    "content": response,
                    "session_id": ws_session_id
                })
                
            except (WebSocketDisconnect, RuntimeError) as _ws_err:
                # WebSocketDisconnect doesn't contain "disconnect" in str() output.
                # Check by type for WebSocketDisconnect or message for RuntimeError.
                if isinstance(_ws_err, WebSocketDisconnect) or "disconnect" in str(_ws_err).lower():
                    print("[WS] Client disconnected")
                    break
                # Not a disconnect — re-raise
                raise
            except Exception as e:
                import traceback
                traceback.print_exc()
                err_str = str(e).lower()
                # Log error to stderr only — don't pollute the chat session
                log_agent_error(str(e))
                print(f"[Agent Error] {e}")
                # Only show API key hint in chat (actionable by user); hide internal errors
                if "api_key" in err_str or "authentication" in err_str or "not found" in err_str or "key" in err_str:
                    hint = (
                        "---\n**💡 提示：您似乎尚未配置此模型的 API Key！**\n\n"
                        "以 DeepSeek 为例，请前往 [DeepSeek 开放平台](https://platform.deepseek.com/api_keys) "
                        "免费申请一个 API Key，然后在左侧边栏的「设置 - 模型配置」中填入并保存即可开始对话！"
                    )
                    save_message("system", hint, ws_session_id)
                    await _safe_send({
                        "type": "error",
                        "content": hint,
                        "session_id": ws_session_id
                    })
                else:
                    # Non-actionable errors: just a brief notification, no full stack in chat
                    print(f"[Agent Error] Full traceback above. Hiding from chat to avoid clutter.")
                
    except WebSocketDisconnect:
        print("Client disconnected")
        if websocket in connected_websockets:
            connected_websockets.remove(websocket)
        _active_agents.pop(ws_session_id, None)  # nested dict cleaned up
        _session_enabled_tools.pop(ws_session_id, None)
    except Exception as e:
        print(f"WebSocket error: {e}")
        if websocket in connected_websockets:
            connected_websockets.remove(websocket)
        _active_agents.pop(ws_session_id, None)  # nested dict cleaned up
        _session_enabled_tools.pop(ws_session_id, None)

def start_email_listener():
    def email_listener_loop():
        from core.email_service import fetch_emails, send_email
        from agent.agent import OpenAGCAgent
        while True:
            try:
                config = load_config()
                # Query all sessions with email enabled
                try:
                    conn = sqlite3.connect(DB_PATH)
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        "SELECT id, email_account, email_password, email_imap_server, "
                        "email_smtp_server, owner_email FROM sessions "
                        "WHERE email_enabled=1 AND email_account!='' AND "
                        "email_password!='' AND email_imap_server!=''"
                    ).fetchall()
                    conn.close()
                except Exception:
                    rows = []

                for row in rows:
                    sess_id = row["id"]
                    try:
                        owner = row["owner_email"] or ""
                        criteria = f'UNSEEN FROM "{owner}"' if owner else 'UNSEEN'
                        emails = fetch_emails(
                            row["email_imap_server"],
                            row["email_account"],
                            row["email_password"],
                            criteria=criteria,
                            limit=5,
                            mark_seen=True
                        )
                        for e in emails:
                            print(f"[Email Listener] Session {sess_id}: new command from {owner}: {e['subject']}")
                            save_message("system",
                                f"📧 已收到来自主人 ({owner}) 的新邮件指令:\n主题: {e['subject']}",
                                session_id=sess_id)

                            agent = OpenAGCAgent(
                                model=config.get("default_model", "gpt-4o"),
                                session_id=sess_id
                            )
                            prompt = f"I received a new email instruction from my owner ({owner}).\nSubject: {e['subject']}\nBody: {e['body']}\nPlease execute this instruction, and then I will automatically email them the result."

                            try:
                                response = agent.run_turn(prompt)
                            except Exception as ex:
                                response = f"Failed to execute instructions: {ex}"

                            success = send_email(
                                row["email_smtp_server"],
                                row["email_account"],
                                row["email_password"],
                                owner,
                                f"Re: {e['subject']} - Task Completed",
                                f"Task Summary:\n\n{response}"
                            )
                            if success:
                                save_message("system",
                                    f"📧 已将执行结果回传至主人邮箱: {owner}",
                                    session_id=sess_id)
                            else:
                                save_message("system",
                                    f"⚠️ 邮件回复发送失败，请检查 SMTP 配置。",
                                    session_id=sess_id)
                    except Exception as e:
                        print(f"[Email Listener] Session {sess_id} error: {e}")
            except Exception as e:
                print(f"Email listener error: {e}")
            _time.sleep(60)

    threading.Thread(target=email_listener_loop, daemon=True).start()

# ==========================================
# Task Scheduler (Background Thread)
# ==========================================

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
        try: connected_websockets.remove(ws)
        except ValueError: pass


def _broadcast_task_history(task_id: int, session_id: int, task_status: str = "interrupted"):
    """Fetch task steps and broadcast as history_steps for UI rendering."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT step_number, tool_name, tool_label, args_preview, result_preview, full_result, full_args, success "
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
            })
        _broadcast_to_websockets({
            "type": "history_steps",
            "task_id": task_id,
            "session_id": session_id,
            "steps": steps,
            "task_status": task_status,
        })
    except Exception as e:
        print(f"[Task] Failed to broadcast task history: {e}")

def _get_task_step_count(task_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cnt = conn.execute("SELECT COUNT(*) FROM task_steps WHERE task_id=?", (task_id,)).fetchone()[0]
    conn.close()
    return cnt


# Wire route modules to shared state
init_benchmark_routes(
    db_path=DB_PATH,
    download_state=_llamacpp_download_state,
    install_state={},
    broadcast_fn=_broadcast_to_websockets,
    get_engine=lambda: None,
    get_llamacpp=get_llamacpp_manager,
    load_config=load_config
)
init_download_routes(
    db_path=DB_PATH,
    download_state=_llamacpp_download_state,
    install_state={"active": False, "stage": "idle", "label": "", "progress": 0, "error": ""},
    broadcast_fn=_broadcast_to_websockets,
    training_avail=False,
    get_llamacpp=get_llamacpp_manager,
    load_config=load_config
)


def _run_background_task(task_id: int, user_query: str, context_messages: list = None,
                         is_resume: bool = False):
    """Execute a task in background (no WebSocket). Results saved to DB and pushed to clients."""
    from agent.agent import OpenAGCAgent

    config = load_config()
    model = config.get("default_model", "moonshot/kimi-latest")

    # Look up session_id BEFORE creating agent, so agent has correct session_id
    # for sandbox auth (_sandbox_waits key must match frontend's session_id)
    bg_session_id = 1
    try:
        bg_conn = sqlite3.connect(DB_PATH)
        row = bg_conn.execute("SELECT session_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row and row[0]:
            bg_session_id = row[0]
        bg_conn.close()
    except Exception:
        pass

    agent = OpenAGCAgent(model=model, session_id=bg_session_id)
    _apply_pending_sandbox_approvals(agent, bg_session_id)

    step_counter = 0
    step_offset = 0
    if is_resume:
        try:
            off_conn = sqlite3.connect(DB_PATH)
            off_conn.row_factory = sqlite3.Row
            max_step = off_conn.execute(
                "SELECT COALESCE(MAX(step_number), 0) FROM task_steps WHERE task_id=?",
                (task_id,)).fetchone()[0]
            step_offset = max_step
            off_conn.close()
        except Exception:
            pass

    # Detect heartbeat tasks — suppress all progress broadcasts and chat messages
    _is_heartbeat = False
    try:
        _hb_conn = sqlite3.connect(DB_PATH)
        _hb_row = _hb_conn.execute("SELECT task_type FROM tasks WHERE id=?", (task_id,)).fetchone()
        if _hb_row and _hb_row[0] == 'heartbeat':
            _is_heartbeat = True
        _hb_conn.close()
    except Exception:
        pass

    def progress_cb(event: dict):
        nonlocal step_counter
        # Persist sandbox approvals so they survive agent recreation on task resume
        if event.get("event") == "sandbox_approved":
            _path = event.get("path", "")
            _sid = event.get("session_id") or bg_session_id
            if _path:
                _pending_sandbox_approvals.setdefault(_sid, []).append({
                    "action": "approve_once", "path": _path
                })
            return
        # Suppress all progress broadcasts for heartbeat tasks
        if _is_heartbeat:
            return
        if event.get("event") == "tool_start":
            step_counter += 1
            display_step = event.get("step", step_counter) + step_offset
            event["step"] = display_step
            try:
                add_task_step(
                    task_id=task_id,
                    step_number=display_step,
                    tool_name=event.get("tool", ""),
                    tool_label=event.get("tool_label", ""),
                    args_preview=event.get("args_preview", ""),
                    session_id=bg_session_id,
                    tool_call_id=event.get("tool_call_id"),
                    full_args=event.get("tool_args")
                )
            except Exception:
                pass
        elif event.get("event") == "tool_done":
            done_step = event.get("step", step_counter) + step_offset
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE task_steps SET result_preview=?, full_result=?, success=? WHERE task_id=? AND step_number=?",
                    (event.get("result_preview", ""), event.get("full_result", event.get("result_preview", "")),
                     1 if event.get("success") else 0, task_id, done_step)
                )
                conn.commit()
                conn.close()
            except Exception:
                pass

        # Push progress to connected clients with session_id for proper routing
        event["task_id"] = task_id
        event["session_id"] = bg_session_id
        event["background"] = True
        _broadcast_to_websockets({"type": "progress", **event})
    
    # Inject saved context if resuming
    if context_messages:
        agent.messages.extend(context_messages)

    query = user_query
    if is_resume and not _is_heartbeat:
        query = (f"【系统指令 - 自动恢复】你之前因为执行步骤过多被系统自动中断了。"
                 f"请根据之前的上下文继续完成未完成的任务。"
                 f"原始任务: {user_query}")

        # Broadcast reconstructed history steps so they appear in the session
        _broadcast_task_history(task_id, bg_session_id, "running")

    update_task_status(task_id, "running")

    # Register so the WS/REST interrupt handler can stop this agent
    _background_agents[task_id] = agent

    # Adopt any orphan shell processes waiting for this task
    try:
        from tools.shell import adopt_orphan_processes
        adopted = adopt_orphan_processes(task_id, session_id=bg_session_id)
        if adopted:
            print(f"[BgTask] Task #{task_id}: adopted {adopted} orphan process(es)")
    except Exception as e:
        print(f"[BgTask] Orphan adoption error: {e}")

    # Notify connected clients (skip for heartbeat tasks)
    if not _is_heartbeat:
        step_count_str = ""
        if is_resume:
            try:
                _sc = _get_task_step_count(task_id)
                if _sc:
                    step_count_str = f"，共 {_sc} 个历史步骤"
            except Exception:
                pass

        resume_msg = (
            f"🔄 **任务自动恢复**\n\n"
            f"任务 **#{task_id}** 已自动恢复执行{step_count_str}，正在继续之前未完成的工作。\n\n"
            f"[📋 查看任务详情](task://{task_id}) · [🔗 任务中心](switch:view/tasks)"
        ) if is_resume else (
            f"⏰ **定时任务执行**\n\n"
            f"任务 **#{task_id}**: {user_query[:80]}"
        )
        _broadcast_to_websockets({
            "type": "message",
            "role": "system",
            "session_id": bg_session_id,
            "content": resume_msg
        })
    
    try:
        msg_count_before = len(agent.messages)
        response = agent.run_turn(query, False, progress_cb, task_id=task_id, skip_rag=bool(context_messages))

        # If user already interrupted this task, don't overwrite the status
        try:
            chk_conn = sqlite3.connect(DB_PATH)
            chk_row = chk_conn.execute("SELECT status, interruption_reason FROM tasks WHERE id=?", (task_id,)).fetchone()
            chk_conn.close()
            if chk_row and chk_row[0] == "interrupted" and chk_row[1] == "user":
                print(f"[BgTask] Task #{task_id} was user-interrupted, skipping status update")
                return response or ""
        except Exception:
            pass

        is_max_iter = response and response.startswith("[MAX_ITERATIONS_REACHED]")
        is_backgrounded = response and response.startswith("[TASK_BACKGROUNDED]")

        summary = response[:200] if response else ""
        if is_backgrounded:
            # Agent auto-backgrounded (shell timeout) — save context for resume
            save_task_context(task_id, agent.messages[msg_count_before:])
            update_task_status(task_id, "backgrounded",
                response[len("[TASK_BACKGROUNDED] "):].strip() or "任务进入后台",
                interruption_reason="backgrounded")
            _broadcast_to_websockets({
                "type": "task_backgrounded",
                "task_id": task_id,
                "message": "后台命令执行中，完成后自动恢复",
                "session_id": bg_session_id,
            })
            return response
        elif is_max_iter:
            save_task_context(task_id, agent.messages[msg_count_before:])
            update_task_status(task_id, "interrupted", summary, interruption_reason="max_iterations")
        else:
            save_task_context(task_id, agent.messages[msg_count_before:] if agent else [])
            _record_task_deliverables(task_id)
            update_task_status(task_id, "completed", summary)
        if response and not is_max_iter and not is_backgrounded:
            title = _extract_task_title(response)
            if title:
                try:
                    tconn = sqlite3.connect(DB_PATH)
                    tconn.execute("UPDATE tasks SET title=? WHERE id=?", (title, task_id))
                    tconn.commit()
                    tconn.close()
                except Exception:
                    pass

        # Push final result to clients (skip heartbeat tasks entirely)
        if not _is_heartbeat:
            _broadcast_to_websockets({
                "type": "message",
                "role": "agent",
                "background": True,
                "session_id": bg_session_id,
                "content": f"**{'🔄 自动恢复' if is_resume else '⏰ 定时'}任务完成**: {user_query[:40]}...\n\n{response[:500]}"
            })

        return response
    except Exception as e:
        update_task_status(task_id, "failed", str(e)[:200], interruption_reason="error")
        _broadcast_to_websockets({
            "type": "error",
            "session_id": bg_session_id,
            "content": f"后台任务失败: {str(e)[:100]}"
        })
        return None
    finally:
        _background_agents.pop(task_id, None)

def start_task_scheduler():
    """Background thread that handles scheduled tasks only.
    Auto-resume is handled by the guardian loop (Phase 1 + Phase 2)."""
    def scheduler_loop():
        print("[TaskScheduler] Started")
        while True:
            try:
                conn = sqlite3.connect(DB_PATH)
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
                
                # 1. Check scheduled tasks due for execution
                cursor.execute(
                    "SELECT * FROM tasks WHERE task_type='scheduled' AND schedule_enabled=1 AND next_run_at <= ? AND status != 'running'",
                    (now_utc,)
                )
                due_tasks = cursor.fetchall()
                
                for task in due_tasks:
                    task_id = task["id"]
                    print(f"[TaskScheduler] Executing scheduled task #{task_id}: {task['title']}")
                    
                    # Update next_run_at and run_count
                    try:
                        from croniter import croniter
                        next_run = croniter(task["schedule_cron"], datetime.now(timezone.utc)).get_next(datetime).strftime('%Y-%m-%d %H:%M:%S')
                        cursor.execute(
                            "UPDATE tasks SET next_run_at=?, last_run_at=?, run_count=run_count+1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (next_run, now_utc, task_id)
                        )
                        conn.commit()
                    except Exception as e:
                        print(f"[TaskScheduler] Cron error for task #{task_id}: {e}")
                        continue
                    
                    # Execute in a separate thread to avoid blocking the scheduler
                    threading.Thread(
                        target=_run_background_task,
                        args=(task_id, task["user_query"]),
                        daemon=True
                    ).start()


                conn.close()
            except Exception as e:
                print(f"[TaskScheduler] Error: {e}")
            
            _time.sleep(30)  # Check every 30 seconds
    
    threading.Thread(target=scheduler_loop, daemon=True).start()



# ── Backoff resume helpers ──

_BACKOFF_SCHEDULE = [30, 30, 120, 120, 300, 300]
_MAX_RESUME_WITH_PROGRESS = 50
_MAX_RESUME_NO_PROGRESS = 10

def _has_recent_progress(task_id: int) -> bool:
    """Check if a task has any successful tool steps in its history."""
    try:
        conn = sqlite3.connect(DB_PATH)
        count = conn.execute(
            "SELECT COUNT(*) FROM task_steps WHERE task_id=? AND success=1", (task_id,)
        ).fetchone()[0]
        conn.close()
        return count > 0
    except Exception:
        return False

def _get_backoff_delay(resume_count: int) -> int:
    """Return seconds to wait before next resume. Returns -1 when maxed out."""
    if resume_count >= len(_BACKOFF_SCHEDULE):
        return -1
    return _BACKOFF_SCHEDULE[resume_count]

def _is_backoff_elapsed(updated_at_str: str, resume_count: int) -> bool:
    """Check if backoff period has elapsed since task was last updated."""
    delay = _get_backoff_delay(resume_count)
    if delay < 0:
        return False
    try:
        from datetime import datetime, timezone
        if not updated_at_str:
            return True
        if 'T' not in updated_at_str:
            updated_at_str = updated_at_str.replace(' ', 'T')
        if not updated_at_str.endswith('Z') and '+' not in updated_at_str:
            updated_at_str += 'Z'
        updated = datetime.fromisoformat(updated_at_str)
        now = datetime.now(timezone.utc)
        return (now - updated).total_seconds() >= delay
    except Exception:
        return True


_SERVER_START_TIME = datetime.now(timezone.utc)  # Used by BgMonitor to detect restart-induced process loss

def start_background_monitor():
    """Monitor backgrounded tasks — check download/process completion and auto-resume."""
    def monitor_loop():
        import time as _t
        import os as _os
        _output_staleness = {}  # {task_id: {"size": int, "count": int}} for output file growth tracking
        while True:
            try:
                conn = sqlite3.connect(DB_PATH)
                conn.row_factory = sqlite3.Row
                bg_tasks = conn.execute(
                    "SELECT id, user_query, resume_count, max_resume_count, created_at, updated_at FROM tasks "
                    "WHERE status='backgrounded'"
                ).fetchall()
                if bg_tasks:
                    print(f"[BgMonitor] Found {len(bg_tasks)} backgrounded task(s) to check")
                for task in bg_tasks:
                    tid = task["id"]
                    # 1. Check downloads linked to this task
                    dl = conn.execute(
                        "SELECT id, status FROM downloads WHERE task_id=? AND status='completed' "
                        "AND background_resumed=0 ORDER BY id DESC LIMIT 1",
                        (tid,)).fetchone()
                    if dl:
                        print(f"[BgMonitor] Task {tid}: download {dl['id']} done — resuming")
                        conn.execute("UPDATE downloads SET background_resumed=1 WHERE id=?",
                                     (dl["id"],))
                        conn.commit()
                        ctx = get_task_context(tid)
                        if ctx:
                            ctx.append({"role": "user", "content": (
                                "【系统通知】后台下载任务已完成，文件已就绪。"
                                "请继续执行之前未完成的任务，不要重复下载已有文件。"
                            )})
                            increment_task_resume(tid)
                            update_task_status(tid, "interrupted",
                                "后台任务已完成", interruption_reason="background_complete")
                            save_task_context(tid, ctx)
                        continue
                    dl_fail = conn.execute(
                        "SELECT id, error_message FROM downloads WHERE task_id=? "
                        "AND status='failed' AND background_resumed=0 "
                        "ORDER BY id DESC LIMIT 1",
                        (tid,)).fetchone()
                    if dl_fail:
                        conn.execute("UPDATE downloads SET background_resumed=1 WHERE id=?",
                                     (dl_fail["id"],))
                        conn.commit()
                        err = dl_fail["error_message"] or "未知错误"
                        update_task_status(tid, "background_failed",
                            f"下载失败: {err}", interruption_reason="download_failed")
                        ctx = get_task_context(tid)
                        if ctx:
                            ctx.append({"role": "user", "content": (
                                f"【系统通知】后台下载任务失败了。错误信息: {err}\n"
                                "请检查下载管理器中的详细错误，尝试修复后重新下载。"
                            )})
                            save_task_context(tid, ctx)
                        print(f"[BgMonitor] Task {tid}: download failed — notifying agent")
                        continue

                    # 2. Check shell background processes
                    try:
                        from tools.shell import (get_background_processes, cleanup_background_process,
                                                  get_orphan_processes, cleanup_orphan_process,
                                                  adopt_orphan_processes)
                        bg_procs = get_background_processes()
                        pinfo = bg_procs.get(str(tid))

                        # Fallback 1: try orphan pool if main pool misses
                        if not pinfo:
                            orphan_procs = get_orphan_processes()
                            if orphan_procs:
                                # Try to adopt orphans matching this task's session/time
                                task_row = conn.execute(
                                    "SELECT session_id FROM tasks WHERE id=?", (tid,)
                                ).fetchone()
                                task_session = task_row["session_id"] if task_row else None
                                adopted = adopt_orphan_processes(tid, session_id=task_session)
                                if adopted:
                                    # Re-read after adoption
                                    bg_procs = get_background_processes()
                                    pinfo = bg_procs.get(str(tid))
                                    if pinfo:
                                        print(f"[BgMonitor] Task {tid}: adopted orphan process")

                        # Fallback 2: handle backgrounded tasks with no process info (e.g. after restart)
                        if not pinfo:
                            try:
                                updated_str = task["updated_at"] or task["created_at"]
                                if updated_str:
                                    updated_dt = datetime.strptime(updated_str, '%Y-%m-%d %H:%M:%S')
                                    updated_dt = updated_dt.replace(tzinfo=timezone.utc)
                                    now = datetime.now(timezone.utc)
                                    age = now - updated_dt

                                    # Only fail if task was backgrounded BEFORE this server started
                                    # (process info was lost during restart)
                                    if updated_dt < _SERVER_START_TIME:
                                        # Give 2h grace period — the shell process might still be running
                                        if age > timedelta(hours=2):
                                            print(f"[BgMonitor] Task {tid}: bg'd before server restart, no process info for {age.total_seconds()/60:.0f}min — marking failed")
                                            update_task_status(tid, "background_failed",
                                                "后台进程信息因服务重启丢失，无法恢复",
                                                interruption_reason="process_lost")
                                            ctx = get_task_context(tid)
                                            if ctx:
                                                ctx.append({"role": "user", "content": (
                                                    "【系统通知】后台命令的进程信息已丢失（可能因服务重启）。"
                                                    "请重新开始任务，或检查是否有残留进程需要手动处理。"
                                                )})
                                                save_task_context(tid, ctx)
                                            continue
                                    else:
                                        # Task bg'd after startup — process info should exist, don't fail.
                                        # Log periodically to aid debugging.
                                        mins = age.total_seconds() / 60
                                        if mins > 60 and int(mins) % 10 == 0:
                                            print(f"[BgMonitor] Task {tid}: bg'd post-startup, no process info for {mins:.0f}min (waiting)")
                            except Exception as ts_err:
                                print(f"[BgMonitor] Task {tid}: time-check error: {ts_err}")

                        if pinfo:
                            pid = pinfo.get("pid")
                            out_file = pinfo.get("output_file", "")
                            command = pinfo.get("command", "")
                            started_at = pinfo.get("started_at", 0)
                            uptime = _time.time() - started_at if started_at else 0
                            is_long_running = uptime > 1800  # 30+ minutes
                            should_resume = False
                            try:
                                os.kill(pid, 0)  # No signal, just check existence
                                # Process still running — check if output file stopped growing
                                if out_file and _os.path.exists(out_file):
                                    cur_size = _os.path.getsize(out_file)
                                    prev = _output_staleness.get(str(tid), {})
                                    prev_size = prev.get("size", -1)
                                    if cur_size == prev_size and cur_size >= 0:
                                        # File not growing — increment staleness counter
                                        new_count = prev.get("count", 0) + 1
                                        _output_staleness[str(tid)] = {"size": cur_size, "count": new_count}
                                        if is_long_running:
                                            # Long-running server: 15-min output freeze
                                            # Clean up process tracking so BgMonitor stops checking it.
                                            # The task stays backgrounded — user can resume via UI if needed.
                                            if new_count >= 90:  # 90 * 10s = 15min
                                                print(f"[BgMonitor] Task {tid}: long-running ({uptime/60:.0f}min), output frozen 15min — removing process tracking")
                                                cleanup_background_process(str(tid))
                                                _output_staleness.pop(str(tid), None)
                                                ctx = get_task_context(tid)
                                                if ctx:
                                                    ctx.append({"role": "user", "content": (
                                                        f"【系统通知】后台进程（PID {pid}）已持续运行 {uptime/60:.0f} 分钟无输出，"
                                                        f"已解除进程追踪。进程可能仍在后台运行，也可手动在任务管理中终止。"
                                                    )})
                                                    save_task_context(tid, ctx)
                                                continue
                                        else:
                                            # Normal process: 30s output freeze → resume
                                            if new_count >= 3:
                                                should_resume = True
                                                print(f"[BgMonitor] Task {tid}: output stale 30s — treating as done")
                                    else:
                                        # File still growing — reset staleness
                                        _output_staleness[str(tid)] = {"size": cur_size, "count": 0}
                            except OSError:
                                # Process has terminated — resume task
                                should_resume = True
                                cleanup_background_process(str(tid))

                            if not should_resume:
                                continue  # Process still active, skip this check

                            # ── Common resume path (process dead OR output stalled) ──
                            _output_staleness.pop(str(tid), None)
                            cleanup_background_process(str(tid))
                            full_out = ""
                            if out_file and _os.path.exists(out_file):
                                try:
                                    with open(out_file, "r", encoding="utf-8", errors="replace") as rf:
                                        full_out = rf.read()[-5000:]
                                except Exception:
                                    pass
                                try:
                                    _os.remove(out_file)
                                except Exception:
                                    pass
                            ctx = get_task_context(tid)
                            if ctx:
                                ctx.append({"role": "user", "content": (
                                    f"【系统通知】后台命令已执行完毕。\n"
                                    f"命令: `{command[:100]}`\n"
                                    f"输出:\n```\n{full_out[:2000]}\n```\n"
                                    f"请根据输出结果继续执行之前未完成的任务。"
                                )})
                                save_task_context(tid, ctx)
                            increment_task_resume(tid)
                            task_row = conn.execute(
                                "SELECT user_query FROM tasks WHERE id=?", (tid,)
                            ).fetchone()
                            user_query = task_row["user_query"] if task_row else ""
                            print(f"[BgMonitor] Task {tid}: shell process done — resuming")
                            threading.Thread(
                                target=lambda _tid=tid, _uq=user_query, _ctx=ctx: (
                                    update_task_status(_tid, "interrupted",
                                        "后台命令完成", interruption_reason="background_complete"),
                                    _run_background_task(_tid, _uq, _ctx, True)
                                ),
                                daemon=True
                            ).start()
                    except Exception as e:
                        print(f"[BgMonitor] Shell process check error: {e}")

                conn.close()
            except Exception as e:
                print(f"[BgMonitor] Error: {e}")
            _t.sleep(10)
    threading.Thread(target=monitor_loop, daemon=True).start()

def _guardian_resume_task(task_id: int) -> None:
    """Resume an interrupted task: load context, mark running, execute one turn."""
    if not _guardian_resume_lock.acquire(blocking=False):
        print(f"[Guardian] Resume #{task_id}: lock held, skipping")
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        conn.close()
        if not row or row[0] != 'interrupted':
            print(f"[Guardian] Resume #{task_id}: not found or not interrupted")
            return

        cfg = load_config()
        model = cfg.get("default_model", "moonshot/kimi-latest")
        from agent.agent import OpenAGCAgent

        # Look up session_id BEFORE creating agent, so sandbox auth (_sandbox_waits)
        # uses the correct key that matches the frontend's session_id
        _hb_session = 1
        try:
            _hb_c = sqlite3.connect(DB_PATH)
            _hb_r = _hb_c.execute("SELECT session_id FROM tasks WHERE id=?", (task_id,)).fetchone()
            if _hb_r: _hb_session = _hb_r[0]
            _hb_c.close()
        except Exception:
            pass

        agent = OpenAGCAgent(model=model, session_id=_hb_session)
        _apply_pending_sandbox_approvals(agent, _hb_session)
        try:
            ctx = get_task_context(task_id)
            if ctx:
                # Trim context to avoid 138K token first call: keep first 2 + last 15 msgs
                if len(ctx) > 20:
                    total_chars = sum(len(m.get("content", "") or "") for m in ctx)
                    if total_chars > 20000:
                        ctx = ctx[:2] + ctx[-15:]
                agent.messages.extend(ctx)
        except Exception:
            pass
        update_task_status(task_id, "running")

        def _hb_cb(e):
            if e.get("event") == "tool_start":
                add_task_step(task_id, e.get("step", 0), e.get("tool", ""), e.get("tool_label", ""), args_preview=e.get("args_preview", ""), session_id=_hb_session)
            # Persist sandbox approvals so they survive agent recreation
            if e.get("event") == "sandbox_approved":
                _path = e.get("path", "")
                if _path:
                    _pending_sandbox_approvals.setdefault(_hb_session, []).append({
                        "action": "approve_once", "path": _path
                    })
                return
            _broadcast_to_websockets({"type": "progress", "session_id": _hb_session, "task_id": task_id, **e})

        _background_agents[task_id] = agent
        try:
            resp = agent.run_turn(
                "【系统恢复】之前执行中断，请继续完成原任务目标。",
                verbose=False,
                progress_callback=_hb_cb,
                skip_rag=True,
                task_id=task_id,
            )
        finally:
            _background_agents.pop(task_id, None)

        # Update task status after run_turn completes
        _resp_str = str(resp or "")[:200]
        if agent.is_interrupted:
            update_task_status(task_id, "interrupted", _resp_str, interruption_reason="user")
        elif "MAX_ITERATIONS_REACHED" in _resp_str:
            # Only increment resume_count for max_iterations — server restart,
            # sandbox timeout, etc. should not count toward the limit.
            try:
                _hb_c2 = sqlite3.connect(DB_PATH)
                _hb_c2.execute("UPDATE tasks SET resume_count = resume_count + 1 WHERE id=?", (task_id,))
                _hb_c2.commit()
                _hb_c2.close()
            except Exception:
                pass
            update_task_status(task_id, "interrupted", _resp_str, interruption_reason="max_iterations")
        elif _resp_str.startswith("[TASK_BACKGROUNDED]"):
            try:
                save_task_context(task_id, agent.messages[1:])
            except Exception:
                pass
            update_task_status(task_id, "backgrounded", _resp_str, interruption_reason="backgrounded")
        elif hasattr(agent, '_consecutive_failures') and agent._consecutive_failures >= 3:
            update_task_status(task_id, "interrupted", _resp_str, interruption_reason="error")
        else:
            save_task_context(task_id, agent.messages[1:])
            _record_task_deliverables(task_id)
            update_task_status(task_id, "completed", _resp_str)
    except Exception as e:
        print(f"[Guardian] Resume #{task_id} error: {e}")
        try:
            update_task_status(task_id, "background_failed",
                f"自动恢复失败: {str(e)[:100]}",
                interruption_reason="error")
        except Exception:
            pass
    finally:
        _guardian_resume_lock.release()


def start_guardian_loop():
    """Background guardian — pure code-based polling, no LLM calls."""
    def _guardian_loop():
        while True:
            try:
                cfg = load_config()
                if not cfg.get("heartbeat_enabled", False):
                    _time.sleep(30)
                    continue

                interval = cfg.get("heartbeat_interval", 180)

                conn = sqlite3.connect(DB_PATH)
                if conn.execute("SELECT 1 FROM tasks WHERE status='running' LIMIT 1").fetchone():
                    conn.close()
                    _time.sleep(max(interval, 10))
                    continue
                row = conn.execute(
                    "SELECT id FROM tasks WHERE status='interrupted' AND resume_count < max_resume_count AND (interruption_reason IS NULL OR interruption_reason != 'user') ORDER BY updated_at DESC LIMIT 1"
                ).fetchone()
                conn.close()

                if row:
                    tid = row[0]
                    print(f"[Guardian] Found interrupted task #{tid}, resuming...")
                    _guardian_resume_task(tid)

                _time.sleep(max(interval, 10))
            except Exception as e:
                print(f"[Guardian] Error: {e}")
                _time.sleep(30)

    threading.Thread(target=_guardian_loop, daemon=True).start()
    print("[Guardian] Started (code-based polling)")


# Start background listeners
start_background_monitor()
start_email_listener()
start_task_scheduler()
start_guardian_loop()