# -*- coding: utf-8 -*-
"""MCP server 管理 API（智能广场·连接应用）。

- GET    /api/mcp/servers            配置+实时状态聚合（连接/工具数/错误）
- POST   /api/mcp/servers            添加（{name,url} 或 {name,command,args?,env?}）+ 热加载
- DELETE /api/mcp/servers/{name}     删除配置 + 拆除会话
- POST   /api/mcp/servers/{name}/test  ping + list_tools 实测
"""
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any, Dict, List, Optional

from api.config import load_config, save_config
from tools.mcp_tool import get_mcp_manager, resolve_mcp_config

router = APIRouter()

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def _servers_from_config() -> dict:
    return dict((load_config() or {}).get("mcp_servers") or {})


class McpServerRequest(BaseModel):
    name: str
    url: Optional[str] = None
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    headers: Optional[Dict[str, str]] = None


@router.get("/api/mcp/servers")
async def list_mcp_servers():
    cfg = _servers_from_config()
    status = get_mcp_manager().server_status()
    out = []
    for name, sc in cfg.items():
        st = status.get(name, {})
        out.append({
            "name": name,
            "display_name": sc.get("display_name") or "",
            "description": sc.get("description") or "",
            "url": sc.get("url") or "",
            "command": sc.get("command") or "",
            "args": sc.get("args") or [],
            "connected": bool(st.get("connected")),
            "tool_count": st.get("tool_count", 0),
            "tools": st.get("tools") or [],
            "error": st.get("error") or "",
        })
    return {"servers": out}


@router.post("/api/mcp/servers")
async def add_mcp_server(req: McpServerRequest):
    name = (req.name or "").strip()
    if not _NAME_RE.match(name):
        raise HTTPException(status_code=400,
                            detail="name 需为英文小写（字母开头，可含数字/下划线/中划线）")
    if bool(req.url) == bool(req.command):
        raise HTTPException(status_code=400, detail="url 与 command 必须且只能填一个")
    config = load_config() or {}
    servers = config.setdefault("mcp_servers", {})
    if name in servers:
        raise HTTPException(status_code=400, detail=f"server '{name}' 已存在")
    entry: Dict[str, Any] = {}
    if req.url:
        entry["url"] = req.url.strip()
        if req.headers:
            entry["headers"] = req.headers
    else:
        entry["command"] = req.command.strip()
        entry["args"] = [str(a) for a in (req.args or [])]
        if req.env:
            entry["env"] = req.env
    servers[name] = entry
    save_config(config)

    # 热加载并回传连接结果
    mgr = get_mcp_manager()
    mgr.load_servers(resolve_mcp_config({name: entry}))
    st = mgr.server_status().get(name, {})
    return {"status": "success", "name": name,
            "connected": bool(st.get("connected")),
            "tool_count": st.get("tool_count", 0),
            "error": st.get("error") or ""}


@router.delete("/api/mcp/servers/{name}")
async def delete_mcp_server(name: str):
    config = load_config() or {}
    servers = config.setdefault("mcp_servers", {})
    if name not in servers:
        raise HTTPException(status_code=404, detail=f"server '{name}' 不存在")
    servers.pop(name)
    save_config(config)
    mgr = get_mcp_manager()
    try:
        import asyncio
        asyncio.run_coroutine_threadsafe(mgr._teardown_session(name), mgr._loop)
    except Exception:
        pass
    mgr.servers.pop(name, None)
    return {"status": "success"}


@router.post("/api/mcp/servers/{name}/test")
async def test_mcp_server(name: str):
    servers = _servers_from_config()
    if name not in servers:
        raise HTTPException(status_code=404, detail=f"server '{name}' 不存在")
    mgr = get_mcp_manager()
    # 强制重连一次再测（配置可能刚改过）
    mgr.servers.pop(name, None)
    mgr.load_servers(resolve_mcp_config({name: servers[name]}))
    st = mgr.server_status().get(name, {})
    if st.get("connected"):
        return {"ok": True, "tool_count": st.get("tool_count", 0),
                "tools": st.get("tools") or []}
    return {"ok": False, "error": st.get("error") or "连接失败"}
