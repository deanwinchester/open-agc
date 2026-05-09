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
from core.llm_client import load_config
from core.plugin_manager import discover_plugins, list_plugins, get_plugin

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

# ── Serve static frontend ──
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_static_dir = os.path.join(_BASE_DIR, "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

@app.get("/")
async def root():
    from fastapi.responses import FileResponse
    index_path = os.path.join(_static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"detail": "index.html not found"}

# Store the main event loop for cross-thread WebSocket broadcasts
_main_event_loop: asyncio.AbstractEventLoop = None

@app.on_event("startup")
async def _capture_event_loop():
    global _main_event_loop
    _main_event_loop = asyncio.get_event_loop()

# Initialize Database
DB_PATH = get_data_path("chat_history.db")

# ── Plugin Discovery ──
_plugins_dir = os.path.abspath(os.path.join(os.path.dirname(DB_PATH), "..", "plugins"))
_plugins = discover_plugins(
    plugins_dir=_plugins_dir,
    broadcast_fn=None,  # set later after WebSocket init
    server_config=load_config(),
)

def _mount_plugins(app, plugins):
    """Mount plugin routers and static files onto the FastAPI app."""
    for p in plugins:
        inst = p.instance
        if inst.router:
            app.include_router(inst.router, prefix=f"/api/plugin/{p.name}")
            print(f"[Server] Mounted plugin router: {p.name}")
        if inst.static_dir and os.path.isdir(inst.static_dir):
            from fastapi.staticfiles import StaticFiles
            app.mount(f"/static/plugins/{p.name}", StaticFiles(directory=inst.static_dir), name=f"plugin_{p.name}_static")
            print(f"[Server] Mounted plugin static: {p.name}")

_mount_plugins(app, _plugins)
print(f"[Server] Loaded {len(_plugins)} plugin(s)")

@app.get("/api/plugins")
async def get_plugins():
    """List all plugins (loaded + disk) with state info."""
    from core.plugin_manager import list_all_plugins
    return {
        "plugins": list_all_plugins(_plugins_dir),
        "plugins_dir": os.path.abspath(_plugins_dir)
    }


@app.post("/api/plugins/scan")
async def scan_plugins():
    """Re-scan plugins directory for new plugins."""
    global _plugins
    from core.plugin_manager import discover_plugins
    _plugins = discover_plugins(
        plugins_dir=_plugins_dir,
        broadcast_fn=_broadcast_to_websockets,
        server_config=load_config(),
    )
    _mount_plugins(app, _plugins)
    return {"status": "ok", "count": len(_plugins), "plugins": list_plugins()}


@app.post("/api/plugins/{name}/toggle")
async def toggle_plugin(name: str):
    """Enable or disable a plugin."""
    from core.plugin_manager import toggle_plugin
    new_state = toggle_plugin(name, _plugins_dir)
    return {"status": "ok", "enabled": new_state.get("enabled", True)}


@app.post("/api/plugins/install")
async def install_plugin(req: Request):
    """Install a plugin from Git URL."""
    import json as _json
    body = await req.json()
    name = body.get("name", "")
    url = body.get("url", "")
    if not name or not url:
        raise HTTPException(status_code=400, detail="name and url required")
    from core.plugin_manager import install_from_git
    ok = install_from_git(name, url, _plugins_dir)
    if not ok:
        raise HTTPException(status_code=500, detail="Install failed — check server logs")
    return {"status": "ok", "message": f"Plugin {name} installed"}


@app.get("/api/marketplace")
async def get_marketplace():
    """Fetch the remote plugin marketplace index."""
    from core.plugin_manager import fetch_marketplace
    data = fetch_marketplace()
    return {"marketplace": data}


@app.delete("/api/plugins/{name}")
async def delete_plugin(name: str):
    """Delete a plugin directory (uninstall)."""
    import shutil
    plugin_dir = os.path.join(_plugins_dir, name)
    if not os.path.isdir(plugin_dir):
        raise HTTPException(status_code=404, detail=f"Plugin not found: {name}")
    try:
        from core.plugin_manager import unload_plugin
        unload_plugin(name)
        shutil.rmtree(plugin_dir)
        return {"status": "ok", "message": f"Plugin {name} deleted"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
start_task_scheduler()
