# -*- coding: utf-8 -*-
"""访问控制层测试：IP 分类 / 令牌签发校验 / 中间件三层策略 / 认证端点 /
登录限速 / 启动入口 proxy_headers / 真实装配（api.server:app）不变量。

中间件行为主要用挂到最小测试 app 的 TestClient 验证；client IP 通过
TestClient(client=(ip, port)) 注入。末尾两组测试导入真实 api.server
（启动副作用与 tests/test_subagent_locks.py 相同，套件已容忍）。
"""
import ast
import os
import time

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import api.routes.routes_auth as routes_auth
from core.access_control import (
    AccessControlMiddleware,
    TOKEN_COOKIE_NAME,
    TOKEN_TTL_SECONDS,
    check_credential,
    classify_ip,
    get_access_password,
    issue_token,
    verify_token,
)

PASSWORD = "test-lan-password"


@pytest.fixture(autouse=True)
def _clear_login_failures():
    """登录限速计数器是 routes_auth 模块级状态，逐用例清零防跨测试累积。"""
    routes_auth._login_failures.clear()
    yield


# ── classify_ip ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("ip,expected", [
    ("127.0.0.1", "local"),
    ("127.0.0.2", "local"),          # 整个 127/8 都是 loopback
    ("::1", "local"),
    ("192.168.1.5", "lan"),
    ("10.0.0.2", "lan"),
    ("172.16.5.5", "lan"),
    ("172.31.255.255", "lan"),       # 172.16/12 上界
    ("fc00::1", "lan"),              # IPv6 ULA
    ("fd12:3456::1", "lan"),         # ULA 也在 fc00::/7 内
    ("fe80::1", "lan"),              # IPv6 链路本地
    ("::ffff:127.0.0.1", "local"),   # v4-mapped loopback
    ("::ffff:192.168.1.5", "lan"),   # v4-mapped LAN
    ("8.8.8.8", "public"),
    ("172.32.0.1", "public"),        # 恰好超出 172.16/12
    ("2001:db8::1", "public"),       # 公网 IPv6
    ("", "public"),                  # 无法解析按公网处理（安全默认）
    ("not-an-ip", "public"),
])
def test_classify_ip(ip, expected):
    assert classify_ip(ip) == expected


# ── 令牌签发 / 校验 ───────────────────────────────────────────────────────

def test_token_issue_and_verify_roundtrip():
    token = issue_token(PASSWORD)
    assert verify_token(token, PASSWORD)
    # 格式 {expiry_ts}.{sig}
    expiry_str, _, sig = token.rpartition(".")
    assert expiry_str.isdigit() and len(sig) == 64


def test_token_expired_rejected():
    now = time.time()
    token = issue_token(PASSWORD, now=now - TOKEN_TTL_SECONDS - 10)
    assert not verify_token(token, PASSWORD, now=now)


def test_token_tampered_rejected():
    token = issue_token(PASSWORD)
    # 篡改签名
    expiry_str, _, sig = token.rpartition(".")
    bad_sig = ("0" if sig[0] != "0" else "1") + sig[1:]
    assert not verify_token(f"{expiry_str}.{bad_sig}", PASSWORD)
    # 篡改过期时间（延长有效期）
    assert not verify_token(f"{int(expiry_str) + 10**9}.{sig}", PASSWORD)
    # 错误密码 / 畸形令牌
    assert not verify_token(token, "wrong-password")
    assert not verify_token("garbage", PASSWORD)
    assert not verify_token("", PASSWORD)


def test_check_credential_accepts_plaintext_or_token():
    assert check_credential(PASSWORD, PASSWORD)
    assert check_credential(issue_token(PASSWORD), PASSWORD)
    assert not check_credential("wrong", PASSWORD)
    assert not check_credential(PASSWORD, "")


def test_get_access_password_strips_and_defaults_empty():
    assert get_access_password({"access_password": "  pw  "}) == "pw"
    assert get_access_password({}) == ""
    assert get_access_password({"access_password": "   "}) == ""


# ── 中间件行为（TestClient + 注入客户端 IP）──────────────────────────────

def _make_app(monkeypatch, password):
    """最小测试 app：一个 HTTP 端点 + 一个 WS 端点 + auth 路由。"""
    monkeypatch.setattr(routes_auth, "load_config",
                        lambda: {"access_password": password} if password else {})
    app = FastAPI()

    @app.get("/api/ping")
    def ping():
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"ok": True})
        await websocket.close()

    app.add_middleware(AccessControlMiddleware,
                       config_loader=lambda: {"access_password": password} if password else {})
    app.include_router(routes_auth.router)
    return app


def _client(app, ip):
    return TestClient(app, client=(ip, 50000))


def test_local_always_allowed(monkeypatch):
    app = _make_app(monkeypatch, PASSWORD)
    assert _client(app, "127.0.0.1").get("/api/ping").status_code == 200
    assert _client(app, "::1").get("/api/ping").status_code == 200
    assert _client(app, "::ffff:127.0.0.1").get("/api/ping").status_code == 200


def test_local_allowed_when_password_empty(monkeypatch):
    app = _make_app(monkeypatch, "")
    assert _client(app, "127.0.0.1").get("/api/ping").status_code == 200


def test_lan_without_credential_401(monkeypatch):
    app = _make_app(monkeypatch, PASSWORD)
    resp = _client(app, "192.168.1.5").get("/api/ping")
    assert resp.status_code == 401
    assert "密码" in resp.json()["detail"]


def test_lan_bearer_plaintext_password_passes(monkeypatch):
    app = _make_app(monkeypatch, PASSWORD)
    resp = _client(app, "10.0.0.2").get(
        "/api/ping", headers={"Authorization": f"Bearer {PASSWORD}"})
    assert resp.status_code == 200


def test_lan_bearer_signed_token_passes(monkeypatch):
    app = _make_app(monkeypatch, PASSWORD)
    token = issue_token(PASSWORD)
    resp = _client(app, "192.168.1.5").get(
        "/api/ping", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


def test_lan_wrong_password_403(monkeypatch):
    app = _make_app(monkeypatch, PASSWORD)
    resp = _client(app, "192.168.1.5").get(
        "/api/ping", headers={"Authorization": "Bearer wrong-password"})
    assert resp.status_code == 403


def test_lan_login_then_cookie_passes(monkeypatch):
    app = _make_app(monkeypatch, PASSWORD)
    client = _client(app, "192.168.1.5")

    login = client.post("/api/auth/login", json={"password": PASSWORD})
    assert login.status_code == 200
    assert login.json()["authenticated"] is True
    # HttpOnly Cookie 已种下
    set_cookie = login.headers.get("set-cookie", "")
    assert TOKEN_COOKIE_NAME in set_cookie and "httponly" in set_cookie.lower()

    # 后续请求凭 Cookie 放行（TestClient 自动携带）
    assert client.get("/api/ping").status_code == 200


def test_lan_login_wrong_password_403(monkeypatch):
    app = _make_app(monkeypatch, PASSWORD)
    resp = _client(app, "192.168.1.5").post(
        "/api/auth/login", json={"password": "nope"})
    assert resp.status_code == 403


def test_lan_expired_or_tampered_cookie_401(monkeypatch):
    app = _make_app(monkeypatch, PASSWORD)
    expired = issue_token(PASSWORD, now=time.time() - TOKEN_TTL_SECONDS - 10)
    for bad in (expired, "1.deadbeef"):
        client = _client(app, "192.168.1.5")
        client.cookies.set(TOKEN_COOKIE_NAME, bad)
        assert client.get("/api/ping").status_code == 401


def test_public_always_403(monkeypatch):
    app = _make_app(monkeypatch, PASSWORD)
    for ip in ("8.8.8.8", "2001:db8::1"):
        client = _client(app, ip)
        assert client.get("/api/ping").status_code == 403
        # 公网即使拿到正确 Bearer 也一律拒绝
        assert client.get(
            "/api/ping",
            headers={"Authorization": f"Bearer {PASSWORD}"}).status_code == 403
        # 公网无法到达登录端点
        assert client.post(
            "/api/auth/login", json={"password": PASSWORD}).status_code == 403


def test_empty_password_blocks_all_non_local(monkeypatch):
    app = _make_app(monkeypatch, "")
    resp = _client(app, "192.168.1.5").get("/api/ping")
    assert resp.status_code == 403
    assert "本机" in resp.json()["detail"]
    # 未配置密码时登录无意义，同样 403
    login = _client(app, "192.168.1.5").post(
        "/api/auth/login", json={"password": "whatever"})
    assert login.status_code == 403


def test_auth_check(monkeypatch):
    app = _make_app(monkeypatch, PASSWORD)
    # 本机：直接已认证
    local = _client(app, "127.0.0.1").get("/api/auth/check")
    assert local.status_code == 200 and local.json()["authenticated"] is True
    # 局域网无 Cookie：未认证（200 + false，由前端决定弹密码页）
    lan = _client(app, "192.168.1.5")
    anon = lan.get("/api/auth/check")
    assert anon.status_code == 200 and anon.json()["authenticated"] is False
    # 登录后：已认证
    lan.post("/api/auth/login", json={"password": PASSWORD})
    authed = lan.get("/api/auth/check")
    assert authed.json()["authenticated"] is True


def test_local_login_is_noop_success(monkeypatch):
    app = _make_app(monkeypatch, PASSWORD)
    resp = _client(app, "127.0.0.1").post("/api/auth/login", json={"password": ""})
    assert resp.status_code == 200 and resp.json()["authenticated"] is True


def test_websocket_covered_by_middleware(monkeypatch):
    app = _make_app(monkeypatch, PASSWORD)
    # 本机 WebSocket 正常握手
    with _client(app, "127.0.0.1").websocket_connect("/ws") as ws:
        assert ws.receive_json() == {"ok": True}
    # 局域网无凭据：4401 关闭
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with _client(app, "192.168.1.5").websocket_connect("/ws"):
            pass
    assert exc_info.value.code == 4401
    # 公网：4403 关闭
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with _client(app, "8.8.8.8").websocket_connect("/ws"):
            pass
    assert exc_info.value.code == 4403


# ── 登录失败限速（同一 IP 5 次失败 → 60s 内 429，成功清零）────────────────

def test_login_rate_limit_locks_after_max_failures(monkeypatch):
    app = _make_app(monkeypatch, PASSWORD)
    client = _client(app, "192.168.1.5")
    for _ in range(routes_auth.LOGIN_MAX_FAILURES):
        resp = client.post("/api/auth/login", json={"password": "bad"})
        assert resp.status_code == 403
    # 到达上限后：窗口期内一律 429，即使密码正确
    assert client.post("/api/auth/login", json={"password": "bad"}).status_code == 429
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 429


def test_login_success_resets_failure_counter(monkeypatch):
    app = _make_app(monkeypatch, PASSWORD)
    client = _client(app, "192.168.1.5")
    for _ in range(routes_auth.LOGIN_MAX_FAILURES - 1):
        assert client.post("/api/auth/login", json={"password": "bad"}).status_code == 403
    # 成功登录清零计数
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
    # 重新计数：连续 N-1 次失败仍不触发限速
    for _ in range(routes_auth.LOGIN_MAX_FAILURES - 1):
        assert client.post("/api/auth/login", json={"password": "bad"}).status_code == 403


def test_login_rate_limit_is_per_ip(monkeypatch):
    app = _make_app(monkeypatch, PASSWORD)
    locked = _client(app, "192.168.1.5")
    other = _client(app, "10.0.0.9")
    for _ in range(routes_auth.LOGIN_MAX_FAILURES):
        locked.post("/api/auth/login", json={"password": "bad"})
    assert locked.post("/api/auth/login", json={"password": "bad"}).status_code == 429
    # 其他 IP 不受影响
    assert other.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200


# ── C1：启动入口必须显式关闭 uvicorn proxy_headers ────────────────────────

def _run_call_proxy_headers_value(path):
    """在指定文件里找 .run(...) 调用的 proxy_headers 关键字常量值；没有则返回 None。"""
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "run"):
            continue
        for kw in node.keywords:
            if kw.arg == "proxy_headers" and isinstance(kw.value, ast.Constant):
                return kw.value.value
    return None


def test_launcher_disables_uvicorn_proxy_headers():
    """uvicorn 默认 proxy_headers=True 会信任 127.0.0.1 的 X-Forwarded-For 并
    改写 scope["client"]——同机透传式反代下可伪造 127.0.0.1 免密绕过访问控制。
    两个启动入口都必须显式 proxy_headers=False。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for entry in ("launcher.py", "gui_app.py"):
        assert _run_call_proxy_headers_value(os.path.join(root, entry)) is False, \
            f"{entry} 的 uvicorn.run 缺少 proxy_headers=False"


def test_uvicorn_proxy_headers_default_is_true():
    """Config 层佐证：uvicorn 的 proxy_headers 默认确为 True（即默认不信任
    该假设一旦改变，入口处的显式 False 才可考虑移除）。"""
    import inspect
    import uvicorn
    default = inspect.signature(uvicorn.Config.__init__).parameters["proxy_headers"].default
    assert default is True


# ── I1：真实装配（api.server:app）安全不变量 ─────────────────────────────

@pytest.fixture
def real_server_app():
    """导入真实 api.server（启动副作用与 tests/test_subagent_locks.py 相同），
    并在用例结束后复原 os.environ——api.server 导入期会 load_dotenv(data/.env)
    把真实密钥写进环境，不复原会污染字母序靠后的用例（如 test_core）。"""
    saved_env = dict(os.environ)
    import api.server as srv
    yield srv
    os.environ.clear()
    os.environ.update(saved_env)


def test_real_app_access_control_is_outermost_middleware(real_server_app):
    """访问控制必须是真实 app 的最外层用户中间件（add_middleware 插入
    user_middleware[0]），否则存在被其他中间件/挂载绕过的风险。"""
    srv = real_server_app
    assert srv.app.user_middleware, "真实 app 未注册任何用户中间件"
    assert srv.app.user_middleware[0].cls is AccessControlMiddleware


def test_real_app_enforces_access_control(real_server_app):
    """真实装配端到端：公网 IP 对 API 与 /static 挂载一律 403；本机 200。
    （不断言局域网行为——它取决于部署机 config.json 是否设了密码。）"""
    srv = real_server_app
    public = TestClient(srv.app, client=("8.8.8.8", 50000))
    assert public.get("/api/version").status_code == 403
    assert public.get("/static/icon_rounded.png").status_code == 403
    local = TestClient(srv.app, client=("127.0.0.1", 50000))
    assert local.get("/api/version").status_code == 200
    assert local.get("/static/icon_rounded.png").status_code == 200
