import os
import json
import asyncio
import sqlite3
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Dict, Optional
from dotenv import load_dotenv, set_key

from core.paths import get_data_path, get_skills_dir

# Load environment variables
env_file = get_data_path(".env")
load_dotenv(env_file)

from agent.agent import OpenAGCAgent
import litellm
#litellm._turn_on_debug()
#litellm.set_verbose = True  # Double down on verbosity for terminal logs

app = FastAPI(title="Open-AGC UI Server")

# Initialize Database
DB_PATH = get_data_path("chat_history.db")

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def save_message(role: str, content: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO messages (role, content) VALUES (?, ?)", (role, content))
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
        "api_keys": {},
        "default_model": "moonshot/kimi-latest",
        "fallback_models": [],
        "disabled_skills": [],
        "sandbox_mode": True,
        "sandbox_dir": os.path.abspath(os.path.join(os.getcwd(), "workspace")),
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
        "ollama": "OLLAMA_API_BASE"
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
            'ollama': ['ollama/qwen2.5:7b', 'ollama/llama3.1:8b', 'ollama/deepseek-r1:8b', 'ollama/llama3.3:70b']
        }
        models = defaults.get(provider, [])
        
    models.sort()
    return {"models": models}

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
    from core.skill_manager import SkillManager
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
async def get_history():
    """Retrieve chat history."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT role, content FROM messages ORDER BY id ASC")
    rows = cursor.fetchall()
    conn.close()
    return {"history": [{"role": row["role"], "content": row["content"]} for row in rows]}

# Initialize a global agent instance
# In a real multi-user system, this would be per-session
# We'll instantiate per connection for simplicity and state isolation in this demo

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    
    # We will maintain conversation history for this session here
    # Load recent chat history from DB instead of starting empty
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        # Load the last 20 messages for working context
        cursor.execute("SELECT role, content FROM (SELECT * FROM messages ORDER BY id DESC LIMIT 20) ORDER BY id ASC")
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
    
    async def run_agent_with_progress(query: str, model: str = None, agent_profile_name: str = None, is_heartbeat: bool = False):
        """Run agent in a thread and push progress to WebSocket via a Queue."""
        nonlocal session_history, last_query, agent_is_running, receive_task
        if not is_heartbeat:
            last_query = query
            
        if agent_is_running:
            return "BUSY"
            
        agent_is_running = True
        try:
            progress_queue = asyncio.Queue()
            has_taken_action = False
            
            def progress_callback(event: dict):
                nonlocal has_taken_action
                """Thread-safe: put progress events into the async queue."""
                if is_heartbeat:
                    if event.get("event") == "tool_start":
                        has_taken_action = True
                    # If heartbeat and no tool action taken yet, suppress UI
                    if not has_taken_action and event.get("event") in ["thinking", "model_switched"]:
                        return
                progress_queue.put_nowait(event)
            
            current_model = model or os.getenv("DEFAULT_MODEL", "moonshot/kimi-latest")
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
                            # Optionally override the model if the profile specifies one
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
                lambda: agent.run_turn(query, False, progress_callback)
            )
            
            progress_task = asyncio.create_task(progress_queue.get())
            
            # Handle agent progress and check for interruption
            while not agent_future.done():
                if receive_task is None:
                    receive_task = asyncio.create_task(websocket.receive_text())
                
                done, pending = await asyncio.wait(
                    [receive_task, progress_task],
                    timeout=0.2,
                    return_when=asyncio.FIRST_COMPLETED
                )
                
                if receive_task in done:
                    try:
                        data = receive_task.result()
                        user_msg = json.loads(data)
                        if user_msg.get("type") == "interrupt":
                            agent.is_interrupted = True
                        # Reset receive_task to None so it's recreated in next iteration
                        receive_task = None
                    except Exception:
                        receive_task = None
                        
                if progress_task in done:
                    try:
                        event = progress_task.result()
                        await websocket.send_json({
                            "type": "progress",
                            **event
                        })
                        progress_task = asyncio.create_task(progress_queue.get())
                    except Exception:
                        # Fallback to prevent infinite loop or dead task
                        progress_task = asyncio.create_task(asyncio.sleep(3600))
            
            # Note: We do NOT cancel receive_task here if it's still pending
            # as it will be reused in the outer loop.
            progress_task.cancel()
            
            while not progress_queue.empty():
                try:
                    event = progress_queue.get_nowait()
                    await websocket.send_json({
                        "type": "progress",
                        **event
                    })
                except Exception:
                    break
            
            response = await agent_future
            session_history = agent.messages[1:]
            return response
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
                    if not query.strip():
                        continue
                else:
                    query = user_msg.get("query", "")
                    retry_model = None
                    agent_profile_name = user_msg.get("agent_name", None)
                    if not query.strip():
                        continue
                        
                    # Save user message to DB
                    save_message("user", query)

                is_heartbeat = False
            except asyncio.TimeoutError:
                if not heartbeat_enabled or agent_is_running:
                    continue
                # Trigger Heartbeat
                query = "【系统指令】后台巡视时间已到。请检查系统状态、后台任务或之前的计划是否需要继续。如果一切正常无需操作，请且仅回复 'HEARTBEAT_OK'。"
                retry_model = None
                agent_profile_name = None
                is_heartbeat = True

            if not is_heartbeat:
                # Send immediate acknowledgment
                await websocket.send_json({
                    "type": "status",
                    "message": "Agent is thinking..."
                })
            
            try:
                response = await run_agent_with_progress(query, retry_model, agent_profile_name, is_heartbeat=is_heartbeat)
                
                if response == "BUSY":
                    continue
                    
                if is_heartbeat and response and response.strip() == "HEARTBEAT_OK":
                    # Silent heartbeat, do nothing
                    continue
                    
                # Save agent response to DB
                save_message("agent", response)

                # Send the final response
                await websocket.send_json({
                    "type": "message",
                    "role": "agent",
                    "content": response
                })
                
            except Exception as e:
                err_str = str(e).lower()
                error_msg = f"Agent Encountered Error: {str(e)}"
                if "api_key" in err_str or "authentication" in err_str or "not found" in err_str or "key" in err_str:
                    error_msg += "\n\n---\n**💡 提示：您似乎尚未配置此模型的 API Key！**\n\n以 Kimi 为例，请前往 [Moonshot 开放平台](https://platform.moonshot.cn/console/api-keys) 免费申请一个 API Key，然后在左侧边栏的「设置 - 模型配置」中填入并保存即可开始对话！"
                
                save_message("system", error_msg)
                await websocket.send_json({
                    "type": "error",
                    "content": error_msg,
                    "original_query": query if not is_heartbeat else ""
                })
                
    except WebSocketDisconnect:
        print("Client disconnected")
    except Exception as e:
        print(f"WebSocket error: {e}")

import threading
import time

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
            time.sleep(60)

    threading.Thread(target=email_listener_loop, daemon=True).start()

# Start background listeners
start_email_listener()
