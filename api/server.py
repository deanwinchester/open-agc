import os
import sys
import json
import asyncio
import sqlite3
import threading
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
                tid = t["id"]
                ctx = get_task_context(tid)
                if ctx:
                    ctx.append({"role": "user", "content": (
                        "【系统通知】服务器重启，后台命令的进程信息已丢失。"
                        "请检查之前的工作状态，如有需要请重新执行。"
                    )})
                    save_task_context(tid, ctx)
                update_task_status(tid, "background_failed",
                    "服务器重启，后台进程信息丢失", interruption_reason="process_lost")
                print(f"[Startup] Task {tid}: marked background_failed (process info lost on restart)")

        conn.close()
    except Exception as e:
        print(f"[Startup] Background recovery error: {e}")

reconcile_backgrounded_after_restart()

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
    return task_id

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


def _is_continuation_query(query: str) -> bool:
    """Heuristic: does this query look like a continuation of the previous task?"""
    q = query.strip()
    if not q:
        return False
    # Very short queries (<15 chars Chinese, <20 chars English) are likely continuations
    if len(q) < 15:
        return True
    # Starts with continuation markers
    q_lower = q.lower()
    for prefix in _CONTINUATION_PREFIXES:
        if q_lower.startswith(prefix):
            return True
    # Single entity name (no verb), 2-6 Chinese chars without action verbs
    import re
    has_verb = re.search(r'[下载搜索查找播放打开创建删除修改更新]', q) is not None
    if not has_verb and 2 <= len(re.findall(r'[一-鿿]', q)) <= 8:
        return True
    return False


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

                if is_recent and _is_continuation_query(query):
                    print(f"[Task] Continuing task {tid} for session {session_id} (continuation: {query[:50]})")
                    update_task_status(tid, "running")
                    return tid
    except Exception as e:
        print(f"[Task] Error resolving task: {e}")

    # Create a brand-new task
    title = _extract_task_title(query) or query[:60]
    if len(title) >= 60:
        title = title[:57] + '...'
    tid = create_task(title, query, session_id=session_id)
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
    # Only keep last 30 messages to avoid huge snapshots
    snapshot = json.dumps(messages[-30:], ensure_ascii=False)
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
            conn.close()
            if ctx:  # valid non-empty snapshot
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
        "FROM task_steps WHERE task_id=? ORDER BY step_number ASC", (task_id,))
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

def add_task_step(task_id: int, step_number: int, tool_name: str, tool_label: str = None,
                  args_preview: str = None, result_preview: str = None, full_result: str = None,
                  success: bool = True, thinking_content: str = None, session_id: int = None,
                  tool_call_id: str = None, full_args: str = None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO task_steps (task_id, step_number, tool_name, tool_label, args_preview, "
        "result_preview, full_result, success, thinking_content, session_id, tool_call_id, full_args) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, step_number, tool_name, tool_label, args_preview, result_preview, full_result,
         1 if success else 0, thinking_content, session_id, tool_call_id, full_args)
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
            cursor.execute("SELECT task_id, label, filename FROM downloads WHERE id=?", (download_id,))
            dl_row = cursor.fetchone()
            if dl_row:
                task_id = dl_row[0]
                label = dl_row[1] or dl_row[2] or f"download #{download_id}"
                if task_id:
                    cursor.execute(
                        "SELECT session_id FROM task_steps WHERE task_id=? AND session_id IS NOT NULL LIMIT 1",
                        (task_id,))
                    sid_row = cursor.fetchone()
                    session_id = sid_row[0] if sid_row else 1

                    if status == 'completed':
                        save_message("system",
                            f"✅ 下载完成: {label}", session_id)
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
                                        "请继续执行之前未完成的任务，不要重复下载已有文件。"
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

_active_agents: dict = {}  # {session_id: OpenAGCAgent} — for non-blocking message injection
_background_agents: dict = {}  # {task_id: OpenAGCAgent} — background tasks for interrupt

_session_enabled_tools: dict = {}  # {session_id: set(tool_names)} — progressive tool persistence

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
            "huggingface": ""
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
        "heartbeat_interval": 60,
        "email_listener_enabled": False,
        "email_account": "",
        "email_password": "",
        "email_imap_server": "",
        "email_smtp_server": "",
        "owner_email": "",
        "mcp_servers": {}
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
        "tool_permissions": config.get("tool_permissions", {})
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
        "huggingface": "HF_TOKEN"
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
            'kimi': ['moonshot/kimi-k2.5', 'moonshot/kimi-latest', 'moonshot/moonshot-v1-8k', 'moonshot/moonshot-v1-32k', 'moonshot/moonshot-v1-128k'],
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
async def get_history(session_id: int = None):
    """Retrieve chat history. Optionally filter by session_id."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if session_id:
        cursor.execute("SELECT role, content FROM messages WHERE session_id=? ORDER BY id ASC LIMIT 500", (session_id,))
    else:
        cursor.execute("SELECT role, content FROM messages ORDER BY id ASC LIMIT 500")
    rows = cursor.fetchall()
    conn.close()
    return {"history": [{"role": row["role"], "content": row["content"]} for row in rows]}


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
               "t.next_run_at, t.resume_count")
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
            "resume_count": row["resume_count"] if "resume_count" in row.keys() else 0
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
    
    cursor.execute("SELECT * FROM task_steps WHERE task_id = ? ORDER BY step_number ASC", (task_id,))
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
            "session_name": task_row["session_name"] if "session_name" in task_row.keys() else None
        }
    }

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
            fg_agent = _active_agents.get(sid)
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
    # Interrupt running agent/process if active
    bg_agent = _background_agents.pop(str(task_id), None)
    if bg_agent:
        try: bg_agent.set_interrupt_flag()
        except Exception: pass
    _background_process_info.pop(str(task_id), None)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM task_steps WHERE task_id = ?", (task_id,))
    cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
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
                    "WHERE task_id=? ORDER BY step_number",
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
    
    async def run_agent_with_progress(query: str, model: str = None, agent_profile_name: str = None, is_heartbeat: bool = False, images: list = None, resume_task_id: int = None):
        """Run agent in a thread and push progress to WebSocket via a Queue.

        If resume_task_id is set, steps are appended to the existing task instead of creating a new one.
        """
        nonlocal session_history, last_query, agent_is_running, receive_task, ws_alive, ws_session_id
        if not is_heartbeat:
            last_query = query

        if agent_is_running:
            return "BUSY"

        agent_is_running = True
        # Pre-resolve task_id BEFORE agent execution so tools always get a valid _task_id.
        # resume_task_id is used when explicitly resuming; otherwise detect new vs continuation.
        if resume_task_id:
            ws_task_id = resume_task_id
        elif not is_heartbeat:
            ws_task_id = _resolve_task_for_query(ws_session_id, query)
        else:
            ws_task_id = None
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

            def progress_callback(event: dict):
                nonlocal has_taken_action, ws_task_id
                """Thread-safe: push progress events from thread pool into queue."""
                if is_heartbeat:
                    if event.get("event") == "tool_start":
                        has_taken_action = True
                    if not has_taken_action and event.get("event") in ["thinking", "model_switched"]:
                        return

                # Record task steps (offset on resume to continue numbering)
                adjusted_step = event.get("step", 0) + step_offset

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
                                        agent_ref = _active_agents.get(ws_session_id)
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
                                        agent_ref = _active_agents.get(ws_session_id)
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
                        await _safe_send({
                            "type": "status",
                            "message": "llama-server 启动失败，请检查模型文件"
                        })
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
            _active_agents[ws_session_id] = agent
            
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
                lambda: agent.run_turn(query, False, progress_callback, images=images, task_id=ws_task_id)
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
                        else:
                            # Non-blocking input: queue message to agent
                            q = user_msg.get("query", user_msg.get("text", ""))
                            if q.strip():
                                a = _active_agents.get(ws_session_id)
                                if a:
                                    a.queue_message(q)
                                    save_message("user", q, ws_session_id)
                                    print(f"[WS] Queued message to agent session {ws_session_id}")
                        receive_task = None
                    except WebSocketDisconnect:
                        ws_alive = False
                        # Immediately remove from broadcast list
                        if websocket in connected_websockets:
                            connected_websockets.remove(websocket)
                        _active_agents.pop(ws_session_id, None)
                        # Don't interrupt — let agent finish in background
                        # Reconnecting clients will replay completed steps
                        receive_task = None
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
                    update_task_status(ws_task_id, "completed", summary)
                
                # Update total tokens in tasks table from stats
                try:
                    stats = get_stats_manager().get_task_usage(ws_task_id)
                    if stats:
                        conn_tmp = sqlite3.connect(DB_PATH)
                        conn_tmp.execute("UPDATE tasks SET total_tokens = ?, total_cost = ? WHERE id = ?", (stats["total"], stats.get("cost", 0.0), ws_task_id))
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
            config = load_config()
            heartbeat_enabled = config.get("heartbeat_enabled", False)
            heartbeat_interval = config.get("heartbeat_interval", 60)
            
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
                        raise asyncio.TimeoutError()
                
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
                                "result_preview, full_result, success FROM task_steps "
                                "WHERE task_id=? ORDER BY step_number", (task_id,)).fetchall()
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
                            # Build step summary so agent knows what was already done
                            step_summary_lines = []
                            for s in steps[-30:]:
                                label = s['tool_label'] or s['tool_name']
                                preview = (s['result_preview'] or '')[:120]
                                step_summary_lines.append(
                                    f"步骤{s['step_number']}: {label} "
                                    f"({'✓' if s['success'] else '✗'}) "
                                    f"{preview}"
                                )
                            step_summary = "\n".join(step_summary_lines) if step_summary_lines else ""

                            # Extract key findings from full_result for resume context
                            key_findings = []
                            seen_urls = set()
                            for s in steps:
                                fr = s['full_result'] or ''
                                if not fr:
                                    continue
                                step_urls = re.findall(r'(?:https?|ftp)://[^\s\'"<>]{5,}', fr)
                                for u in step_urls:
                                    if u not in seen_urls:
                                        seen_urls.add(u)
                                        key_findings.append(f"📎 URL: {u}")
                                # Detect extracted data patterns
                                if s['tool_name'] == 'execute_shell' and s['success']:
                                    if re.search(r'(?:m3u8|mp4|\.ts)', fr, re.IGNORECASE):
                                        short = fr[:500].replace('\n', ' ').replace('\r', '')[:120]
                                        key_findings.append(f"📄 命令产出: {short}")

                            findings_block = ""
                            if key_findings:
                                findings_block = (
                                    "\n--- 之前的关键发现 ---\n"
                                    + "\n".join(key_findings[:15])
                                    + "\n---\n"
                                )

                            query = (
                                f"【原始任务目标】{original_goal}\n\n"
                                "你需要继续执行这个任务。以下是之前已完成的执行步骤摘要：\n"
                                "--- 已完成步骤 ---\n"
                                f"{step_summary}\n"
                                f"{findings_block}"
                                "请根据以上原始目标和已完成步骤，从上次中断的地方继续执行。"
                                "不要重复读取已经成功获得的文件内容，直接使用已有的结果继续下一步。"
                            )
                        except Exception as e:
                            print(f"[WS] Resume error: {e}")
                            query = "继续执行未完成的任务。"
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

                is_heartbeat = False
            except asyncio.TimeoutError:
                if not heartbeat_enabled or agent_is_running:
                    continue
                # Trigger Heartbeat
                query = "【系统指令】后台巡视时间已到。请检查系统状态、后台任务或之前的计划是否需要继续。如果一切正常无需操作，请且仅回复 'HEARTBEAT_OK'。"
                retry_model = None
                agent_profile_name = None
                ws_images = None
                is_heartbeat = True

            if not is_heartbeat:
                # Send immediate acknowledgment
                await _safe_send({
                    "type": "status",
                    "message": "Agent is thinking...",
                    "session_id": ws_session_id
                })
            
            try:
                response = await run_agent_with_progress(query, retry_model, agent_profile_name, is_heartbeat=is_heartbeat, images=ws_images, resume_task_id=resume_id_for_run)
                
                if response == "BUSY":
                    continue
                    
                if is_heartbeat and response and response.strip() == "HEARTBEAT_OK":
                    # Silent heartbeat, do nothing
                    continue
                
                # If it's a heartbeat response that isn't HEARTBEAT_OK, it's a resume or proactive thought.
                # Ensure it's tagged with the correct session.
                save_message("agent", response, ws_session_id)


                # Send the final response
                await _safe_send({
                    "type": "message",
                    "role": "agent",
                    "content": response,
                    "session_id": ws_session_id
                })
                
            except Exception as e:
                err_str = str(e).lower()
                error_msg = f"Agent Encountered Error: {str(e)}"
                if "api_key" in err_str or "authentication" in err_str or "not found" in err_str or "key" in err_str:
                    error_msg += "\n\n---\n**💡 提示：您似乎尚未配置此模型的 API Key！**\n\n以 Kimi 为例，请前往 [Moonshot 开放平台](https://platform.moonshot.cn/console/api-keys) 免费申请一个 API Key，然后在左侧边栏的「设置 - 模型配置」中填入并保存即可开始对话！"
                
                save_message("system", error_msg, ws_session_id)
                await _safe_send({
                    "type": "error",
                    "content": error_msg,
                    "original_query": query if not is_heartbeat else "",
                    "session_id": ws_session_id
                })
                
    except WebSocketDisconnect:
        print("Client disconnected")
        if websocket in connected_websockets:
            connected_websockets.remove(websocket)
        _active_agents.pop(ws_session_id, None)
        _session_enabled_tools.pop(ws_session_id, None)
    except Exception as e:
        print(f"WebSocket error: {e}")
        if websocket in connected_websockets:
            connected_websockets.remove(websocket)
        _active_agents.pop(ws_session_id, None)
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
    for ws in list(connected_websockets):
        try:
            asyncio.run_coroutine_threadsafe(_ws_send_safe(ws, message), loop)
        except Exception:
            dead.append(ws)
    for ws in dead:
        try: connected_websockets.remove(ws)
        except ValueError: pass


def _broadcast_task_history(task_id: int, session_id: int):
    """Fetch task steps and broadcast as history_steps for UI rendering."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT step_number, tool_name, tool_label, args_preview, result_preview, full_result, success "
            "FROM task_steps WHERE task_id=? ORDER BY step_number ASC", (task_id,)).fetchall()
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
                "success": bool(r["success"]),
            })
        _broadcast_to_websockets({
            "type": "history_steps",
            "task_id": task_id,
            "session_id": session_id,
            "steps": steps,
            "task_status": "interrupted",
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
    agent = OpenAGCAgent(model=model)

    # Look up session_id so progress events are routed to the correct session
    bg_session_id = 1
    try:
        bg_conn = sqlite3.connect(DB_PATH)
        row = bg_conn.execute("SELECT session_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row and row[0]:
            bg_session_id = row[0]
        bg_conn.close()
    except Exception:
        pass

    step_counter = 0

    def progress_cb(event: dict):
        nonlocal step_counter
        if event.get("event") == "tool_start":
            step_counter += 1
            try:
                add_task_step(
                    task_id=task_id,
                    step_number=event.get("step", step_counter),
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
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE task_steps SET result_preview=?, full_result=?, success=? WHERE task_id=? AND step_number=?",
                    (event.get("result_preview", ""), event.get("full_result", event.get("result_preview", "")),
                     1 if event.get("success") else 0, task_id, event.get("step", step_counter))
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
    if is_resume:
        query = (f"【系统指令 - 自动恢复】你之前因为执行步骤过多被系统自动中断了。"
                 f"请根据之前的上下文继续完成未完成的任务。"
                 f"原始任务: {user_query}")

        # Broadcast reconstructed history steps so they appear in the session
        _broadcast_task_history(task_id, bg_session_id)

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

    # Notify connected clients
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
        response = agent.run_turn(query, False, progress_cb, task_id=task_id)

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
            save_task_context(task_id, agent.messages[1:])
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
            save_task_context(task_id, agent.messages[1:])
            update_task_status(task_id, "interrupted", summary, interruption_reason="max_iterations")
        else:
            update_task_status(task_id, "completed", summary)
            save_task_context(task_id, [])  # Clear context on success
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

        # Push final result to clients
        _broadcast_to_websockets({
            "type": "message",
            "role": "agent",
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
    """Background thread that handles scheduled tasks and long-run auto-resume."""
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


                # 2. Auto-resume longrun tasks interrupted by faults (exclude max_iterations)
                cursor.execute(
                    "SELECT * FROM tasks WHERE task_type='longrun' AND status='interrupted' "
                    "AND interruption_reason != 'max_iterations' AND interruption_reason != 'user' "
                    "AND resume_count < max_resume_count"
                )
                resume_tasks = cursor.fetchall()

                for task in resume_tasks:
                    task_id = task["id"]
                    print(f"[TaskScheduler] Auto-resuming longrun task #{task_id}: {task['title']}")

                    increment_task_resume(task_id)
                    ctx = get_task_context(task_id)

                    threading.Thread(
                        target=_run_background_task,
                        args=(task_id, task["user_query"], ctx, True),
                        daemon=True
                    ).start()

                conn.close()
            except Exception as e:
                print(f"[TaskScheduler] Error: {e}")
            
            _time.sleep(30)  # Check every 30 seconds
    
    threading.Thread(target=scheduler_loop, daemon=True).start()

def start_background_monitor():
    """Monitor backgrounded tasks — check download/process completion and auto-resume."""
    def monitor_loop():
        import time as _t
        import os as _os
        while True:
            try:
                conn = sqlite3.connect(DB_PATH)
                conn.row_factory = sqlite3.Row
                bg_tasks = conn.execute(
                    "SELECT id, user_query, resume_count, max_resume_count, created_at FROM tasks "
                    "WHERE status='backgrounded' AND resume_count < max_resume_count"
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

                        # Fallback 2: check for stale backgrounded tasks (> 30 min, no process track)
                        if not pinfo:
                            try:
                                created_str = task["created_at"]
                                if created_str:
                                    from datetime import datetime, timezone, timedelta
                                    created_dt = datetime.strptime(created_str, '%Y-%m-%d %H:%M:%S')
                                    created_dt = created_dt.replace(tzinfo=timezone.utc)
                                    now = datetime.now(timezone.utc)
                                    age = now - created_dt
                                    if age > timedelta(minutes=30):
                                        print(f"[BgMonitor] Task {tid}: no process info for {age.total_seconds()/60:.0f}min — marking failed")
                                        update_task_status(tid, "background_failed",
                                            "后台进程信息丢失（可能因服务重启），无法恢复",
                                            interruption_reason="process_lost")
                                        ctx = get_task_context(tid)
                                        if ctx:
                                            ctx.append({"role": "user", "content": (
                                                "【系统通知】后台命令的进程信息已丢失（可能因服务重启）。"
                                                "请重新开始任务，或检查是否有残留进程需要手动处理。"
                                            )})
                                            save_task_context(tid, ctx)
                                        continue
                            except Exception as ts_err:
                                print(f"[BgMonitor] Task {tid}: time-check error: {ts_err}")

                        if pinfo:
                            pid = pinfo.get("pid")
                            out_file = pinfo.get("output_file", "")
                            command = pinfo.get("command", "")
                            try:
                                _os.kill(pid, 0)  # No signal, just check existence
                                # Process still running — skip
                            except OSError:
                                # Process has terminated — directly resume task
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
                                # Directly resume instead of waiting for scheduler
                                increment_task_resume(tid)
                                task_row = conn.execute(
                                    "SELECT user_query FROM tasks WHERE id=?", (tid,)
                                ).fetchone()
                                user_query = task_row["user_query"] if task_row else ""
                                print(f"[BgMonitor] Task {tid}: shell process {pid} done — resuming directly")
                                # Update status inside the thread to avoid race with scheduler
                                def _do_resume_with_status():
                                    update_task_status(tid, "interrupted",
                                        "后台命令完成", interruption_reason="background_complete")
                                    _run_background_task(tid, user_query, ctx, True)
                                threading.Thread(
                                    target=_do_resume_with_status,
                                    daemon=True
                                ).start()
                    except Exception as e:
                        print(f"[BgMonitor] Shell process check error: {e}")

                conn.close()
            except Exception as e:
                print(f"[BgMonitor] Error: {e}")
            _t.sleep(10)
    threading.Thread(target=monitor_loop, daemon=True).start()

# Start background listeners
start_background_monitor()
start_email_listener()
start_task_scheduler()
