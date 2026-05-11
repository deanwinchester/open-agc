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
from typing import List, Dict, Optional
from dotenv import load_dotenv, set_key

from core.paths import get_data_path, get_skills_dir
from core.llamacpp_manager import get_llamacpp_manager
from core.plugin_manager import discover_plugins, list_plugins, list_all_plugins, unload_plugin, toggle_plugin, install_from_git, fetch_marketplace

# ── Route modules ──
from api.routes.benchmark import router as benchmark_router, init_benchmark_routes
from api.routes.downloads import router as downloads_router, init_download_routes

# Load environment variables
env_file = get_data_path(".env")
load_dotenv(env_file)

from agent.agent import OpenAGCAgent
import litellm
# Fix for PyInstaller bundling issue with tiktoken
litellm.num_tokens_logging = False 
litellm.supports_token_counter = False
litellm._turn_on_debug()
litellm.set_verbose = True  # Double down on verbosity for terminal logs

# Ensure local connections bypass proxy (important for Ollama on Windows)
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

# Store the main event loop for cross-thread WebSocket broadcasts
_main_event_loop: asyncio.AbstractEventLoop = None

@app.on_event("startup")
async def _capture_event_loop():
    global _main_event_loop
    _main_event_loop = asyncio.get_event_loop()

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
        cursor.execute("ALTER TABLE tasks ADD COLUMN interruption_reason TEXT")
    except Exception:
        pass
    conn.commit()
    conn.close()

init_db()

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

# Task helper functions
def create_task(title: str, user_query: str, task_type: str = 'oneshot',
                schedule_cron: str = None, schedule_enabled: bool = False) -> int:
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
        "INSERT INTO tasks (title, user_query, task_type, schedule_cron, schedule_enabled, next_run_at) VALUES (?, ?, ?, ?, ?, ?)",
        (title, user_query, task_type, schedule_cron, 1 if schedule_enabled else 0, next_run)
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
    """Load saved conversation context for a task."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT context_snapshot FROM tasks WHERE id=?", (task_id,))
    row = cursor.fetchone()
    conn.close()
    if row and row[0]:
        try:
            return json.loads(row[0])
        except Exception:
            pass
    return []

def increment_task_resume(task_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE tasks SET resume_count = resume_count + 1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (task_id,))
    conn.commit()
    conn.close()

def add_task_step(task_id: int, step_number: int, tool_name: str, tool_label: str = None,
                  args_preview: str = None, result_preview: str = None, full_result: str = None,
                  success: bool = True, thinking_content: str = None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO task_steps (task_id, step_number, tool_name, tool_label, args_preview, result_preview, full_result, success, thinking_content) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, step_number, tool_name, tool_label, args_preview, result_preview, full_result, 1 if success else 0, thinking_content)
    )
    conn.commit()
    conn.close()

# ==========================================
# Download record helpers
# ==========================================

def create_download_record(type_: str, label: str, repo_id: str = None,
                           filename: str = None, source: str = "huggingface",
                           url: str = None, target_path: str = "",
                           partial_path: str = "", total_size: int = 0) -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        '''INSERT INTO downloads (type, label, repo_id, filename, source, url,
           target_path, partial_path, total_size, downloaded_bytes, status, progress)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'downloading', 0.0)''',
        (type_, label, repo_id, filename, source, url, target_path, partial_path, total_size)
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
    conn.close()


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
    "error": ""      # error message if stage == "error"
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
            "ollama": "http://localhost:11434",
            "llamacpp": "http://localhost:8080/v1",
            "sglang": "http://localhost:8009/v1",
            "vllm": "http://localhost:8000/v1",
            "huggingface": ""
        },
        "default_model": "sglang/Qwen/Qwen3.5-9B-Instruct",
        "fallback_models": ["ollama/qwen3.5:9b"],
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
        "owner_email": ""
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

@app.get("/api/settings")
async def get_settings():
    """Return current configuration."""
    config = load_config()
    
    # Mask API keys before sending to frontend
    masked_keys = {}
    for k, v in config.get("api_keys", {}).items():
        if v:
            masked_keys[k] = f"{v[:3]}...{v[-3:]}" if len(v) > 6 else "***"
        else:
            masked_keys[k] = ""
            
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
        "email_listener_enabled": config.get("email_listener_enabled", False),
        "email_account": config.get("email_account", ""),
        "email_password": ("***" if config.get("email_password") else ""),
        "email_imap_server": config.get("email_imap_server", ""),
        "email_smtp_server": config.get("email_smtp_server", ""),
        "owner_email": config.get("owner_email", "")
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
        "ollama": "OLLAMA_API_BASE",
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
    elif provider == "ollama":
        base_url = api_keys.get("ollama", "http://localhost:11434")
        if not base_url.startswith("http"):
            base_url = "http://" + base_url
        try:
            res = requests.get(f"{base_url.rstrip('/')}/api/tags", timeout=5)
            if res.status_code == 200:
                models = [f"ollama/{m['name']}" for m in res.json().get("models", [])]
        except Exception: pass
    elif provider == "sglang":
        base_url = api_keys.get("sglang", "http://localhost:8009/v1")
        if not base_url.startswith("http"):
            base_url = "http://" + base_url
        try:
            res = requests.get(f"{base_url.rstrip('/')}/models", timeout=5)
            if res.status_code == 200:
                models = [f"sglang/{m['id']}" for m in res.json().get("data", [])]
        except Exception: pass
    elif provider == "vllm":
        base_url = api_keys.get("vllm", "http://localhost:8000/v1")
        if not base_url.startswith("http"):
            base_url = "http://" + base_url
        try:
            res = requests.get(f"{base_url.rstrip('/')}/models", timeout=5)
            if res.status_code == 200:
                models = [f"vllm/{m['id']}" for m in res.json().get("data", [])]
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
            'ollama': ['ollama/qwen2.5:7b', 'ollama/llama3.1:8b', 'ollama/deepseek-r1:8b', 'ollama/llama3.3:70b'],
            'llamacpp': ['llamacpp/local-model (需先下载 GGUF 模型)'],
            'sglang': ['sglang/Qwen/Qwen3.5-9B-Instruct', 'sglang/Qwen/Qwen2.5-7B-Instruct'],
            'vllm': ['vllm/Qwen/Qwen3.5-9B-Instruct']
        }
        models = defaults.get(provider, [])
        
    models.sort()
    return {"models": models}

class PullRequest(BaseModel):
    model_name: str
    tool: str = "huggingface" # "huggingface" or "modelscope"

@app.post("/api/sglang/pull")
async def pull_model(req: PullRequest):
    """Pull local models via huggingface-cli or modelscope."""
    try:
        if req.tool == "modelscope":
            cmd = ["modelscope", "download", "--model", req.model_name]
        else:
            cmd = ["huggingface-cli", "download", req.model_name]
            
        import subprocess
        # Simply run in blocking mode or background depending on requirements
        # Here we do a blocking subprocess call, which might block the API 
        # but it's okay for a local GUI utility unless the user wants background downloading.
        # Alternatively, returning immediately and logging to a file.
        subprocess.Popen(cmd)
        return {"status": "success", "message": f"Downloading {req.model_name} in background..."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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
        "error": ""
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
                _llamacpp_download_state["progress"] = ratio
                update_download_progress(dl_id, ratio)
                _broadcast_to_websockets({
                    "type": "llamacpp_download",
                    "task": "binary",
                    "label": "正在下载 llama.cpp 二进制文件...",
                    "progress": ratio,
                    "stage": "downloading"
                })

            manager2 = get_llamacpp_manager()
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
    manager = get_llamacpp_manager()
    success = manager.download_model(req.url, req.filename)
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
    manager = get_llamacpp_manager()
    if req.source == "modelscope":
        results = manager.search_ms_models(req.query)
    else:
        results = manager.search_hf_models(req.query)
    return {"status": "success", "models": results}

class ModelFilesRequest(BaseModel):
    repo_id: str
    source: str = "huggingface"

@app.post("/api/llamacpp/model-files")
async def get_llamacpp_model_files(req: ModelFilesRequest):
    """List GGUF files in a model repository (HF or ModelScope)."""
    manager = get_llamacpp_manager()
    if req.source == "modelscope":
        files = manager.get_ms_model_files(req.repo_id)
    else:
        files = manager.get_hf_model_files(req.repo_id)
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
        "download_id": db_download_id
    }

    def run_download():
        global _llamacpp_download_state
        dl_id = db_download_id
        try:
            def progress_cb(ratio):
                _llamacpp_download_state["progress"] = ratio
                update_download_progress(dl_id, ratio)
                _broadcast_to_websockets({
                    "type": "llamacpp_download",
                    "task": "model",
                    "label": f"正在下载 {short_name}...",
                    "progress": ratio,
                    "stage": "downloading"
                })

            manager2 = get_llamacpp_manager()
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
        "download_id": download_id
    }

    update_download_progress(download_id, record["progress"] or 0.0,
                             downloaded_bytes=resume_offset,
                             status="downloading", error_message="")

    def run_resume():
        global _llamacpp_download_state
        dl_id = download_id
        try:
            def progress_cb(ratio):
                _llamacpp_download_state["progress"] = ratio
                update_download_progress(dl_id, ratio)
                _broadcast_to_websockets({
                    "type": "llamacpp_download",
                    "task": record["type"],
                    "label": f"续传 {short_name}...",
                    "progress": ratio,
                    "stage": "downloading"
                })

            manager = get_llamacpp_manager()
            if record["type"] == "binary":
                success = manager.download_binary(progress_callback=progress_cb)
            elif record["source"] == "modelscope":
                success = manager.download_model_from_ms(
                    record["repo_id"], record["filename"], progress_callback=progress_cb
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
        cursor.execute("SELECT role, content FROM messages WHERE session_id=? ORDER BY id ASC", (session_id,))
    else:
        cursor.execute("SELECT role, content FROM messages ORDER BY id ASC")
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
    return {"sessions": [dict(r) for r in rows]}

@app.post("/api/sessions")
async def create_session(body: dict = {}):
    """Create a new session."""
    name = body.get("name", None)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    if not name:
        # Auto-name: "会话 N"
        cursor.execute("SELECT COUNT(*) FROM sessions")
        count = cursor.fetchone()[0] + 1
        name = f"会话 {count}"
    cursor.execute("INSERT INTO sessions (name) VALUES (?)", (name,))
    session_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return {"session": {"id": session_id, "name": name}}

@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: int):
    """Delete a session and its messages."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
    cursor.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    conn.commit()
    conn.close()
    return {"ok": True}

@app.put("/api/sessions/{session_id}")
async def rename_session(session_id: int, body: dict = {}):
    """Rename a session."""
    name = body.get("name", "会话")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE sessions SET name=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (name, session_id))
    conn.commit()
    conn.close()
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
async def get_tasks(status: str = None, q: str = None):
    """List tasks with optional status filter and search."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    query = "SELECT t.*, (SELECT COUNT(*) FROM task_steps WHERE task_id = t.id) as step_count FROM tasks t"
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
    
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY t.created_at DESC LIMIT 100"
    
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    
    tasks = []
    for row in rows:
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
            "schedule_cron": row["schedule_cron"] if "schedule_cron" in row.keys() else None,
            "schedule_enabled": bool(row["schedule_enabled"]) if "schedule_enabled" in row.keys() else False,
            "next_run_at": row["next_run_at"] if "next_run_at" in row.keys() else None,
            "resume_count": row["resume_count"] if "resume_count" in row.keys() else 0
        })
    return {"tasks": tasks}

@app.get("/api/tasks/{task_id}")
async def get_task_detail(task_id: int):
    """Get task detail with all steps."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
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
            "interruption_reason": task_row["interruption_reason"] if "interruption_reason" in task_row.keys() else None
        }
    }

@app.post("/api/tasks/{task_id}/interrupt")
async def interrupt_task(task_id: int):
    """Mark a task as interrupted by user."""
    update_task_status(task_id, "interrupted", interruption_reason="user")
    return {"status": "success", "message": "Task marked as interrupted"}

@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int):
    """Delete a task and its steps."""
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
    
    async def run_agent_with_progress(query: str, model: str = None, agent_profile_name: str = None, is_heartbeat: bool = False, images: list = None):
        """Run agent in a thread and push progress to WebSocket via a Queue."""
        nonlocal session_history, last_query, agent_is_running, receive_task, ws_alive, ws_session_id
        if not is_heartbeat:
            last_query = query
            
        if agent_is_running:
            return "BUSY"
            
        agent_is_running = True
        ws_task_id = None  # Track task_id for this run
        task_has_tools = False  # Only create task if tools are called
        
        try:
            import queue as thread_queue
            progress_queue = thread_queue.Queue()
            has_taken_action = False

            def progress_callback(event: dict):
                nonlocal has_taken_action, ws_task_id, task_has_tools
                """Thread-safe: push progress events from thread pool into queue."""
                if is_heartbeat:
                    if event.get("event") == "tool_start":
                        has_taken_action = True
                    if not has_taken_action and event.get("event") in ["thinking", "model_switched"]:
                        return

                # Auto-create task on first tool_start
                if event.get("event") == "tool_start" and not task_has_tools and not is_heartbeat:
                    task_has_tools = True
                    try:
                        title = query[:60] + ('...' if len(query) > 60 else '')
                        ws_task_id = create_task(title, query)
                    except Exception as e:
                        print(f"[Task] Failed to create task: {e}")

                # Record task steps
                if ws_task_id and event.get("event") == "tool_start":
                    try:
                        add_task_step(
                            task_id=ws_task_id,
                            step_number=event.get("step", 0),
                            tool_name=event.get("tool", ""),
                            tool_label=event.get("tool_label", ""),
                            args_preview=event.get("args_preview", "")
                        )
                    except Exception as e:
                        print(f"[Task] Failed to add step: {e}")

                if ws_task_id and event.get("event") == "tool_done":
                    try:
                        # Update the step with result
                        conn = sqlite3.connect(DB_PATH)
                        cursor = conn.cursor()
                        cursor.execute(
                            "UPDATE task_steps SET result_preview=?, full_result=?, success=? WHERE task_id=? AND step_number=?",
                            (event.get("result_preview", ""), event.get("result_preview", ""),
                             1 if event.get("success") else 0, ws_task_id, event.get("step", 0))
                        )
                        conn.commit()
                        conn.close()
                    except Exception as e:
                        print(f"[Task] Failed to update step: {e}")

                # Attach task_id to the event so frontend can track it
                if ws_task_id:
                    event["task_id"] = ws_task_id

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

            agent = OpenAGCAgent(model=current_model)
            
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
                lambda: agent.run_turn(query, False, progress_callback, images=images)
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
                            if ws_task_id:
                                update_task_status(ws_task_id, "interrupted", interruption_reason="user")
                        receive_task = None
                    except WebSocketDisconnect:
                        ws_alive = False
                        agent.is_interrupted = True
                        receive_task = None
                    except Exception:
                        receive_task = None

                # Drain the thread-safe queue (no cross-thread race)
                while True:
                    try:
                        event = progress_queue.get_nowait()
                        await _safe_send({
                            "type": "progress",
                            **event
                        })
                    except thread_queue.Empty:
                        break

            while not progress_queue.empty():
                try:
                    event = progress_queue.get_nowait()
                    await _safe_send({
                        "type": "progress",
                        **event
                    })
                except Exception:
                    break
            
            response = await agent_future
            session_history = agent.messages[1:]
            
            # Detect max_iterations hit for longrun auto-resume
            is_max_iter = response and response.startswith("[MAX_ITERATIONS_REACHED]")
            
            if ws_task_id:
                summary = response[:200] if response else ""
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
                else:
                    update_task_status(ws_task_id, "completed", summary)
            
            return response
        except Exception as e:
            if ws_task_id:
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
                
                if msg_type == "retry":
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
                    "message": "Agent is thinking..."
                })
            
            try:
                response = await run_agent_with_progress(query, retry_model, agent_profile_name, is_heartbeat=is_heartbeat, images=ws_images)
                
                if response == "BUSY":
                    continue
                    
                if is_heartbeat and response and response.strip() == "HEARTBEAT_OK":
                    # Silent heartbeat, do nothing
                    continue
                    
                # Save agent response to DB
                save_message("agent", response, ws_session_id)

                # Send the final response
                await _safe_send({
                    "type": "message",
                    "role": "agent",
                    "content": response
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
                    "original_query": query if not is_heartbeat else ""
                })
                
    except WebSocketDisconnect:
        print("Client disconnected")
        if websocket in connected_websockets:
            connected_websockets.remove(websocket)
    except Exception as e:
        print(f"WebSocket error: {e}")
        if websocket in connected_websockets:
            connected_websockets.remove(websocket)

def start_email_listener():
    def email_listener_loop():
        from core.email_service import fetch_emails, send_email
        from agent.agent import OpenAGCAgent
        while True:
            try:
                config = load_config()
                if config.get("email_listener_enabled") and config.get("email_account") and config.get("email_password") and config.get("email_imap_server"):
                    owner = config.get("owner_email", "")
                    if owner:
                        criteria = f'UNSEEN FROM "{owner}"'
                        emails = fetch_emails(
                            config["email_imap_server"],
                            config["email_account"],
                            config["email_password"],
                            criteria=criteria,
                            limit=5,
                            mark_seen=True
                        )
                        for e in emails:
                            print(f"[Email Listener] Found new command from owner: {e['subject']}")
                            save_message("system", f"🔔 已收到来自主人 ({owner}) 的新邮件指令:\n主题: {e['subject']}")
                            
                            agent = OpenAGCAgent(model=config.get("default_model", "gpt-4o"))
                            prompt = f"I received a new email instruction from my owner ({owner}).\nSubject: {e['subject']}\nBody: {e['body']}\nPlease execute this instruction, and then I will automatically email them the result."
                            
                            try:
                                response = agent.run_turn(prompt)
                            except Exception as ex:
                                response = f"Failed to execute instructions: {ex}"
                                
                            success = send_email(
                                config["email_smtp_server"],
                                config["email_account"],
                                config["email_password"],
                                owner,
                                f"Re: {e['subject']} - Task Completed",
                                f"Task Summary:\n\n{response}"
                            )
                            if success:
                                save_message("system", f"📧 已将执行结果回传至主人邮箱: {owner}")
                            else:
                                save_message("system", f"⚠️ 邮件回复发送失败，请检查 SMTP 配置。")
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
            pass


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
                    args_preview=event.get("args_preview", "")
                )
            except Exception:
                pass
        elif event.get("event") == "tool_done":
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE task_steps SET result_preview=?, full_result=?, success=? WHERE task_id=? AND step_number=?",
                    (event.get("result_preview", ""), event.get("result_preview", ""),
                     1 if event.get("success") else 0, task_id, event.get("step", step_counter))
                )
                conn.commit()
                conn.close()
            except Exception:
                pass
        
        # Push progress to connected clients
        event["task_id"] = task_id
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
    
    update_task_status(task_id, "running")
    
    # Notify connected clients
    _broadcast_to_websockets({
        "type": "message",
        "role": "system",
        "content": f"{'🔄 自动恢复' if is_resume else '⏰ 定时执行'}任务: {user_query[:60]}..."
    })
    
    try:
        response = agent.run_turn(query, False, progress_cb)
        is_max_iter = response and response.startswith("[MAX_ITERATIONS_REACHED]")
        
        summary = response[:200] if response else ""
        if is_max_iter:
            save_task_context(task_id, agent.messages[1:])
            update_task_status(task_id, "interrupted", summary, interruption_reason="max_iterations")
        else:
            update_task_status(task_id, "completed", summary)
            save_task_context(task_id, [])  # Clear context on success
        
        # Push final result to clients
        _broadcast_to_websockets({
            "type": "message",
            "role": "agent",
            "content": f"**{'🔄 自动恢复' if is_resume else '⏰ 定时'}任务完成**: {user_query[:40]}...\n\n{response[:500]}"
        })
        
        return response
    except Exception as e:
        update_task_status(task_id, "failed", str(e)[:200], interruption_reason="error")
        _broadcast_to_websockets({
            "type": "error",
            "content": f"后台任务失败: {str(e)[:100]}"
        })
        return None

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
                
                # 2. Check long-running tasks that need auto-resume
                cursor.execute(
                    "SELECT * FROM tasks WHERE task_type='longrun' AND status='interrupted' AND interruption_reason='max_iterations' AND resume_count < max_resume_count"
                )
                resume_tasks = cursor.fetchall()
                
                for task in resume_tasks:
                    task_id = task["id"]
                    print(f"[TaskScheduler] Auto-resuming longrun task #{task_id}: {task['title']}")
                    
                    # Increment resume count
                    increment_task_resume(task_id)
                    
                    # Load saved context
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

# Start background listeners
start_email_listener()
start_task_scheduler()
