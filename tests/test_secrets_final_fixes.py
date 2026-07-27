"""
凭证功能终审修复回归（I-1 / I-2）:

- I-1: SessionLogger.log_tool_result 落盘 jsonl 前统一过 mask_secrets。
  agent.py 在脱敏之前就把原始结果交给 logger（agent/agent.py:2830），
  read_file/fetch_url 等未做工具层脱敏的工具若回显库内密码，jsonl 里
  必须是 *** 而非明文；密码跨越 8000 字符截断边界时也不得泄漏。
- I-2: api.ws._try_store_late_secret — 授权等待超时后的迟到凭据提交
  （前端恒带 secret_name/secret_type，见 ChatView.vue:onSandboxRespond）
  直接 upsert 入 vault 并返回用户提示；普通迟到授权返回 None，
  由调用分支继续走白名单/resume。凭据提交绝不触碰 _pending_sandbox_approvals。

全部使用隔离 vault（OPEN_AGC_DATA_DIR -> tmp），无 LLM、无网络、无真实 websocket。
"""
import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.logger import SessionLogger  # noqa: E402
from core.secrets import get_secret, list_secrets, upsert_secret  # noqa: E402

PASSWORD = "Sup3rSecret"


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    """Isolated vault: OPEN_AGC_DATA_DIR redirects data/secrets.json to tmp."""
    monkeypatch.setenv("OPEN_AGC_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_pending_approvals():
    from api.state import _pending_sandbox_approvals
    _pending_sandbox_approvals.clear()
    yield
    _pending_sandbox_approvals.clear()


def _read_single_jsonl(log_dir):
    files = [f for f in os.listdir(log_dir) if f.endswith(".jsonl")]
    assert len(files) == 1
    with open(os.path.join(log_dir, files[0]), "r", encoding="utf-8") as f:
        raw = f.read()
    return raw, [json.loads(line) for line in raw.strip().splitlines()]


# ── I-1: log_tool_result 落盘脱敏 ──

def test_log_tool_result_masks_vault_password(vault):
    upsert_secret(name="mydb", type="mongodb", username="root", password=PASSWORD)
    log_dir = os.path.join(str(vault), "logs")
    logger = SessionLogger(log_dir, session_id=991001)
    # read_file 类工具不做工具层脱敏，结果里直接回显了库内密码
    logger.log_tool_result("read_file", f"配置内容: password={PASSWORD} 其余文本")

    raw, entries = _read_single_jsonl(log_dir)
    assert PASSWORD not in raw, "jsonl 落盘含明文密码"
    assert entries[0]["type"] == "tool_result"
    assert entries[0]["tool"] == "read_file"
    assert "***" in entries[0]["result"]
    assert entries[0]["success"] is True


def test_log_tool_result_masks_password_across_truncation_boundary(vault):
    upsert_secret(name="mydb", password=PASSWORD)
    log_dir = os.path.join(str(vault), "logs")
    logger = SessionLogger(log_dir, session_id=991002)
    # 密码横跨 8000 字符截断点：必须先脱敏后截断，否则漏出前半截明文
    payload = "x" * 7995 + PASSWORD + "tail"
    logger.log_tool_result("fetch_url", payload)

    raw, entries = _read_single_jsonl(log_dir)
    assert PASSWORD not in raw
    assert len(entries[0]["result"]) <= 8000


def test_log_tool_result_plain_text_unchanged(vault):
    log_dir = os.path.join(str(vault), "logs")
    logger = SessionLogger(log_dir, session_id=991003)
    logger.log_tool_result("read_file", "普通文件内容", success=False)

    _, entries = _read_single_jsonl(log_dir)
    assert entries[0]["result"] == "普通文件内容"
    assert entries[0]["success"] is False


def test_log_tool_result_none_result_safe(vault):
    log_dir = os.path.join(str(vault), "logs")
    logger = SessionLogger(log_dir, session_id=991004)
    logger.log_tool_result("browser", None)

    _, entries = _read_single_jsonl(log_dir)
    assert entries[0]["result"] == ""


# ── I-2: 迟到凭据提交入库 ──

def _late_secret_msg(**overrides):
    msg = {
        "type": "sandbox_response", "session_id": 555001,
        "action": "approve_always",
        "path": "prod_db",  # LLM 建议的凭据名 —— 绝不能当沙箱路径加白名单
        "password": PASSWORD,
        "secret_name": "prod_db", "secret_type": "mongodb",
        "host": "db.internal", "username": "root", "note": "prod master",
    }
    msg.update(overrides)
    return msg


def test_late_secret_submission_stored_in_vault(vault):
    from api.state import _pending_sandbox_approvals
    from api.ws import _try_store_late_secret

    reply = _try_store_late_secret(_late_secret_msg())

    assert reply is not None and "已入库" in reply
    assert "prod_db" in reply
    entry = get_secret("prod_db")
    assert entry is not None
    assert entry["password"] == PASSWORD
    assert entry["type"] == "mongodb"
    assert entry["host"] == "db.internal"
    assert entry["username"] == "root"
    assert entry["note"] == "prod master"
    # 不走通用 late-approval 路径：不写 pending approvals（调用方据此跳过 resume）
    assert _pending_sandbox_approvals.get(555001) is None


def test_late_secret_plain_approval_returns_none(vault):
    from api.ws import _try_store_late_secret

    # 普通迟到沙箱授权（无 secret 字段）——不拦截，交给白名单/resume 逻辑
    assert _try_store_late_secret({
        "type": "sandbox_response", "session_id": 555002,
        "action": "approve_dir", "path": "D:/data",
    }) is None
    # sudo 弹窗的迟到密码也不带 secret 字段 —— 同样不拦截
    assert _try_store_late_secret({
        "type": "sandbox_response", "session_id": 555002,
        "action": "approve_once", "path": "", "password": PASSWORD,
    }) is None
    assert list_secrets() == []


def test_late_secret_invalid_name_reported_not_whitelisted(vault):
    from api.state import _pending_sandbox_approvals
    from api.ws import _try_store_late_secret

    reply = _try_store_late_secret(_late_secret_msg(**{
        "path": "bad name!", "secret_name": "bad name!", "secret_type": "generic",
    }))

    # 是凭据提交就必须拦截（返回错误提示），绝不能落入白名单分支
    assert reply is not None and "失败" in reply
    assert list_secrets() == []
    assert _pending_sandbox_approvals.get(555001) is None
