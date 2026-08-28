"""Settings, Llamacpp, Downloads, and AI Designer API endpoints."""
import os, sys, json, re, sqlite3, threading, asyncio, time
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from dotenv import load_dotenv, set_key

from api.db import DB_PATH
from api.config import load_config, save_config, CONFIG_PATH
from api.state import connected_websockets, _llamacpp_download_state, _broadcast_to_websockets, _active_agents, _background_agents, _sandbox_waits
from api.task_core import create_task, get_task_context, save_task_context, add_task_step, claim_task_for_resume
from core.paths import get_data_path
from core.llamacpp_manager import get_llamacpp_manager
from api.background import _run_background_task
from api.ws import save_message
from core.stats_manager import get_stats_manager


# Download helper functions
def delete_download_record(download_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM downloads WHERE id=?", (download_id,))
    conn.commit()
    conn.close()


# Global state imported from api.state: connected_websockets, _sandbox_waits,
# _pending_sandbox_approvals, _apply_pending_sandbox_approvals, _active_agents,
# _background_agents, _session_enabled_tools, _guardian_resume_lock,
# _llamacpp_download_state, _main_event_loop, _broadcast_to_websockets, etc.




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



def _direct_resume_background_task(task_id: int, user_query: str, context: list,
                                     download_id: int = None):
    """Directly resume a backgrounded task (thread-safe, non-blocking).

    统一语义「认领即 running，不再降级」：
    1. download_id 给定时，先用单条原子 UPDATE 消费 background_resumed 标志
       （``... WHERE id=? AND background_resumed=0``，rowcount 判赢）——
       与 BgMonitor 下载分支互斥，输家直接放弃；
    2. 起线程前 ``claim_task_for_resume(tid, ('backgrounded',))`` CAS 认领——
       与 wake/shell/Guardian/WS 各恢复路径互斥，认领失败说明另一路径已接管。
    任一关口失败都不再拉起 worker，避免双 agent 烧 token。"""
    # 1. Atomic flag: exactly one path consumes the download row.
    if download_id is not None:
        try:
            _conn = sqlite3.connect(DB_PATH)
            _cur = _conn.execute(
                "UPDATE downloads SET background_resumed=1 WHERE id=? AND background_resumed=0",
                (download_id,))
            _flag_won = _cur.rowcount == 1
            _conn.commit()
            _conn.close()
            if not _flag_won:
                print(f"[Download] Task {task_id}: download #{download_id} already consumed by another path — skip direct resume")
                return
        except Exception as _fe:
            # 标志异常不阻断：下方 CAS 仍是权威关口
            print(f"[Download] background_resumed flag update failed for #{download_id}: {_fe}")
    # 2. CAS claim BEFORE spawning the worker (认领即 running)
    if not claim_task_for_resume(task_id, ('backgrounded',)):
        print(f"[Download] Task {task_id}: resume claim failed (claimed by another path), skipping")
        return
    try:
        threading.Thread(
            target=_run_background_task,
            args=(task_id, user_query, context, True),
            daemon=True).start()
    except Exception as e:
        print(f"[Download] _direct_resume_background_task failed for task {task_id}: {e}")



def _inject_notice_to_running_agent(task_id: int, session_id: int, notice: str) -> bool:
    """Inject a 【系统通知】 into the agent still running this task, if any.

    Foreground agents register in _active_agents[session_id][task_id (or 0)];
    running background agents live in _background_agents[task_id]. Reuses the
    standard queue_message injection path (same as user interjections), so the
    agent sees the notice on its next iteration instead of reporting stale
    progress. Returns True when the notice was delivered to a live agent.
    """
    try:
        candidates = []
        bg_agent = _background_agents.get(task_id)
        if bg_agent is not None:
            candidates.append(bg_agent)
        session_agents = _active_agents.get(session_id, {}) if session_id else {}
        # Task-keyed agent first (most specific), then ALL other session
        # agents: a foreground agent may be registered under key 0 (task id
        # unknown at registration), and an interrupted exact match must not
        # shadow a live session agent — try every candidate in order.
        if task_id in session_agents:
            candidates.append(session_agents[task_id])
        candidates.extend(a for tid, a in session_agents.items() if tid != task_id)
        for agent in candidates:
            if agent is not None and not getattr(agent, "is_interrupted", False):
                agent.queue_message(notice)
                return True
    except Exception as e:
        print(f"[Download] Failed to inject notice into running agent: {e}")
    return False



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
                        # Live-notify the agent if it is still running so it
                        # reports based on facts, not assumptions.
                        _notice = (f"【系统通知】下载完成: {label}{path_hint}\n"
                                   "文件已就绪。向用户汇报时必须基于本通知，不要重复下载。")
                        if _inject_notice_to_running_agent(task_id, session_id, _notice):
                            print(f"[Download] Injected completion notice into running agent (task {task_id})")
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
                        # Live-notify the agent if it is still running — it must
                        # report the failure honestly instead of claiming success.
                        _notice = (f"【系统通知】下载失败: {label}\n错误信息: {err}\n"
                                   "请分析失败原因并向用户如实说明失败，严禁谎称下载成功；"
                                   "如需换源重试，请先验证新源上文件确实存在。")
                        if _inject_notice_to_running_agent(task_id, session_id, _notice):
                            print(f"[Download] Injected failure notice into running agent (task {task_id})")
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



router = APIRouter()


def _load_janitor_section() -> dict:
    """GET /api/settings 用：返回合并缺省后的 sandbox_janitor 配置节。"""
    try:
        from core.sandbox_janitor import load_janitor_config
        return load_janitor_config()
    except Exception:
        return {"enabled": True, "tmp_ttl_days": 7, "interval_hours": 1.0,
                "soft_gb": 20, "hard_gb": 50}


def _sanitize_janitor_section(raw: dict) -> dict:
    """POST /api/settings 用：白名单校验 sandbox_janitor 配置节——只收已知键，
    数值钳制非负（interval_hours 至少 0.01），坏值抛 400 由调用方转成 HTTPException。"""
    allowed = {"enabled", "tmp_ttl_days", "interval_hours", "soft_gb", "hard_gb"}
    out = {}
    for key, value in (raw or {}).items():
        if key not in allowed:
            continue
        if key == "enabled":
            out[key] = bool(value)
            continue
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"sandbox_janitor.{key} 必须是数字")
        if key == "interval_hours":
            num = max(num, 0.01)
        else:
            num = max(num, 0.0)
        out[key] = int(num) if num == int(num) else num
    return out


class ConfigUpdate(BaseModel):
    # All fields optional: POST /api/settings is incremental — only fields
    # explicitly provided (non-None) are written to config, so partial
    # payloads (e.g. MCP-only save) never clobber unrelated settings.
    api_keys: Optional[Dict[str, str]] = None
    default_model: Optional[str] = None
    fallback_models: Optional[List[str]] = None
    disabled_skills: Optional[List[str]] = None
    sandbox_mode: Optional[bool] = None
    sandbox_dir: Optional[str] = None
    llamacpp_ctx_size: Optional[int] = None
    browser_headless: Optional[bool] = None
    http_proxy: Optional[str] = None
    heartbeat_enabled: Optional[bool] = None
    heartbeat_interval: Optional[int] = None
    email_listener_enabled: Optional[bool] = None
    email_account: Optional[str] = None
    email_password: Optional[str] = None
    email_imap_server: Optional[str] = None
    email_smtp_server: Optional[str] = None
    owner_email: Optional[str] = None
    mcp_servers: Optional[Dict[str, Any]] = None
    session_id: Optional[int] = None
    tool_permissions: Optional[Dict[str, Any]] = None
    searxng_url: Optional[str] = None
    searxng_port: Optional[int] = None
    max_correction_attempts: Optional[int] = None
    cold_cache_ttl: Optional[int] = None
    max_resume_count: Optional[int] = None
    max_total_tokens: Optional[int] = None
    tool_tiered_exposure: Optional[bool] = None
    sandbox_janitor: Optional[Dict[str, Any]] = None
    # 访问控制：局域网访问密码。空字符串 = 清除密码 = 恢复仅本机访问
    access_password: Optional[str] = None
    # 自定义厂商（OpenAI 兼容端点）：[{name, base_url, api_key, models[]}]
    custom_providers: Optional[List[Dict[str, Any]]] = None
    # 调度者（分身）模式开关与分身叫法
    dispatcher_mode: Optional[bool] = None
    agent_worker_name: Optional[str] = None
    # 视觉模型配置：vision_models 按模型名包含匹配；vision_capable 强制开关
    vision_models: Optional[List[str]] = None
    vision_capable: Optional[bool] = None



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

        "vision_models": config.get("vision_models", []),

        "vision_capable": config.get("vision_capable", None),

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

        "tool_tiered_exposure": config.get("tool_tiered_exposure", True),

        "context_budget": config.get("context_budget", {"max_total_tokens": 128000}),

        "mcp_servers": config.get("mcp_servers", {}),

        "sandbox_janitor": _load_janitor_section(),

        # 访问控制：只暴露「是否已设置」，绝不回传密码本身
        "access_password_set": bool((config.get("access_password") or "").strip()),

        # 自定义厂商（api_key 掩码）、调度者模式与分身叫法
        "custom_providers": [
            {**cp, "api_key": (f"{str(cp.get('api_key',''))[:3]}...{str(cp.get('api_key',''))[-3:]}"
                               if len(str(cp.get("api_key", ""))) > 6 else
                               ("***" if cp.get("api_key") else ""))}
            for cp in (config.get("custom_providers") or [])
        ],
        "dispatcher_mode": bool(config.get("dispatcher_mode", False)),
        "agent_worker_name": config.get("agent_worker_name", "分身"),

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

        "kimi_code": "KIMI_CODE_API_KEY",

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

        if config_update.api_keys is not None:

            for provider, new_key in config_update.api_keys.items():

                # Reject masked values ("xxx...xxx" from GET, or "***") so a
                # mask can never be persisted as a real key.
                if new_key and not new_key.endswith("***") and "..." not in new_key:

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

        # Incremental update: only write fields explicitly provided (non-None).

        if config_update.default_model is not None:

            config["default_model"] = config_update.default_model

        if config_update.fallback_models is not None:

            config["fallback_models"] = config_update.fallback_models

        if config_update.disabled_skills is not None:

            config["disabled_skills"] = config_update.disabled_skills

        if config_update.sandbox_mode is not None:

            config["sandbox_mode"] = config_update.sandbox_mode

        if config_update.sandbox_dir is not None:

            config["sandbox_dir"] = os.path.abspath(config_update.sandbox_dir) if config_update.sandbox_dir else os.path.abspath(os.path.join(os.getcwd(), "workspace"))

        if config_update.llamacpp_ctx_size is not None:

            config["llamacpp_ctx_size"] = config_update.llamacpp_ctx_size

        if config_update.vision_models is not None:

            config["vision_models"] = [str(s).strip() for s in config_update.vision_models if str(s).strip()]

        if config_update.vision_capable is not None:

            config["vision_capable"] = config_update.vision_capable

        if config_update.browser_headless is not None:

            config["browser_headless"] = config_update.browser_headless

        # 自定义厂商（OpenAI 兼容端点）：整体替换（前端管理的就是完整列表）
        if config_update.custom_providers is not None:

            _clean = []
            for cp in config_update.custom_providers:
                try:
                    name = str(cp.get("name", "")).strip()
                    base_url = str(cp.get("base_url", "")).strip()
                    if not (name and base_url):
                        continue
                    _clean.append({
                        "name": name, "base_url": base_url,
                        "api_key": str(cp.get("api_key", "") or "").strip(),
                        "models": [str(m).strip() for m in (cp.get("models") or []) if str(m).strip()],
                    })
                except Exception:
                    continue
            config["custom_providers"] = _clean

        # 调度者（分身）模式与叫法
        if config_update.dispatcher_mode is not None:

            config["dispatcher_mode"] = bool(config_update.dispatcher_mode)

        if config_update.agent_worker_name is not None:

            config["agent_worker_name"] = (config_update.agent_worker_name or "").strip() or "分身"

        if config_update.tool_tiered_exposure is not None:

            config["tool_tiered_exposure"] = config_update.tool_tiered_exposure

        if config_update.http_proxy is not None:

            config["http_proxy"] = config_update.http_proxy

        if config_update.heartbeat_enabled is not None:

            config["heartbeat_enabled"] = config_update.heartbeat_enabled

        if config_update.heartbeat_interval is not None:

            config["heartbeat_interval"] = config_update.heartbeat_interval

        if config_update.email_listener_enabled is not None:

            config["email_listener_enabled"] = config_update.email_listener_enabled

        if config_update.email_account is not None:

            config["email_account"] = config_update.email_account

        if config_update.email_password is not None and config_update.email_password != "***":

            config["email_password"] = config_update.email_password

        if config_update.email_imap_server is not None:

            config["email_imap_server"] = config_update.email_imap_server

        if config_update.email_smtp_server is not None:

            config["email_smtp_server"] = config_update.email_smtp_server

        if config_update.owner_email is not None:

            config["owner_email"] = config_update.owner_email

        if config_update.mcp_servers is not None:

            config["mcp_servers"] = config_update.mcp_servers

        if config_update.tool_permissions is not None:

            config["tool_permissions"] = config_update.tool_permissions

        if config_update.searxng_url is not None:

            config["searxng_url"] = config_update.searxng_url

            set_key(env_file, "SEARXNG_URL", config_update.searxng_url)

            os.environ["SEARXNG_URL"] = config_update.searxng_url

        if config_update.searxng_port is not None:

            config["searxng_port"] = config_update.searxng_port

        if config_update.max_correction_attempts is not None:

            config["max_correction_attempts"] = config_update.max_correction_attempts

        if config_update.cold_cache_ttl is not None:

            config["cold_cache_ttl"] = config_update.cold_cache_ttl

        if config_update.max_resume_count is not None:

            config["max_resume_count"] = config_update.max_resume_count

        if config_update.sandbox_janitor is not None:

            section = config.get("sandbox_janitor") or {}

            if not isinstance(section, dict):

                section = {}

            section.update(_sanitize_janitor_section(config_update.sandbox_janitor))

            config["sandbox_janitor"] = section

        if config_update.max_total_tokens is not None:

            _budget = config.get("context_budget", {})

            if not isinstance(_budget, dict):

                _budget = {}

            _budget["max_total_tokens"] = config_update.max_total_tokens

            config["context_budget"] = _budget

        # 访问控制密码：非空写入；空字符串 = 清除（恢复仅本机访问）。
        # 密码变更后已签发的签名令牌自然失效（HMAC 密钥含密码）。
        if config_update.access_password is not None:

            _pw = config_update.access_password.strip()

            if _pw:

                config["access_password"] = _pw

            else:

                config.pop("access_password", None)



        # Save per-session email config when session_id is provided — but only
        # when the payload actually carries email fields; partial saves (e.g.
        # MCP-only) must not write NULLs into the sessions row.
        _email_fields = (config_update.email_listener_enabled, config_update.email_account,
                         config_update.email_password, config_update.email_imap_server,
                         config_update.email_smtp_server, config_update.owner_email)

        if config_update.session_id is not None and any(f is not None for f in _email_fields):

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

        

        if config_update.default_model is not None:

            set_key(env_file, "DEFAULT_MODEL", config_update.default_model)

            os.environ["DEFAULT_MODEL"] = config_update.default_model



        # Save to JSON (atomic write via save_config)

        save_config(config)

            

        load_dotenv(env_file, override=True)

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

    # 自定义厂商：直接返回配置里登记的模型列表（id 为 <name>/<model>），
    # 并尝试从端点 /models 拉取补充（OpenAI 兼容）
    _cp_name = provider[len("custom:"):] if provider.startswith("custom:") else None
    if _cp_name:
        for cp in (config.get("custom_providers") or []):
            if str(cp.get("name", "")) == _cp_name:
                base_url = str(cp.get("base_url", "")).rstrip("/")
                key = str(cp.get("api_key", "") or "")
                seen = set()
                for m in (cp.get("models") or []):
                    full = f"{_cp_name}/{m}"
                    if full not in seen:
                        models.append(full)
                        seen.add(full)
                try:
                    headers = {"Authorization": f"Bearer {key}"} if key else {}
                    res = requests.get(f"{base_url}/models", headers=headers, timeout=5)
                    if res.status_code == 200:
                        for m in (res.json().get("data") or []):
                            mid = m.get("id") if isinstance(m, dict) else None
                            if mid:
                                full = f"{_cp_name}/{mid}"
                                if full not in seen:
                                    models.append(full)
                                    seen.add(full)
                except Exception:
                    pass
                return {"models": models}
        return {"models": []}

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

    elif provider == "xiaomi":

        # 小米 MiMo（OpenAI 兼容端点）：先拉端点 /models，失败回退预置
        key = api_keys.get("xiaomi")

        _preset = ["xiaomi/mimo-v2.5", "xiaomi/mimo-v2.5-pro"]
        try:
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            res = requests.get("https://api.xiaomimimo.com/v1/models", headers=headers, timeout=5)
            if res.status_code == 200:
                models = [f"xiaomi/{m['id']}" for m in res.json().get("data", []) if m.get("id")]
        except Exception:
            pass
        if not models:
            models = _preset





    # Fallback default models if API call fails or key not set

    # Model names include litellm provider prefix as required by litellm.completion()

    if not models:

        defaults = {

            'openai': ['gpt-4o', 'gpt-4o-mini', 'gpt-4-turbo', 'gpt-3.5-turbo'],

            'anthropic': ['claude-3-5-sonnet-20240620', 'claude-3-opus-20240229', 'claude-3-haiku-20240307'],

            'deepseek': ['deepseek/deepseek-chat', 'deepseek/deepseek-reasoner'],

            'gemini': ['gemini/gemini-1.5-pro', 'gemini/gemini-2.5-pro-preview-05-06'],

            'kimi': ['moonshot/kimi-k2.6', 'moonshot/kimi-k2.5', 'moonshot/kimi-latest', 'moonshot/moonshot-v1-8k', 'moonshot/moonshot-v1-32k', 'moonshot/moonshot-v1-128k'],

            'kimi_code': ['kimi_code/kimi-for-coding', 'kimi_code/kimi-for-coding-highspeed', 'kimi_code/k3'],

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

    # Mutated in place (never rebound) so api/ws.py, tools/download.py and
    # other holders of the original dict reference stay in sync.

    if _llamacpp_download_state["active"]:

        raise HTTPException(status_code=409, detail="下载任务正在进行中")



    _llamacpp_download_state.clear()

    _llamacpp_download_state.update({

        "active": True,

        "type": "binary",

        "label": "正在下载 llama.cpp 二进制文件...",

        "progress": 0.0,

        "stage": "downloading",

        "error": "",

        "cancelled": False

    })



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

                _llamacpp_download_state.update({"active": False, "stage": "complete", "progress": 1.0})

                update_download_progress(dl_id, 1.0, status="completed")

                _broadcast_to_websockets({

                    "type": "llamacpp_download",

                    "task": "binary",

                    "label": "llama.cpp 安装完成",

                    "progress": 1.0,

                    "stage": "complete"

                })

            else:

                _llamacpp_download_state.update({"active": False, "stage": "error", "error": "下载失败"})

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

            _llamacpp_download_state.update({"active": False, "stage": "error", "error": str(e)})

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

    # Mutated in place (never rebound) — see setup_llamacpp.

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



    _llamacpp_download_state.clear()

    _llamacpp_download_state.update({

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

    })



    def run_download():

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

                _llamacpp_download_state.update({"active": False, "stage": "complete", "progress": 1.0})

                update_download_progress(dl_id, 1.0, status="completed")

                _broadcast_to_websockets({

                    "type": "llamacpp_download",

                    "task": "model",

                    "label": f"{short_name} 下载完成",

                    "progress": 1.0,

                    "stage": "complete"

                })

            else:

                _llamacpp_download_state.update({"active": False, "stage": "error", "error": "下载中断，可重新下载自动续传"})

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

            _llamacpp_download_state.update({"active": False, "stage": "error", "error": str(e)})

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

    # Mutated in place (never rebound) — see setup_llamacpp.

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



    _llamacpp_download_state.clear()

    _llamacpp_download_state.update({

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

    })



    update_download_progress(download_id, record["progress"] or 0.0,

                             downloaded_bytes=resume_offset,

                             status="downloading", error_message="")



    def run_resume():

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

                _llamacpp_download_state.update({"active": False, "stage": "complete", "progress": 1.0})

                update_download_progress(dl_id, 1.0, status="completed")

                _broadcast_to_websockets({

                    "type": "llamacpp_download",

                    "task": record["type"],

                    "label": f"{short_name} 下载完成",

                    "progress": 1.0,

                    "stage": "complete"

                })

            else:

                _llamacpp_download_state.update({"active": False, "stage": "error", "error": "下载失败"})

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

            _llamacpp_download_state.update({"active": False, "stage": "error", "error": str(e)})

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

        # client.chat 是同步网络调用，直接 await 会冻结事件循环 —— 移入线程池
        loop = asyncio.get_running_loop()
        response, actual_model = await loop.run_in_executor(
            None, lambda: client.chat(messages=messages, tools=None))

        reply = response.choices[0].message.content or ""

        return {"response": reply, "model_used": actual_model}

    except Exception as e:

        raise HTTPException(status_code=500, detail=f"AI design failed: {str(e)}")





# ==========================================

# Task Management API

# ==========================================

# Tasks and Processes API routes imported from api.routes.routes_tasks
