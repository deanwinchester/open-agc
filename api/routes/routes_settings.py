"""Settings, Llamacpp, Downloads, and AI Designer API endpoints."""
import os, sys, json, re, sqlite3, threading, asyncio, time
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from dotenv import load_dotenv, set_key

from api.db import DB_PATH
from api.config import load_config, save_config, CONFIG_PATH
from api.state import connected_websockets, _llamacpp_download_state, _broadcast_to_websockets, _active_agents, _sandbox_waits
from api.task_core import create_task, update_task_status, get_task_context, save_task_context, add_task_step
from core.paths import get_data_path
from core.llamacpp_manager import get_llamacpp_manager
from core.stats_manager import get_stats_manager

router = APIRouter()


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
    session_id: Optional[int] = None
    tool_permissions: Optional[Dict[str, Any]] = None
    searxng_url: str = ""
    searxng_port: int = 8888
    max_correction_attempts: int = 5
    cold_cache_ttl: int = 3600
    max_resume_count: int = 10
    max_total_tokens: int = 128000



@router.get("/api/settings")

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

        "cold_cache_ttl": config.get("cold_cache_ttl", 3600),

        "max_resume_count": config.get("max_resume_count", 10),

        "context_budget": config.get("context_budget", {"max_total_tokens": 128000}),

    }



@router.post("/api/settings")

async def update_settings(config_update: ConfigUpdate):

    """Update JSON config and set env vars dynamically."""
    config = load_config()
    env_file = get_data_path('.env')
    load_dotenv(env_file)
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

        config["cold_cache_ttl"] = config_update.cold_cache_ttl

        config["max_resume_count"] = config_update.max_resume_count

        _budget = config.get("context_budget", {})

        if not isinstance(_budget, dict):

            _budget = {}

        _budget["max_total_tokens"] = config_update.max_total_tokens

        config["context_budget"] = _budget

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

@router.get("/api/provider-models")

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



@router.get("/api/stats/token_usage")

async def get_token_usage_stats(provider: str, days: int = 30):

    """Get historical token usage stats for a specific provider."""

    from core.stats_manager import get_stats_manager

    manager = get_stats_manager(DB_PATH)

    history = manager.get_usage_history(provider, days)

    return {"status": "success", "data": history}

# Route block removed — imported from api.routes

@router.get("/api/llamacpp/status")

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



@router.post("/api/llamacpp/setup")

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



@router.post("/api/llamacpp/download-model")

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



@router.post("/api/llamacpp/search-models")

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



@router.post("/api/llamacpp/model-files")

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



@router.post("/api/llamacpp/download-from-hf")

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



@router.get("/api/downloads")

async def get_downloads(status: str = None):

    """List all download records with optional status filter."""

    records = list_download_records(status_filter=status)

    return {"downloads": records}





@router.get("/api/downloads/{download_id}/events")

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





@router.post("/api/downloads/{download_id}/resume")

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





@router.delete("/api/downloads/{download_id}")

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

class LlamaControlRequest(BaseModel):
    action: str
    model: Optional[str] = None


@router.post("/api/llamacpp/control")

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



# Skills, Memories, History API routes imported from api.routes.routes_skills and routes_memories





# Sessions, Agents, Models API routes imported from api.routes.routes_sessions



# /api/models/available imported from api.routes.routes_sessions





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



@router.post("/api/agent-design")

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

# Tasks and Processes API routes imported from api.routes.routes_tasks
