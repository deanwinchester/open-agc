# -*- coding: utf-8 -*-
"""中央服务器账号体系（zxs 产品线）测试：

- core.server_auth：token 持久化 / 登录态判定 / 临期刷新 / 登出
- AccessControlMiddleware 的 server 模式：账号登录取代实例密码
- LLMClient 网关厂商（use_server_token）的 token 注入与未登录拦截

全部用 mock/临时文件，不依赖真实中央服务器。
"""
import base64
import json
import os
import time

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import api.routes.routes_auth as routes_auth
from core import server_auth
from core.access_control import AccessControlMiddleware


def _fake_token(exp: int) -> str:
    """伪造自研 JWT 格式：base64url(payload).sig（客户端只解析 exp）。"""
    payload = json.dumps({"sub": "u1", "name": "测试", "exp": exp}).encode()
    b64 = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{b64}.fakesig"


@pytest.fixture(autouse=True)
def _isolate_auth_file(tmp_path, monkeypatch):
    """token 文件指向临时目录 + 清缓存 + 清登录限速计数。"""
    monkeypatch.setattr(server_auth, "AUTH_FILE", str(tmp_path / "server_auth.json"))
    monkeypatch.setattr(server_auth, "_cache", None)
    monkeypatch.setattr(server_auth, "_cache_mtime", 0.0)
    routes_auth._login_failures.clear()
    yield


def _write_token(exp_offset: int = 86400, username: str = "13800000000"):
    server_auth._save({
        "token": _fake_token(int(time.time()) + exp_offset),
        "username": username,
        "display_name": "测试用户",
        "exp": int(time.time()) + exp_offset,
    })


# ── server_auth 单元 ─────────────────────────────────────────────────────

def test_decode_exp():
    token = _fake_token(1893456000)
    assert server_auth._decode_exp(token) == 1893456000
    assert server_auth._decode_exp("garbage") == 0


def test_status_logged_in_and_out(monkeypatch):
    monkeypatch.setattr(server_auth, "server_url", lambda: "http://x")
    assert server_auth.status()["logged_in"] is False
    _write_token()
    st = server_auth.status()
    assert st["logged_in"] is True
    assert st["display_name"] == "测试用户"
    # 过期 token 视为未登录
    _write_token(exp_offset=-10)
    assert server_auth.is_logged_in() is False


def test_login_saves_token(monkeypatch):
    monkeypatch.setattr(server_auth, "server_url", lambda: "http://x")
    token = _fake_token(int(time.time()) + 86400)
    monkeypatch.setattr(server_auth, "_post", lambda *a, **k: {
        "token": token, "username": "13800000000", "display_name": "张三"})
    info = server_auth.login("13800000000", "pw")
    assert info["display_name"] == "张三"
    assert server_auth.is_logged_in()


def test_refresh_when_near_expiry(monkeypatch):
    monkeypatch.setattr(server_auth, "server_url", lambda: "http://x")
    # 剩余 10 天（<30 天阈值）→ get_token 触发 refresh
    _write_token(exp_offset=10 * 86400)
    new_token = _fake_token(int(time.time()) + 180 * 86400)
    calls = []
    monkeypatch.setattr(server_auth, "_post",
                        lambda path, **k: calls.append(path) or {"token": new_token})
    tok = server_auth.get_token()
    assert tok == new_token
    assert "/api/auth/refresh" in calls


def test_lan_client_token_remains_valid_across_refresh(monkeypatch):
    app = _make_server_mode_app(monkeypatch)
    _write_token(exp_offset=10 * 86400)
    old_token = server_auth._load()["token"]
    new_token = _fake_token(int(time.time()) + 180 * 86400)
    monkeypatch.setattr(server_auth, "_post", lambda *a, **k: {"token": new_token})
    assert server_auth.refresh_if_needed()
    client = TestClient(app, client=("192.168.1.5", 50000))
    assert client.get("/api/ping", headers={
        "Authorization": f"Bearer {old_token}"}).status_code == 200
    assert client.get("/api/ping", headers={
        "Authorization": f"Bearer {new_token}"}).status_code == 200


def test_no_refresh_when_fresh(monkeypatch):
    monkeypatch.setattr(server_auth, "server_url", lambda: "http://x")
    _write_token(exp_offset=170 * 86400)
    monkeypatch.setattr(server_auth, "_post",
                        lambda *a, **k: pytest.fail("不应调用 refresh"))
    assert server_auth.get_token() is not None


def test_logout_clears_state(monkeypatch):
    monkeypatch.setattr(server_auth, "server_url", lambda: "http://x")
    _write_token()
    monkeypatch.setattr(server_auth, "_post", lambda *a, **k: {"ok": True})
    server_auth.logout()
    assert not server_auth.is_logged_in()


# ── 中间件 server 模式 ───────────────────────────────────────────────────

def _make_server_mode_app(monkeypatch):
    monkeypatch.setattr(routes_auth, "load_config",
                        lambda: {"server_url": "http://x"})
    monkeypatch.setattr(server_auth, "server_url", lambda: "http://x")
    app = FastAPI()

    @app.get("/api/ping")
    def ping():
        return {"ok": True}

    @app.get("/index.html")
    def index():
        return {"html": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.close()

    app.add_middleware(AccessControlMiddleware,
                       config_loader=lambda: {"server_url": "http://x"})
    app.include_router(routes_auth.router)
    return app


def test_server_mode_blocks_api_when_logged_out(monkeypatch):
    app = _make_server_mode_app(monkeypatch)
    resp = TestClient(app, client=("127.0.0.1", 50000)).get("/api/ping")
    assert resp.status_code == 401
    assert resp.json().get("code") == "server_login_required"


def test_server_mode_allows_after_login(monkeypatch):
    app = _make_server_mode_app(monkeypatch)
    _write_token()
    client = TestClient(app, client=("127.0.0.1", 50000))
    assert client.get("/api/ping").status_code == 200


def test_server_mode_exempt_and_static(monkeypatch):
    app = _make_server_mode_app(monkeypatch)
    client = TestClient(app, client=("192.168.1.5", 50000))
    # 局域网免实例密码；登录/状态端点豁免；静态资源放行（SPA 要渲染登录页）
    assert client.get("/api/auth/server-status").status_code == 200
    assert client.get("/index.html").status_code == 200
    assert client.get("/api/ping").status_code == 401
    # WS 未登录关闭 4401
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws"):
            pass
    assert exc.value.code == 4401


def test_server_mode_public_still_403(monkeypatch):
    app = _make_server_mode_app(monkeypatch)
    _write_token()
    resp = TestClient(app, client=("8.8.8.8", 50000)).get("/api/ping")
    assert resp.status_code == 403


def test_server_mode_lan_must_present_saved_token(monkeypatch):
    app = _make_server_mode_app(monkeypatch)
    _write_token()
    token = server_auth.get_token()
    anonymous = TestClient(app, client=("192.168.1.5", 50000))
    # 进程已登录不再等于每个 LAN 客户端已认证。
    assert anonymous.get("/api/ping").status_code == 401
    assert anonymous.get("/api/ping", headers={
        "Authorization": "Bearer wrong"}).status_code == 401
    assert anonymous.get("/api/ping", headers={
        "Authorization": f"Bearer {token}"}).status_code == 200
    assert anonymous.get(f"/api/ping?token={token}").status_code == 200


def test_server_login_returns_token_to_lan_client(monkeypatch):
    app = _make_server_mode_app(monkeypatch)
    token = _fake_token(int(time.time()) + 86400)
    monkeypatch.setattr(server_auth, "_post", lambda *a, **k: {
        "token": token, "username": "13800000000", "display_name": "测试用户"})
    resp = TestClient(app, client=("192.168.1.5", 50000)).post(
        "/api/auth/server-login",
        headers={"X-OpenAGC-Client": "1"},
        json={"username": "13800000000", "password": "pw"})
    assert resp.status_code == 200
    assert resp.json()["token"] == token
    assert resp.json()["layer"] == "lan"


def test_ws_bootstrap_token_is_loopback_only(monkeypatch):
    app = _make_server_mode_app(monkeypatch)
    _write_token()
    from api import state
    local = TestClient(app, client=("127.0.0.1", 50000))
    assert local.get("/api/bootstrap/ws-token").json()["token"] == state._WS_TOKEN
    assert local.get("/api/bootstrap/ws-token", headers={
        "Host": "evil.example"}).status_code == 403
    lan = TestClient(app, client=("192.168.1.5", 50000))
    # 即使本机进程已登录，匿名 LAN 也不能领取 WS 会话令牌。
    assert lan.get("/api/bootstrap/ws-token").status_code == 401


# ── LLMClient 网关 token 注入 ─────────────────────────────────────────────

def test_llm_gateway_injects_server_token(monkeypatch):
    import core.llm_client as llm_mod
    from core.llm_client import LLMClient
    monkeypatch.setattr(llm_mod, "load_config", lambda: {
        "custom_providers": [{
            "name": "zxs_gw", "base_url": "http://gw/v1", "api_key": "",
            "use_server_token": True, "models": ["M"]}]})
    monkeypatch.setattr(server_auth, "server_url", lambda: "http://x")
    _write_token()
    client = LLMClient()
    kwargs = client._build_model_kwargs("zxs_gw/M", [{"role": "user", "content": "hi"}])
    assert kwargs["api_key"] != "sk-no-key-required"
    assert kwargs["api_key"].endswith(".fakesig")


def test_llm_gateway_requires_login(monkeypatch):
    import core.llm_client as llm_mod
    from core.llm_client import LLMClient, ServerLoginRequiredError
    monkeypatch.setattr(llm_mod, "load_config", lambda: {
        "custom_providers": [{
            "name": "zxs_gw", "base_url": "http://gw/v1", "api_key": "",
            "use_server_token": True, "models": ["M"]}]})
    client = LLMClient()
    with pytest.raises(ServerLoginRequiredError):
        client._build_model_kwargs("zxs_gw/M", [{"role": "user", "content": "hi"}])


def test_llm_normal_custom_provider_unaffected(monkeypatch):
    import core.llm_client as llm_mod
    from core.llm_client import LLMClient
    monkeypatch.setattr(llm_mod, "load_config", lambda: {
        "custom_providers": [{
            "name": "zxs", "base_url": "http://direct/v1", "api_key": "k",
            "models": ["M"]}]})
    client = LLMClient()
    kwargs = client._build_model_kwargs("zxs/M", [{"role": "user", "content": "hi"}])
    assert kwargs["api_key"] == "k"


# ── 用户自助（资料/密码）─────────────────────────────────────────────────

def test_change_password_validates_length(monkeypatch):
    monkeypatch.setattr(server_auth, "server_url", lambda: "http://x")
    _write_token()
    with pytest.raises(server_auth.ServerAuthError):
        server_auth.change_password("old", "123")
    monkeypatch.setattr(server_auth, "_post", lambda *a, **k: {"status": "success"})
    server_auth.change_password("old", "newpass123")


def test_self_service_requires_login(monkeypatch):
    monkeypatch.setattr(server_auth, "server_url", lambda: "http://x")
    with pytest.raises(server_auth.ServerAuthError):
        server_auth.fetch_profile()
    with pytest.raises(server_auth.ServerAuthError):
        server_auth.change_password("a", "bbbbbb")
