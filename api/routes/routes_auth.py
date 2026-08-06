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
