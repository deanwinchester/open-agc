"""Plugins, Marketplace, Sandbox API endpoints."""
import os
from fastapi import APIRouter, HTTPException, Request
from api.config import load_config

router = APIRouter()


# ── Sandbox API ──

@router.post("/api/sandbox/approve")
async def approve_sandbox_request(body: dict):
    """Approve a sandbox path via persistent config."""
    from api.state import _sandbox_waits
    sid = body.get("session_id", body.get("sid", 1))
    wait = _sandbox_waits.get(sid)
    if wait:
        action = body.get("action", "deny_once")
        wait["result"]["action"] = action
        wait["result"]["path"] = body.get("path", "")
        wait["event"].set()
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

_plugins_dir = os.path.abspath(os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "plugins"))


@router.get("/api/plugins")
async def get_plugins():
    from core.plugin_manager import list_all_plugins
    return {"plugins": list_all_plugins(_plugins_dir), "plugins_dir": os.path.abspath(_plugins_dir)}


@router.post("/api/plugins/scan")
async def scan_plugins():
    """Re-scan and mount plugins."""
    import api.server as _srv
    from core.plugin_manager import discover_plugins, list_plugins
    _srv._plugins = discover_plugins(plugins_dir=_plugins_dir,
                                      broadcast_fn=_srv._broadcast_to_websockets,
                                      server_config=load_config())
    _srv._mount_plugins(_srv.app, _srv._plugins)
    return {"status": "ok", "count": len(_srv._plugins), "plugins": list_plugins()}


@router.post("/api/plugins/{name}/toggle")
async def plugin_toggle(name: str):
    from core.plugin_manager import toggle_plugin
    new_state = toggle_plugin(name, _plugins_dir)
    return {"status": "ok", "enabled": new_state.get("enabled", True)}


@router.post("/api/plugins/install")
async def plugin_install(req: Request):
    import json as _json
    body = await req.json()
    name, url = body.get("name", ""), body.get("url", "")
    if not name or not url:
        raise HTTPException(status_code=400, detail="name and url required")
    from core.plugin_manager import install_from_git
    ok = install_from_git(name, url, _plugins_dir)
    if not ok:
        raise HTTPException(status_code=500, detail="Install failed")
    return {"status": "ok", "message": f"Plugin {name} installed"}


@router.delete("/api/plugins/{name}")
async def plugin_delete(name: str):
    import shutil
    d = os.path.join(_plugins_dir, name)
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
