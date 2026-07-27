"""Plugins, Marketplace, Sandbox API endpoints."""
import os
from fastapi import APIRouter, HTTPException, Request
from api.config import load_config

router = APIRouter()


# ── Sandbox API ──

@router.post("/api/sandbox/approve")
async def approve_sandbox_request(body: dict):
    """Approve a sandbox path via persistent config."""
    from api.state import _sandbox_waits, resolve_sandbox_wait
    sid = body.get("session_id", body.get("sid", 1))
    # Prefer the unique request_id (concurrent waits in one session). Only fall
    # back to the legacy session_id key when the client sent no request_id —
    # a stale rid must not route the reply to another wait.
    req_id = body.get("request_id")
    wait = _sandbox_waits.get(req_id) if req_id else _sandbox_waits.get(sid)
    if wait:
        # Secret form fields pass through in-memory (see api.state)
        resolve_sandbox_wait(wait, body)
    return {"status": "ok"}


@router.post("/api/sandbox/remove-path")
async def remove_sandbox_path(body: dict):
    """Remove a path from the sandbox whitelist."""
    path = body.get("path", "")
    sid = body.get("session_id", 1)
    from api.state import _active_agents
    _agents = _active_agents.get(sid, {})
    for _a in _agents.values():
        if hasattr(_a, '_session_sandbox_whitelist'):
            _a._session_sandbox_whitelist.discard(path)
            import os as _os
            _a._session_sandbox_whitelist.discard(_os.path.dirname(_os.path.abspath(path)))
    return {"status": "ok"}


@router.post("/api/sandbox/remove-permission")
async def remove_tool_permission(body: dict):
    """Remove a tool permission override."""
    tool = body.get("tool", "")
    sid = body.get("session_id", 1)
    from api.state import _active_agents
    _agents = _active_agents.get(sid, {})
    for _a in _agents.values():
        if hasattr(_a, '_session_permission_whitelist'):
            _a._session_permission_whitelist.discard(tool)
    return {"status": "ok"}


# ── Plugins API ──

_builtin_plugins_dir = os.path.abspath(os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "plugins"))
_user_plugins_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "plugins")
os.makedirs(_user_plugins_dir, exist_ok=True)


def _all_plugin_dirs():
    """Return both built-in and user-installed plugin directories."""
    dirs = [_builtin_plugins_dir]
    if _user_plugins_dir != _builtin_plugins_dir:
        dirs.append(_user_plugins_dir)
    return dirs


def _find_plugin_dir(name: str) -> str:
    """Find which plugin directory a plugin lives in."""
    for d in _all_plugin_dirs():
        if os.path.isdir(os.path.join(d, name)):
            return d
    return _user_plugins_dir  # default to user dir for installs


@router.get("/api/plugins")
async def get_plugins():
    from core.plugin_manager import list_all_plugins
    # 只扫描用户插件目录（data/plugins）一次：已加载的插件（含仓库内置的
    # open-agc-train）经 _loaded_plugins 呈现一次；此前对内置+用户两个目录
    # 各调一次 list_all_plugins，每次都会带上 _loaded_plugins，导致内置插件
    # 在列表中出现两次。
    all_plugins = list_all_plugins(_user_plugins_dir)
    return {"plugins": all_plugins, "plugins_dir": str(_user_plugins_dir)}


def _plugins_to_preserve_on_scan() -> set:
    """Loaded plugins that must survive a scan because unloading would orphan live state.

    open-agc-train's TrainingEngine is a module-level singleton (engine.py
    get_training_engine); an active training job runs in a background thread
    owned by that engine. Unload+reload would purge the module and create a new
    engine with an empty job table — pause/abort/status would hit the new
    instance while the old thread keeps running untracked (and a second
    training could be started concurrently). Keeping the plugin loaded makes
    discover_plugins() reuse the existing instance and engine.
    """
    preserved = set()
    try:
        from core.plugin_manager import get_plugin
        info = get_plugin("open-agc-train")
        engine = (info.instance.state or {}).get("engine") if info and info.instance else None
        if engine is not None and engine.get_state().get("active"):
            preserved.add("open-agc-train")
    except Exception:
        pass  # 检测失败不得阻塞扫描
    return preserved


@router.post("/api/plugins/scan")
async def scan_plugins():
    """Re-scan and mount plugins.

    Unloads currently loaded plugins first (including their sys.modules
    entries) so code changes take effect on re-discovery — no server restart
    needed. Plugins with live state (active training, see
    _plugins_to_preserve_on_scan) are kept loaded and remounted as-is.
    Frontend vue-entry.js is re-read from disk by the static mount.
    """
    import api.server as _srv
    from core.plugin_manager import discover_plugins, list_plugins, unload_all_plugins
    unload_all_plugins(except_names=_plugins_to_preserve_on_scan())
    all_plugins = []
    for d in _all_plugin_dirs():
        all_plugins.extend(discover_plugins(plugins_dir=d,
                                             broadcast_fn=_srv._broadcast_to_websockets,
                                             server_config=load_config()))
    _srv._plugins = all_plugins
    _srv._mount_plugins(_srv.app, _srv._plugins)
    return {"status": "ok", "count": len(_srv._plugins), "plugins": list_plugins()}


@router.post("/api/plugins/{name}/toggle")
async def plugin_toggle(name: str):
    from core.plugin_manager import toggle_plugin
    pdir = _find_plugin_dir(name)
    new_state = toggle_plugin(name, pdir)
    return {"status": "ok", "enabled": new_state.get("enabled", True)}


@router.post("/api/plugins/install")
async def plugin_install(req: Request):
    import json as _json
    body = await req.json()
    name, url = body.get("name", ""), body.get("url", "")
    if not name or not url:
        raise HTTPException(status_code=400, detail="name and url required")
    from core.security import resolve_under
    try:
        resolve_under(_user_plugins_dir, name)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid plugin name: {name}")
    from core.plugin_manager import install_from_git
    ok = install_from_git(name, url, _user_plugins_dir)
    if not ok:
        raise HTTPException(status_code=500, detail="Install failed")
    return {"status": "ok", "message": f"Plugin {name} installed in data/plugins/"}


@router.delete("/api/plugins/{name}")
async def plugin_delete(name: str):
    import shutil
    from core.security import resolve_under
    pdir = _find_plugin_dir(name)
    try:
        d = resolve_under(pdir, name)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid plugin name: {name}")
    if not os.path.isdir(d):
        raise HTTPException(status_code=404, detail=f"Plugin not found: {name}")
    from core.plugin_manager import unload_plugin
    unload_plugin(name)
    shutil.rmtree(d)
    return {"status": "ok", "message": f"Plugin {name} deleted"}


@router.get("/api/marketplace")
async def get_marketplace():
    from core.plugin_manager import fetch_marketplace
    data = fetch_marketplace()
    return {"marketplace": data}
