# -*- coding: utf-8 -*-
"""恶意网页隔离、WS 会话令牌、LAN 账号绑定与沙箱审批绑定回归测试。"""
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import api.state as state
import api.ws as ws_module
from core.access_control import AccessControlMiddleware


LOCAL_HEADERS = {"Host": "localhost:8765"}
CSRF_HEADERS = {"X-OpenAGC-Client": "1"}


@pytest.fixture
def ws_app(monkeypatch):
    token = "test-ws-session-token"
    monkeypatch.setattr(state, "_WS_TOKEN", token, raising=False)
    monkeypatch.setattr(ws_module, "_load_session_history", lambda *a, **k: [])
    monkeypatch.setattr(ws_module, "load_config", lambda: {})
    state._sandbox_waits.clear()
    state._pending_sandbox_approvals.clear()
    app = FastAPI()
    app.websocket("/ws")(ws_module.websocket_endpoint)
    yield app, token
    state._sandbox_waits.clear()
    state._pending_sandbox_approvals.clear()
    state.connected_websockets.clear()


def _denied(client, url, headers):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(url, headers=headers):
            pass


def test_ws_rejects_malicious_origin_and_non_loopback_host(ws_app):
    app, token = ws_app
    client = TestClient(app, client=("127.0.0.1", 50000))
    _denied(client, f"/ws?token={token}", {
        **LOCAL_HEADERS, "Origin": "https://evil.example"})
    _denied(client, f"/ws?token={token}", {
        "Host": "open-agc.evil.example"})


def test_ws_requires_session_token_and_allows_local_non_browser(ws_app):
    app, token = ws_app
    client = TestClient(app, client=("127.0.0.1", 50000))
    _denied(client, "/ws?token=wrong", LOCAL_HEADERS)
    _denied(client, "/ws", LOCAL_HEADERS)
    with client.websocket_connect(f"/ws?token={token}", headers=LOCAL_HEADERS):
        pass


def test_ws_allows_desktop_and_loopback_browser_origins(ws_app):
    app, token = ws_app
    client = TestClient(app, client=("127.0.0.1", 50000))
    for origin in ("http://localhost:5173", "https://127.0.0.1:8765",
                   "file://", "app://", "tauri://"):
        with client.websocket_connect(f"/ws?token={token}", headers={
                **LOCAL_HEADERS, "Origin": origin}):
            pass


def test_ws_server_mode_accepts_account_jwt(monkeypatch, ws_app):
    """中央账号模式下本机也接受账号 JWT（实证：登录后实时步骤全断）。"""
    app, _ = ws_app
    from core import server_auth
    monkeypatch.setattr(ws_module, "load_config",
                        lambda: {"server_url": "http://172.16.100.84:8080"})
    monkeypatch.setattr(server_auth, "get_token", lambda: "account-jwt")
    client = TestClient(app, client=("127.0.0.1", 50000))
    with client.websocket_connect("/ws?token=account-jwt", headers=LOCAL_HEADERS):
        pass


def _wait_entry():
    return {
        "event": threading.Event(),
        "result": {"action": "timeout"},
        "session_id": 7,
        "task_id": 42,
        "request_id": "req-real",
        "payload": {
            "path": "/expected/path",
            "tool_name": "shell",
            "block_type": "path",
            "description": "",
            "category": "",
        },
    }


def _sandbox_message(**overrides):
    msg = {
        "type": "sandbox_response",
        "session_id": 7,
        "task_id": 42,
        "request_id": "req-real",
        "action": "approve_once",
        "path": "/expected/path",
        "tool_name": "shell",
    }
    msg.update(overrides)
    return msg


def test_sandbox_response_requires_request_id(ws_app):
    app, token = ws_app
    wait = _wait_entry()
    state._sandbox_waits["req-real"] = wait
    state._sandbox_waits[7] = wait  # 旧版 session 兼容键：不得再用于审批
    with TestClient(app, client=("127.0.0.1", 50000)).websocket_connect(f"/ws?token={token}", headers=LOCAL_HEADERS) as sock:
        msg = _sandbox_message()
        del msg["request_id"]
        sock.send_json(msg)
    assert not wait["event"].is_set()


def test_sandbox_response_rejects_forged_path(ws_app):
    app, token = ws_app
    wait = _wait_entry()
    state._sandbox_waits["req-real"] = wait
    with TestClient(app, client=("127.0.0.1", 50000)).websocket_connect(f"/ws?token={token}", headers=LOCAL_HEADERS) as sock:
        sock.send_json(_sandbox_message(path="/attacker/controlled"))
    assert not wait["event"].is_set()


def test_sandbox_response_rejects_unknown_request_without_late_approval(ws_app):
    app, token = ws_app
    with TestClient(app, client=("127.0.0.1", 50000)).websocket_connect(f"/ws?token={token}", headers=LOCAL_HEADERS) as sock:
        sock.send_json(_sandbox_message(request_id="expired-or-forged"))
    assert not state._pending_sandbox_approvals


def test_sandbox_response_accepts_exact_binding(ws_app):
    app, token = ws_app
    wait = _wait_entry()
    state._sandbox_waits["req-real"] = wait
    with TestClient(app, client=("127.0.0.1", 50000)).websocket_connect(f"/ws?token={token}", headers=LOCAL_HEADERS) as sock:
        sock.send_json(_sandbox_message())
    assert wait["event"].is_set()
    assert wait["result"]["action"] == "approve_once"


def test_sandbox_response_accepts_bound_deny_always(ws_app):
    app, token = ws_app
    wait = _wait_entry()
    state._sandbox_waits["req-real"] = wait
    with TestClient(app, client=("127.0.0.1", 50000)).websocket_connect(f"/ws?token={token}", headers=LOCAL_HEADERS) as sock:
        sock.send_json(_sandbox_message(action="deny_always"))
    assert wait["event"].is_set()
    assert wait["result"]["action"] == "deny_always"


def test_http_sandbox_approval_uses_same_exact_binding():
    from api.routes.routes_plugins import router
    app = FastAPI()
    app.include_router(router)
    wait = _wait_entry()
    state._sandbox_waits["req-real"] = wait
    state._sandbox_waits[7] = wait
    client = TestClient(app)
    assert client.post("/api/sandbox/approve", json={
        "session_id": 7, "action": "approve_once", "path": "/expected/path",
    }, headers={**LOCAL_HEADERS, **CSRF_HEADERS}).status_code == 403
    assert not wait["event"].is_set()
    assert client.post("/api/sandbox/approve", json=_sandbox_message(),
                       headers={**LOCAL_HEADERS, **CSRF_HEADERS}).status_code == 200
    assert wait["event"].is_set()
    state._sandbox_waits.clear()


def _write_app():
    app = FastAPI()

    @app.post("/api/write")
    async def post_write():
        return {"ok": True}

    @app.put("/api/write")
    async def put_write():
        return {"ok": True}

    @app.delete("/api/write")
    async def delete_write():
        return {"ok": True}

    app.add_middleware(AccessControlMiddleware, config_loader=lambda: {})
    return app


@pytest.mark.parametrize("method", ["post", "put", "delete"])
def test_write_requests_require_custom_client_header(method):
    client = TestClient(_write_app(), client=("127.0.0.1", 50000),
                        headers=LOCAL_HEADERS)
    assert getattr(client, method)("/api/write").status_code == 403
    assert getattr(client, method)("/api/write", headers=CSRF_HEADERS).status_code == 200


def test_local_http_rejects_dns_rebinding_host_even_with_csrf_header():
    client = TestClient(_write_app(), client=("127.0.0.1", 50000))
    assert client.post("/api/write", headers={
        "Host": "evil.example", **CSRF_HEADERS,
    }).status_code == 403
