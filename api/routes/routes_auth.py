# -*- coding: utf-8 -*-
"""认证端点：/api/auth/login（密码换签名 Cookie）与 /api/auth/check（自检）。

三层访问策略本身由 core.access_control.AccessControlMiddleware 执行；
这两个端点被中间件列为 AUTH_EXEMPT_PATHS（局域网未认证可到达），
公网 403 与「未配置密码 = 仅本机」仍由中间件先行拦截。
"""
import hmac
import threading
import time
from typing import Dict, List

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from api.config import load_config
from core.access_control import (
    TOKEN_COOKIE_NAME,
    TOKEN_TTL_SECONDS,
    classify_ip,
    get_access_password,
    issue_token,
    verify_token,
)

router = APIRouter()


# ── 登录失败限速（进程内存，单用户/单实例场景足够）──
# 同一客户端 IP 在 _LOGIN_WINDOW_SECONDS 内连续失败 _LOGIN_MAX_FAILURES 次后，
# 窗口剩余时间内一律 429；登录成功立即清零。
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 60
_login_failures: Dict[str, List[float]] = {}  # ip -> [fail_count, window_start_ts]
_login_lock = threading.Lock()


def _login_throttled(ip: str) -> bool:
    """True = 该 IP 当前处于限速窗口，应拒绝（429）。"""
    with _login_lock:
        rec = _login_failures.get(ip)
        return bool(
            rec and rec[0] >= LOGIN_MAX_FAILURES
            and time.time() - rec[1] < LOGIN_WINDOW_SECONDS
        )


def _login_record_failure(ip: str) -> None:
    now = time.time()
    with _login_lock:
        rec = _login_failures.get(ip)
        if rec and now - rec[1] < LOGIN_WINDOW_SECONDS:
            rec[0] += 1
        else:
            _login_failures[ip] = [1, now]


def _login_clear(ip: str) -> None:
    with _login_lock:
        _login_failures.pop(ip, None)


class LoginBody(BaseModel):
    password: str = ""


def _client_layer(request: Request) -> str:
    host = request.client.host if request.client else ""
    return classify_ip(host)


@router.post("/api/auth/login")
async def auth_login(body: LoginBody, request: Request):
    """局域网客户端用密码换取 HttpOnly 签名 Cookie（7 天有效）。"""
    if _client_layer(request) == "local":
        # 本机免密，直接视为已认证（不发 Cookie，也不需要）
        return {"status": "success", "authenticated": True, "layer": "local"}

    client_ip = request.client.host if request.client else ""
    if _login_throttled(client_ip):
        raise HTTPException(status_code=429, detail="失败次数过多，请 60 秒后再试。")

    password = get_access_password(load_config())
    if not password:
        raise HTTPException(status_code=403, detail="本实例未配置局域网访问密码，仅允许本机访问。")
    if not hmac.compare_digest(body.password or "", password):
        _login_record_failure(client_ip)
        raise HTTPException(status_code=403, detail="访问密码错误。")

    _login_clear(client_ip)
    token = issue_token(password)
    resp = JSONResponse({"status": "success", "authenticated": True})
    resp.set_cookie(
        TOKEN_COOKIE_NAME, token,
        max_age=TOKEN_TTL_SECONDS, httponly=True, samesite="lax", path="/",
    )
    return resp


@router.get("/api/auth/check")
async def auth_check(request: Request):
    """启动自检：前端据此判断当前客户端是否已具备访问资格。"""
    layer = _client_layer(request)
    if layer == "local":
        return {"authenticated": True, "layer": "local"}
    password = get_access_password(load_config())
    if not password:
        # 未配置密码：非本机无法访问（公网/空密码已由中间件 403，这里
        # 只会被局域网客户端到达，如实报告未认证）
        return {"authenticated": False, "layer": layer}
    token = request.cookies.get(TOKEN_COOKIE_NAME, "")
    return {"authenticated": bool(token and verify_token(token, password)), "layer": layer}
<<<<<<< HEAD
=======


@router.get("/api/bootstrap/ws-token")
async def bootstrap_ws_token(request: Request):
    """仅本机回环客户端可领取当前进程的 WS 会话令牌。"""
    if _client_layer(request) != "local":
        raise HTTPException(status_code=403, detail="WS 会话令牌仅允许本机获取。")
    from api import state
    return {"token": state._WS_TOKEN}


# ── 中央服务器账号登录（zxs 产品线，配置 server_url 时启用）──────────────
# 与上面的实例级访问密码是两套机制：server_url 存在时由账号登录取代
# 实例密码（LAN 逐请求携带同一 JWT，公网仍禁），token 同时作为内网大模型
# 网关的调用凭据（服务端按账号记账/限额）。


class ServerLoginBody(BaseModel):
    username: str = ""
    password: str = ""


@router.post("/api/auth/server-login")
async def server_login(body: ServerLoginBody, request: Request):
    """账号密码登录中央服务器，落本地 token。"""
    from core import server_auth
    if not server_auth.enabled():
        raise HTTPException(status_code=404, detail="当前实例未启用账号登录。")
    client_ip = request.client.host if request.client else ""
    if _login_throttled(client_ip):
        raise HTTPException(status_code=429, detail="失败次数过多，请 60 秒后再试。")
    try:
        info = server_auth.login(body.username, body.password)
    except server_auth.ServerAuthError as e:
        _login_record_failure(client_ip)
        raise HTTPException(status_code=401, detail=str(e))
    _login_clear(client_ip)
    # LAN 客户端后续必须自行携带同一 JWT；本机仍由中间件免凭据判定。
    return {"status": "success", **info,
            "token": server_auth.get_token(), "layer": _client_layer(request)}


@router.get("/api/auth/server-status")
async def server_status():
    """账号登录态快照：前端启动时据此决定是否弹登录遮罩。"""
    from core import server_auth
    return server_auth.status()


@router.post("/api/auth/server-logout")
async def server_logout():
    """登出：吊销服务端 token 并清除本地登录态（幂等）。"""
    from core import server_auth
    server_auth.logout()
    return {"ok": True}


@router.get("/api/auth/server-profile")
async def server_profile_get():
    """本人资料（姓名/手机号/部门/额度），供「个人信息」页只读展示。"""
    from core import server_auth
    try:
        p = server_auth.fetch_profile()
    except server_auth.ServerAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    return {"username": p.get("username", ""), "display_name": p.get("display_name", ""),
            "phone": p.get("phone", ""), "department": p.get("department", ""),
            "token_quota": p.get("token_quota"), "token_used": p.get("token_used", 0)}


class ServerPasswordBody(BaseModel):
    old_password: str = ""
    new_password: str = ""


@router.post("/api/auth/server-password")
async def server_password_change(body: ServerPasswordBody):
    """修改本人密码（服务端校验旧密码）。"""
    from core import server_auth
    try:
        server_auth.change_password(body.old_password, body.new_password)
    except server_auth.ServerAuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "success"}
>>>>>>> 7edbead (feat(security): 远程接管/供应链/沙箱逃逸三层收口 + promote 版本一致 + UOS 侧栏)
