"""
Tests for the secrets vault (Task 1):
- storage roundtrip (data/secrets.json, atomic write)
- masked view / API endpoints never expose the password (recursive assertion)
- substitute_refs: username/password/host/uri/note, unknown refs preserved
- mask_secrets: password & full credential URI -> ***, empty values skipped
- shell end-to-end: stubbed Popen proves the real value only reaches the
  child-process command while the returned result is masked
- python_repl end-to-end: same guarantee for executed code
"""
import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core import secrets as secrets_mod
from core.secrets import (
    build_uri,
    delete_secret,
    get_secret,
    has_secret_ref,
    list_secrets,
    mask_secrets,
    substitute_refs,
    upsert_secret,
)


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    """Isolated vault: OPEN_AGC_DATA_DIR redirects data/secrets.json to tmp."""
    monkeypatch.setenv("OPEN_AGC_DATA_DIR", str(tmp_path))
    return tmp_path


def _seed(**overrides):
    kwargs = dict(
        name="mydb", type="mongodb", host="db.internal", port="27017",
        username="root", password="Sup3rSecret", note="prod master",
    )
    kwargs.update(overrides)
    return upsert_secret(**kwargs)


def _assert_no_password(obj, password):
    """Recursively assert no 'password' key and no password value anywhere."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            assert key != "password", f"'password' key leaked: {obj!r}"
            _assert_no_password(value, password)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _assert_no_password(value, password)
    elif isinstance(obj, str):
        assert password not in obj, f"password value leaked: {obj!r}"


# ── Storage roundtrip ──

def test_storage_roundtrip(vault):
    entry = _seed()
    assert entry["name"] == "mydb"
    assert entry["username_masked"] == "r***"

    # File on disk holds the plaintext (at-rest, local-only by design)
    path = os.path.join(str(vault), "data", "secrets.json")
    assert os.path.exists(path)
    with open(path, "r", encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk["mydb"]["password"] == "Sup3rSecret"
    assert on_disk["mydb"]["type"] == "mongodb"

    # Internal accessor returns plaintext
    full = get_secret("mydb")
    assert full["password"] == "Sup3rSecret"
    assert full["username"] == "root"
    assert get_secret("missing") is None

    # Update preserves created_at, overwrites fields
    created = full["created_at"]
    upsert_secret(name="mydb", type="mongodb", host="db2.internal", port="27018",
                  username="admin", password="NewPass456", note="")
    updated = get_secret("mydb")
    assert updated["password"] == "NewPass456"
    assert updated["host"] == "db2.internal"
    assert updated["created_at"] == created

    # Delete
    assert delete_secret("mydb") is True
    assert get_secret("mydb") is None
    assert delete_secret("mydb") is False


def test_upsert_rejects_invalid_name(vault):
    for bad in ["", "has space", "slash/name", "dot.name", "中文名"]:
        with pytest.raises(ValueError):
            upsert_secret(name=bad, password="x")


# ── Masked view contains no password ──

def test_masked_view_has_no_password(vault):
    _seed()
    view = list_secrets()
    assert len(view) == 1
    item = view[0]
    assert set(item.keys()) == {"name", "type", "host", "database", "username_masked", "note", "created_at"}
    _assert_no_password(view, "Sup3rSecret")


# ── build_uri ──

def test_build_uri(vault):
    _seed()
    assert build_uri("mydb") == "mongodb://root:Sup3rSecret@db.internal:27017/"
    assert build_uri("missing") == ""


def test_build_uri_percent_encodes_special_chars(vault):
    _seed(password="p@ss/w:d", username="us er")
    uri = build_uri("mydb")
    assert uri == "mongodb://us%20er:p%40ss%2Fw%3Ad@db.internal:27017/"


def test_build_uri_variants(vault):
    upsert_secret(name="pg", type="postgres", host="127.0.0.1:5432",
                  username="bob", password="pw")
    # host already carries the port -> not duplicated
    assert build_uri("pg") == "postgresql://bob:pw@127.0.0.1:5432/"
    upsert_secret(name="cache", type="redis", host="localhost", port="6379",
                  username="", password="onlypass")
    # password-only -> redis style :password@host
    assert build_uri("cache") == "redis://:onlypass@localhost:6379/"


# ── substitute_refs / has_secret_ref ──

def test_substitute_all_fields(vault):
    _seed()
    text = ("u={{secret:mydb.username}} p={{secret:mydb.password}} "
            "h={{secret:mydb.host}} n={{secret:mydb.note}}")
    out = substitute_refs(text)
    assert out == "u=root p=Sup3rSecret h=db.internal n=prod master"
    assert substitute_refs("{{secret:mydb.uri}}") == "mongodb://root:Sup3rSecret@db.internal:27017/"


def test_substitute_unknown_refs_preserved(vault):
    _seed()
    text = "{{secret:ghost.password}} {{secret:mydb.bogus}} {{secret:mydb.username}}"
    assert substitute_refs(text) == "{{secret:ghost.password}} {{secret:mydb.bogus}} root"


def test_has_secret_ref(vault):
    assert has_secret_ref("connect {{secret:mydb.uri}} now") is True
    assert has_secret_ref("no refs here") is False
    assert has_secret_ref("") is False


# ── mask_secrets ──

def test_mask_password_and_uri(vault):
    _seed()
    uri = build_uri("mydb")
    assert mask_secrets("login ok: Sup3rSecret") == "login ok: ***"
    assert mask_secrets(f"connected via {uri} done") == "connected via *** done"
    # unrelated text untouched
    assert mask_secrets("nothing to hide") == "nothing to hide"
    assert mask_secrets("") == ""


def test_mask_skips_empty_password(vault):
    upsert_secret(name="nopw", type="generic", host="", username="", password="", note="")
    # Empty password must not blank out text; usernames are not masked by design
    assert mask_secrets("user root on host") == "user root on host"


def test_mask_handles_special_regex_chars(vault):
    _seed(password="p.*+[a](b){2}$^")
    out = mask_secrets("leaked p.*+[a](b){2}$^ here")
    assert out == "leaked *** here"


# ── API endpoints ──

def _make_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes.routes_secrets import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_api_endpoints_never_return_password(vault):
    client = _make_client()

    # Create
    resp = client.post("/api/secrets", json={
        "name": "mydb", "type": "mongodb", "host": "db.internal", "port": "27017",
        "username": "root", "password": "Sup3rSecret", "note": "prod",
    })
    assert resp.status_code == 200, resp.text
    _assert_no_password(resp.json(), "Sup3rSecret")
    assert resp.json()["secret"]["username_masked"] == "r***"

    # List view
    resp = client.get("/api/secrets")
    assert resp.status_code == 200
    _assert_no_password(resp.json(), "Sup3rSecret")
    assert resp.json()["secrets"][0]["name"] == "mydb"

    # For-LLM view
    resp = client.get("/api/secrets/for-llm")
    assert resp.status_code == 200
    _assert_no_password(resp.json(), "Sup3rSecret")

    # Delete
    assert client.delete("/api/secrets/mydb").json() == {"ok": True}
    assert client.get("/api/secrets").json() == {"secrets": []}
    assert client.delete("/api/secrets/mydb").status_code == 404


def test_api_rejects_invalid_name(vault):
    client = _make_client()
    resp = client.post("/api/secrets", json={"name": "bad name!", "password": "x"})
    assert resp.status_code == 400
    assert list_secrets() == []


# ── Shell end-to-end with stubbed Popen ──

def test_shell_substitutes_for_child_and_masks_result(vault, monkeypatch):
    _seed()
    captured = {}

    class _StubPopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            self.returncode = 0
            self.pid = 4242
            self.stdin = None
            out = kwargs.get("stdout")
            if out is not None:
                # Simulate the child process echoing the real credential
                out.write(b"auth ok with Sup3rSecret\n")
                out.flush()

        def poll(self):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr("tools.shell.subprocess.Popen", _StubPopen)
    from tools.shell import ShellTool
    result = ShellTool().execute(command='mongosh "{{secret:mydb.uri}}" --quiet')

    # Real value reaches the child process command only
    assert "Sup3rSecret" in captured["cmd"]
    assert "mongodb://root:Sup3rSecret@db.internal:27017/" in captured["cmd"]
    assert "{{secret:mydb.uri}}" not in captured["cmd"]

    # What comes back (LLM context / frontend / logs) is masked
    assert "Sup3rSecret" not in result
    assert "***" in result
    assert "Exit Code: 0" in result


def test_shell_result_masks_child_output_even_without_refs(vault, monkeypatch):
    """A command with no placeholder can still leak a secret in its output."""
    _seed()

    class _StubPopen:
        def __init__(self, cmd, **kwargs):
            self.returncode = 0
            self.pid = 4243
            self.stdin = None
            out = kwargs.get("stdout")
            if out is not None:
                out.write("dumped uri: mongodb://root:Sup3rSecret@db.internal:27017/\n".encode("utf-8"))
                out.flush()

        def poll(self):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr("tools.shell.subprocess.Popen", _StubPopen)
    from tools.shell import ShellTool
    result = ShellTool().execute(command="cat dump.log")
    assert "Sup3rSecret" not in result
    assert "mongodb://" not in result
    assert "dumped uri: ***" in result


# ── Python REPL end-to-end with stubbed Popen ──

def test_python_repl_substitutes_for_child_and_masks_result(vault, monkeypatch):
    _seed()
    captured = {}

    class _StubPopen:
        def __init__(self, args, **kwargs):
            captured["args"] = args
            self.returncode = 0
            self.pid = 4244
            with open(args[1], "r", encoding="utf-8") as f:
                captured["code"] = f.read()

        def communicate(self, timeout=None):
            return ("token is Sup3rSecret", "")

        def kill(self):
            pass

    monkeypatch.setattr("tools.python_repl.subprocess.Popen", _StubPopen)
    from tools.python_repl import PythonREPLTool
    result = PythonREPLTool().execute(code='print("pw={{secret:mydb.password}}")')

    # Real value only inside the executed temp file
    assert 'print("pw=Sup3rSecret")' in captured["code"]
    assert "{{secret:mydb.password}}" not in captured["code"]

    # Returned output is masked
    assert "Sup3rSecret" not in result
    assert "token is ***" in result


# ── upsert optional-field semantics (review fix) ──

def test_upsert_preserves_password_when_omitted(vault):
    _seed()
    # Update without password -> preserved
    upsert_secret(name="mydb", host="db2.internal", note="rotated host")
    assert get_secret("mydb")["password"] == "Sup3rSecret"
    assert get_secret("mydb")["host"] == "db2.internal"
    # Explicit empty string -> cleared
    upsert_secret(name="mydb", password="")
    assert get_secret("mydb")["password"] == ""


def test_api_upsert_password_optional_semantics(vault):
    client = _make_client()
    assert client.post("/api/secrets", json={
        "name": "mydb", "type": "mongodb", "host": "db.internal",
        "username": "root", "password": "Sup3rSecret",
    }).status_code == 200
    # Omit password -> preserved
    assert client.post("/api/secrets", json={"name": "mydb", "note": "n2"}).status_code == 200
    assert get_secret("mydb")["password"] == "Sup3rSecret"
    assert get_secret("mydb")["note"] == "n2"
    # Explicit empty string -> cleared
    assert client.post("/api/secrets", json={"name": "mydb", "password": ""}).status_code == 200
    assert get_secret("mydb")["password"] == ""


# ── mask thresholds (review fix) ──

def test_mask_short_password_threshold(vault):
    upsert_secret(name="tiny", type="generic", password="ab1")
    assert mask_secrets("pin ab1 ok") == "pin ab1 ok"      # len 3 -> not masked
    upsert_secret(name="four", type="generic", password="ab12")
    assert mask_secrets("pin ab12 ok") == "pin *** ok"     # len 4 -> masked


def test_mask_quoted_password_form(vault):
    upsert_secret(name="enc", type="generic", password="p@ss w0rd!")
    # percent-encoded form must not escape masking
    assert mask_secrets("escaped p%40ss%20w0rd%21 ok") == "escaped *** ok"


# ── mask BEFORE truncate leaves no residue (review fix) ──

def test_mask_before_truncate_leaves_no_residue(vault):
    _seed()  # password Sup3rSecret (11 chars)
    text = "A" * 50 + "Sup3rSecret" + "B" * 50
    cut = 56  # lands mid-password
    # Old order (truncate then mask): whole-string match fails on the fragment
    fragment = mask_secrets(text[:cut])
    assert "Sup3rSecret" not in fragment
    assert "Sup3r" in fragment  # leaked prefix of the password
    # New order (mask then truncate, as agent.py / sub_agent.py now do)
    masked_first = mask_secrets(text)[:cut]
    assert "Sup3r" not in masked_first
    assert "***" in masked_first


# ── API file-read paths mask raw shell output (review Critical) ──

def _make_tasks_client(monkeypatch, out_file):
    import api.routes.routes_tasks as rt
    monkeypatch.setattr(rt, "get_background_processes_for_task",
                        lambda tid: {"1234": {"output_file": str(out_file)}})
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(rt.router)
    return TestClient(app), rt


def test_task_logs_endpoint_masks_output(vault, monkeypatch, tmp_path):
    _seed()
    out_file = tmp_path / "shell.log"
    out_file.write_text(
        "connecting...\nauth ok with Sup3rSecret\n"
        "uri mongodb://root:Sup3rSecret@db.internal:27017/\n",
        encoding="utf-8")
    client, _ = _make_tasks_client(monkeypatch, out_file)
    resp = client.get("/api/tasks/7/logs")
    assert resp.status_code == 200
    assert "Sup3rSecret" not in resp.text
    assert "mongodb://" not in resp.text
    assert "***" in resp.text


def test_kill_endpoint_masks_output(vault, monkeypatch, tmp_path):
    _seed()
    out_file = tmp_path / "shell.log"
    out_file.write_text("token Sup3rSecret\n", encoding="utf-8")
    client, rt = _make_tasks_client(monkeypatch, out_file)

    captured = {}
    monkeypatch.setattr(rt, "get_task_context", lambda tid: [])
    monkeypatch.setattr(rt, "save_task_context",
                        lambda tid, ctx: captured.__setitem__("ctx", ctx))
    monkeypatch.setattr(rt, "update_task_status",
                        lambda tid, status, summary=None, **kw:
                        captured.update(status=status, summary=summary))

    # Point the kill endpoint's resume_count UPDATE at a throwaway DB
    import sqlite3 as _sq
    db_path = tmp_path / "tasks.db"
    conn = _sq.connect(str(db_path))
    conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, resume_count INTEGER)")
    conn.execute("INSERT INTO tasks (id, resume_count) VALUES (7, 3)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(rt, "DB_PATH", str(db_path))

    resp = client.post("/api/tasks/7/kill")
    assert resp.status_code == 200, resp.text
    # Injected into task context masked
    ctx_text = captured["ctx"][0]["content"]
    assert "Sup3rSecret" not in ctx_text
    assert "***" in ctx_text
    # Status summary masked
    assert "Sup3rSecret" not in captured["summary"]


def test_bgmonitor_output_read_is_masked(vault, tmp_path):
    _seed()
    from api.background import _read_masked_output_tail

    f = tmp_path / "out.log"
    f.write_text("auth Sup3rSecret ok\n", encoding="utf-8")
    out = _read_masked_output_tail(str(f), 5000)
    assert "Sup3rSecret" not in out
    assert "***" in out

    # Tail cut lands mid-password: mask-before-cut leaves no fragment
    f2 = tmp_path / "out2.log"
    f2.write_text("A" * 50 + "Sup3rSecret" + "B" * 20, encoding="utf-8")
    tail = _read_masked_output_tail(str(f2), 25)
    assert "Sup3r" not in tail
    assert "ecret" not in tail
    assert "***" in tail


# ── database field (Task 3 review fix: URI path segment no longer dropped) ──

def test_database_roundtrip_and_build_uri(vault):
    upsert_secret(name="mongo1", type="mongodb", host="db.internal", port="27017",
                  username="root", password="Sup3rSecret", database="admin")
    assert get_secret("mongo1")["database"] == "admin"
    uri = build_uri("mongo1")
    assert uri == "mongodb://root:Sup3rSecret@db.internal:27017/admin"
    # Masked view carries database (for-llm shares the same view), never the password
    masked = [s for s in list_secrets() if s["name"] == "mongo1"][0]
    assert masked["database"] == "admin"
    _assert_no_password(masked, "Sup3rSecret")
    # {{secret:x.database}} substitutable; {{secret:x.uri}} includes the database
    assert substitute_refs("db={{secret:mongo1.database}}") == "db=admin"
    assert substitute_refs("go {{secret:mongo1.uri}}") == f"go {uri}"


def test_database_preserved_when_omitted(vault):
    upsert_secret(name="mongo1", type="mongodb", host="h", password="pw12345", database="admin")
    # Omit database -> preserved (None semantics, same as other optional fields)
    upsert_secret(name="mongo1", note="n2")
    assert get_secret("mongo1")["database"] == "admin"
    # Explicit empty string -> cleared
    upsert_secret(name="mongo1", database="")
    assert get_secret("mongo1")["database"] == ""


def test_api_upsert_database_field(vault):
    client = _make_client()
    assert client.post("/api/secrets", json={
        "name": "pg1", "type": "postgres", "host": "h:5432",
        "username": "u", "password": "pw12345", "database": "app",
    }).status_code == 200
    res = client.get("/api/secrets")
    entry = [s for s in res.json()["secrets"] if s["name"] == "pg1"][0]
    assert entry["database"] == "app"
    _assert_no_password(res.json(), "pw12345")
    assert build_uri("pg1") == "postgresql://u:pw12345@h:5432/app"
