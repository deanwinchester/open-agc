"""
request_secret popup chain tests (Task 2 — 入口 A：agent 弹窗收集自动入库).

Covers:
- RequestSecretTool raises SandboxBlocked(category='secret') carrying purpose/name
  (path keeps the raw suggested name — sandbox_dir='permission' skips abspath).
- Full popup chain: agent._handle_sandbox_blocked waits; a simulated ws form
  submit (via the REAL api.state.resolve_sandbox_wait passthrough) delivers the
  form fields; the agent upserts into the vault and returns None (caller retries);
  the retried tool returns the confirmation text with NO plaintext.
- Auto-generated name when neither the LLM nor the user provides one.
- Overwrite semantics: same name re-submitted overwrites (popup = confirmation),
  created_at preserved, and an 'overwritten' log line is printed.
- Deny / missing password / invalid name: no vault write, clear error text.
- After the chain, {{secret:name.field}} placeholders substitute at execution
  time while unknown names stay untouched.
- resolve_sandbox_wait only forwards secret fields for category='secret' waits.

All tests use an isolated vault (OPEN_AGC_DATA_DIR -> tmp) and stub agent
instances (OpenAGCAgent.__new__) — no LLM, no network, no real websocket.
"""
import os
import re
import sys
import threading
import time

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.secrets import get_secret, list_secrets, substitute_refs  # noqa: E402
from tools.base import SandboxBlocked  # noqa: E402
from tools.request_secret import RequestSecretTool, confirmation_text  # noqa: E402

TEST_SID = 776001
PASSWORD = "Sup3rSecret"


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    """Isolated vault: OPEN_AGC_DATA_DIR redirects data/secrets.json to tmp."""
    monkeypatch.setenv("OPEN_AGC_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_sandbox_waits():
    from api.state import _sandbox_waits
    _sandbox_waits.clear()
    yield
    _sandbox_waits.clear()


def _make_agent():
    from agent.agent import OpenAGCAgent
    agent = OpenAGCAgent.__new__(OpenAGCAgent)
    agent.session_id = TEST_SID
    agent.is_interrupted = False
    return agent


def _raise_secret(purpose="连接生产 MongoDB", name="prod_db"):
    """Run the tool once and capture the SandboxBlocked it raises."""
    tool = RequestSecretTool()
    with pytest.raises(SandboxBlocked) as exc_info:
        tool.execute(purpose=purpose, name=name)
    return exc_info.value


def _respond_via_ws(msg: dict, timeout: float = 10.0):
    """Simulate the ws.py sandbox_response path: wait for the agent to register
    the wait entry, then resolve it through the real shared passthrough."""
    from api.state import _sandbox_waits, resolve_sandbox_wait

    deadline = time.time() + timeout
    while time.time() < deadline:
        seen = {}
        for entry in list(_sandbox_waits.values()):
            if isinstance(entry, dict) and entry.get("request_id"):
                seen.setdefault(entry["request_id"], entry)
        if seen:
            wait = next(iter(seen.values()))
            resolve_sandbox_wait(wait, msg)
            return wait
        time.sleep(0.02)
    raise AssertionError("agent never registered a sandbox wait")


def _run_popup_chain(agent, sb, ws_msg: dict):
    """Drive agent._handle_sandbox_blocked with a concurrent ws-style response."""
    responder = threading.Thread(target=_respond_via_ws, args=(ws_msg,), daemon=True)
    responder.start()
    result = agent._handle_sandbox_blocked(sb, "request_secret",
                                           {"purpose": "x"}, progress_callback=None)
    responder.join(timeout=15)
    assert not responder.is_alive(), "ws responder thread stuck"
    return result


# ── Tool raising behavior ──

def test_tool_raises_sandbox_blocked_secret(vault):
    sb = _raise_secret(purpose="连接生产 MongoDB", name="prod_db")
    assert sb.category == "secret"
    assert sb.sandbox_dir == "permission"
    assert sb.tool_name == "request_secret"
    assert sb.description == "连接生产 MongoDB"
    # Suggested name passes through raw (no abspath rewriting)
    assert sb.path == "prod_db"


def test_tool_requires_purpose(vault):
    tool = RequestSecretTool()
    out = tool.execute(purpose="  ")
    assert out.startswith("Error")
    assert list_secrets() == []


def test_tool_confirms_when_name_already_in_vault(vault):
    from core.secrets import upsert_secret
    upsert_secret(name="prod_db", type="mongodb", host="db.internal",
                  username="root", password=PASSWORD)
    tool = RequestSecretTool()
    out = tool.execute(purpose="连接生产 MongoDB", name="prod_db")
    assert PASSWORD not in out
    assert "{{secret:prod_db}}" in out


# ── Full popup chain ──

def test_popup_chain_saves_and_confirms_without_plaintext(vault):
    agent = _make_agent()
    sb = _raise_secret()
    result = _run_popup_chain(agent, sb, {
        "action": "approve_once", "path": "",
        "password": PASSWORD,
        "secret_name": "prod_db", "secret_type": "mongodb",
        "host": "db.internal", "username": "root", "note": "prod master",
    })

    # Approved -> None (caller retries the tool)
    assert result is None

    # Vault now holds the record (server-side write)
    entry = get_secret("prod_db")
    assert entry is not None
    assert entry["password"] == PASSWORD
    assert entry["type"] == "mongodb"
    assert entry["host"] == "db.internal"
    assert entry["username"] == "root"
    assert entry["note"] == "prod master"

    # Retried tool returns the reference text — never the plaintext
    text = RequestSecretTool().execute(purpose="连接生产 MongoDB", name="prod_db",
                                       _agent_context=agent)
    assert PASSWORD not in text
    assert "已保存为 {{secret:prod_db}}（mongodb@db.internal）。" in text
    assert "{{secret:prod_db.password}}" in text
    assert "{{secret:prod_db.uri}}" in text
    # One-shot marker consumed
    assert getattr(agent, "_last_saved_secret", None) is None


def test_popup_chain_auto_generates_name(vault):
    agent = _make_agent()
    sb = _raise_secret(name="")  # LLM suggested nothing
    result = _run_popup_chain(agent, sb, {
        "action": "approve_once", "path": "",
        "password": PASSWORD,
        "secret_name": "", "secret_type": "api_key",
        "host": "", "username": "", "note": "",
    })
    assert result is None

    saved = list_secrets()
    assert len(saved) == 1
    auto_name = saved[0]["name"]
    assert re.fullmatch(r"secret_\d+", auto_name)

    text = RequestSecretTool().execute(purpose="p", name="", _agent_context=agent)
    assert PASSWORD not in text
    assert f"{{{{secret:{auto_name}}}}}" in text
    assert "api_key@-" in text  # empty host renders as '-'


def test_popup_chain_overwrite_same_name(vault, capsys):
    from core.secrets import upsert_secret
    upsert_secret(name="prod_db", type="mongodb", host="old.host",
                  username="root", password="OldPass123", note="old")
    created_at = get_secret("prod_db")["created_at"]

    agent = _make_agent()
    # LLM 不知道库中已有 prod_db（或未指定名称）→ 弹窗；用户在表单里填入同名 → 覆盖
    sb = _raise_secret(name="")
    result = _run_popup_chain(agent, sb, {
        "action": "approve_once", "path": "",
        "password": PASSWORD,
        "secret_name": "prod_db", "secret_type": "mysql",
        "host": "db.internal", "username": "admin", "note": "new",
    })
    assert result is None

    entry = get_secret("prod_db")
    # The popup submission itself is the user's confirmation -> overwrite
    assert entry["password"] == PASSWORD
    assert entry["type"] == "mysql"
    assert entry["host"] == "db.internal"
    assert entry["username"] == "admin"
    assert entry["created_at"] == created_at  # preserved across overwrite
    assert list_secrets().__len__() == 1

    # Overwrite is logged (name/type/host only — no plaintext)
    out = capsys.readouterr().out
    assert "overwritten" in out
    assert "prod_db" in out
    assert PASSWORD not in out


def test_popup_chain_deny_writes_nothing(vault):
    agent = _make_agent()
    sb = _raise_secret()
    result = _run_popup_chain(agent, sb, {"action": "deny_once", "path": ""})
    assert result is not None
    assert "取消" in result
    assert list_secrets() == []
    assert getattr(agent, "_last_saved_secret", None) is None


def test_popup_chain_missing_password_writes_nothing(vault):
    agent = _make_agent()
    sb = _raise_secret()
    result = _run_popup_chain(agent, sb, {
        "action": "approve_once", "path": "",
        "secret_name": "prod_db", "secret_type": "generic",
        "host": "", "username": "root", "note": "",
    })
    assert result is not None
    assert "必填" in result
    assert get_secret("prod_db") is None


def test_popup_chain_invalid_name_rejected(vault):
    agent = _make_agent()
    sb = _raise_secret()
    result = _run_popup_chain(agent, sb, {
        "action": "approve_once", "path": "",
        "password": PASSWORD,
        "secret_name": "bad name!", "secret_type": "generic",
        "host": "", "username": "", "note": "",
    })
    assert result is not None
    assert "未保存" in result
    assert list_secrets() == []


# ── Placeholder substitution after the chain ──

def test_placeholders_substitute_saved_and_preserve_unknown(vault):
    agent = _make_agent()
    sb = _raise_secret()
    _run_popup_chain(agent, sb, {
        "action": "approve_once", "path": "",
        "password": PASSWORD,
        "secret_name": "prod_db", "secret_type": "mongodb",
        "host": "db.internal", "username": "root", "note": "",
    })

    # Saved refs resolve at execution time
    assert substitute_refs("pw={{secret:prod_db.password}}") == f"pw={PASSWORD}"
    assert substitute_refs("{{secret:prod_db.host}}") == "db.internal"
    # Unknown names are left untouched
    assert substitute_refs("{{secret:ghost.password}}") == "{{secret:ghost.password}}"
    mixed = substitute_refs("{{secret:ghost.uri}} {{secret:prod_db.username}}")
    assert mixed == "{{secret:ghost.uri}} root"


# ── resolve_sandbox_wait passthrough (ws.py / /api/sandbox/approve shared path) ──

def _wait_entry(category: str):
    return {
        "event": threading.Event(),
        "result": {"action": "timeout"},
        "session_id": TEST_SID,
        "request_id": "rid-1",
        "payload": {"path": "", "tool_name": "request_secret",
                    "block_type": "permission", "description": "p",
                    "category": category},
    }


def test_resolve_sandbox_wait_forwards_secret_fields_only_for_secret():
    from api.state import resolve_sandbox_wait

    wait = _wait_entry("secret")
    resolve_sandbox_wait(wait, {
        "action": "approve_once", "path": "", "password": PASSWORD,
        "secret_name": "prod_db", "secret_type": "mongodb",
        "host": "db.internal", "username": "root", "note": "n",
    })
    assert wait["event"].is_set()
    res = wait["result"]
    assert res["action"] == "approve_once"
    assert res["password"] == PASSWORD
    assert res["secret_name"] == "prod_db"
    assert res["secret_type"] == "mongodb"
    assert res["host"] == "db.internal"
    assert res["username"] == "root"
    assert res["note"] == "n"

    # Non-secret waits must NOT pick up secret form fields
    wait2 = _wait_entry("sudo")
    resolve_sandbox_wait(wait2, {
        "action": "approve_once", "path": "", "password": "pw",
        "secret_name": "prod_db", "secret_type": "mongodb", "host": "h",
    })
    res2 = wait2["result"]
    assert res2["password"] == "pw"  # sudo password path unchanged
    assert "secret_name" not in res2
    assert "secret_type" not in res2
    assert "host" not in res2


def test_confirmation_text_format():
    text = confirmation_text("prod_db", "mongodb", "db.internal")
    assert text == ("已保存为 {{secret:prod_db}}（mongodb@db.internal）。"
                    "请用 {{secret:prod_db.password}} 或 {{secret:prod_db.uri}} 引用，"
                    "不要在上下文中包含明文。")
