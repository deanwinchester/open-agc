# -*- coding: utf-8 -*-
"""提示注入到插件/MCP/配置/脚本执行路径的工具级单次审批测试。"""
import io
import json
import os
import threading
import time
import zipfile

import pytest

from tools.base import SandboxBlocked


@pytest.fixture()
def session_id():
    from api.state import _session_permission_once, _session_permission_whitelists
    sid = 987654
    _session_permission_once.pop(sid, None)
    _session_permission_whitelists.pop(sid, None)
    try:
        yield sid
    finally:
        _session_permission_once.pop(sid, None)
        _session_permission_whitelists.pop(sid, None)


def _grant(session_id, tool_name, payload):
    from api.state import _session_permission_once
    from core.system_guard import approval_operation
    operation = approval_operation(tool_name, payload)
    _session_permission_once.setdefault(session_id, set()).add(operation)
    return operation


def _skill_zip(with_script=True):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("demo/SKILL.md", "---\nname: demo\n---\n# Demo\n")
        if with_script:
            zf.writestr("demo/scripts/run.py", "print('run')\n")
    return buf.getvalue()


def test_develop_plugin_install_requires_one_shot_approval(monkeypatch, session_id):
    from tools.plugin_dev import DevelopPluginTool
    calls = []
    monkeypatch.setattr(DevelopPluginTool, "_install", lambda self, name: calls.append(name) or "installed")
    tool = DevelopPluginTool()

    with pytest.raises(SandboxBlocked) as exc_info:
        tool.execute(action="install", plugin_name="demo", _session_id=session_id)
    assert exc_info.value.category == "plugin_install"
    assert exc_info.value.sandbox_dir == "permission"
    assert calls == []

    _grant(session_id, "develop_plugin", {"action": "install", "plugin_name": "demo"})
    assert tool.execute(action="install", plugin_name="demo", _session_id=session_id) == "installed"
    assert calls == ["demo"]

    with pytest.raises(SandboxBlocked):
        tool.execute(action="install", plugin_name="demo", _session_id=session_id)
    assert calls == ["demo"]


@pytest.mark.parametrize("raw_value", ["false", "0", "null"])
def test_configure_system_sandbox_off_requires_approval(monkeypatch, tmp_path, session_id, raw_value):
    import tools.system_config as sc
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text('{"sandbox_mode": true}', encoding="utf-8")
    monkeypatch.setattr(sc, "CONFIG_PATH", str(cfg_path))
    tool = sc.ConfigureSystemTool()

    with pytest.raises(SandboxBlocked) as exc_info:
        tool.execute(action="update_setting", key="sandbox_mode", value=raw_value,
                     _session_id=session_id)
    assert exc_info.value.category == "sandbox_off"
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["sandbox_mode"] is True

    parsed = json.loads(raw_value)
    payload = {"action": "update_setting", "key": "sandbox_mode", "value": parsed}
    _grant(session_id, "configure_system", payload)
    assert "已更新配置" in tool.execute(action="update_setting", key="sandbox_mode",
                                        value=raw_value, _session_id=session_id)
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["sandbox_mode"] == parsed


@pytest.mark.parametrize("key,value", [
    ("access_password", '"new-password"'),
    ("server_url", '"https://accounts.example/"'),
    ("update_manifest_url", '"https://updates.example/version.json"'),
    ("base_url", '"https://model-gateway.example/v1"'),
    ("custom_providers", '[{"name":"gw","base_url":"https://model-gateway.example/v1"}]'),
])
def test_configure_system_risky_keys_require_approval(monkeypatch, tmp_path, session_id, key, value):
    import tools.system_config as sc
    monkeypatch.setattr(sc, "CONFIG_PATH", str(tmp_path / "config.json"))
    with pytest.raises(SandboxBlocked) as exc_info:
        sc.ConfigureSystemTool().execute(action="update_setting", key=key, value=value,
                                        _session_id=session_id)
    assert exc_info.value.category == "config_risk"
    assert not (tmp_path / "config.json").exists()


def test_configure_system_normal_setting_does_not_require_approval(monkeypatch, tmp_path):
    import tools.system_config as sc
    monkeypatch.setattr(sc, "CONFIG_PATH", str(tmp_path / "config.json"))
    out = sc.ConfigureSystemTool().execute(
        action="update_setting", key="heartbeat_interval", value="300")
    assert "已更新配置" in out
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))["heartbeat_interval"] == 300


def test_configure_system_stdio_mcp_requires_approval(monkeypatch, tmp_path, session_id):
    import tools.system_config as sc
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text('{"mcp_servers": {}}', encoding="utf-8")
    monkeypatch.setattr(sc, "CONFIG_PATH", str(cfg_path))
    tool = sc.ConfigureSystemTool()
    kwargs = dict(action="add_mcp_server", mcp_server_name="local",
                  mcp_command="python", mcp_args='["evil.py"]')

    with pytest.raises(SandboxBlocked) as exc_info:
        tool.execute(**kwargs, _session_id=session_id)
    assert exc_info.value.category == "mcp_add"
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["mcp_servers"] == {}

    payload = {"action": "add_mcp_server", "name": "local",
               "command": "python", "args": ["evil.py"], "env": None}
    _grant(session_id, "configure_system", payload)
    assert "已添加" in tool.execute(**kwargs, _session_id=session_id)
    assert "local" in json.loads(cfg_path.read_text(encoding="utf-8"))["mcp_servers"]


def test_update_setting_cannot_bypass_stdio_mcp_approval(monkeypatch, tmp_path, session_id):
    import tools.system_config as sc
    monkeypatch.setattr(sc, "CONFIG_PATH", str(tmp_path / "config.json"))
    servers = {"local": {"command": "python", "args": ["evil.py"], "env": {"X": "1"}}}
    with pytest.raises(SandboxBlocked) as exc_info:
        sc.ConfigureSystemTool().execute(action="update_setting", key="mcp_servers",
                                        value=json.dumps(servers), _session_id=session_id)
    assert exc_info.value.category == "mcp_add"
    assert not (tmp_path / "config.json").exists()


def test_mcp_and_plugin_reads_remain_unblocked(monkeypatch, tmp_path):
    import tools.system_config as sc
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"mcp_servers": {
        "existing": {"command": "python", "args": ["server.py"]}}}), encoding="utf-8")
    monkeypatch.setattr(sc, "CONFIG_PATH", str(cfg_path))
    assert "existing" in sc.ConfigureSystemTool().execute(action="get_mcp_servers")


def test_install_skill_with_script_requires_approval_before_copy(monkeypatch, tmp_path, session_id):
    import core.skill_installer as si
    from tools.install_skill import InstallSkillTool
    url = "https://github.com/owner/demo"
    monkeypatch.setattr(si, "get_skills_dir", lambda: str(tmp_path / "skills"))
    monkeypatch.setattr(si, "_fetch_bytes", lambda *args, **kwargs: _skill_zip())
    tool = InstallSkillTool()

    with pytest.raises(SandboxBlocked) as exc_info:
        tool.execute(url=url, _session_id=session_id)
    assert exc_info.value.category == "plugin_install"
    assert not (tmp_path / "skills" / "demo").exists()

    payload = {"action": "install_skill", "url": url, "scripts": ["scripts/run.py"]}
    _grant(session_id, "install_skill", payload)
    assert "安装成功" in tool.execute(url=url, _session_id=session_id)
    assert (tmp_path / "skills" / "demo" / "scripts" / "run.py").is_file()


def test_install_skill_without_scripts_keeps_existing_flow(monkeypatch, tmp_path):
    import core.skill_installer as si
    from tools.install_skill import InstallSkillTool
    monkeypatch.setattr(si, "get_skills_dir", lambda: str(tmp_path / "skills"))
    monkeypatch.setattr(si, "_fetch_bytes", lambda *args, **kwargs: _skill_zip(False))
    out = InstallSkillTool().execute(url="https://github.com/owner/demo")
    assert "安装成功" in out


def _respond_to_permission(agent, sb, action):
    from api.state import _sandbox_waits
    events = []
    result = {}

    def run():
        result["value"] = agent._handle_sandbox_blocked(sb, "develop_plugin", {}, events.append)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline and not events:
        time.sleep(0.01)
    assert events
    entry = _sandbox_waits[events[0]["request_id"]]
    entry["result"]["action"] = action
    entry["event"].set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    return result["value"]


def _bare_agent(session_id):
    from agent.agent import OpenAGCAgent
    agent = OpenAGCAgent.__new__(OpenAGCAgent)
    agent.session_id = session_id
    agent.is_interrupted = False
    agent._session_permission_whitelist = set()
    agent._session_sandbox_whitelist = set()
    agent._session_network_whitelist = set()
    agent._session_sudo_password = ""
    agent._pending_sudo_password = ""
    return agent


@pytest.mark.parametrize("category", ["plugin_install", "mcp_add", "config_risk", "sandbox_off"])
@pytest.mark.parametrize("action", ["approve_session", "approve_always"])
def test_rce_categories_downgrade_broad_grants_to_once(category, action, session_id):
    from api.state import _session_permission_once, _session_permission_whitelists
    agent = _bare_agent(session_id)
    sb = SandboxBlocked("operation-id", sandbox_dir="permission", tool_name="develop_plugin",
                        category=category, description="高风险操作")
    assert _respond_to_permission(agent, sb, action) is None
    assert "operation-id" in _session_permission_once[session_id]
    assert category not in agent._session_permission_whitelist
    assert category not in _session_permission_whitelists.get(session_id, set())


def test_rce_permission_denial_does_not_create_one_shot_grant(session_id):
    from api.state import _session_permission_once
    agent = _bare_agent(session_id)
    sb = SandboxBlocked("operation-id", sandbox_dir="permission", tool_name="develop_plugin",
                        category="plugin_install", description="安装插件会执行代码")
    result = _respond_to_permission(agent, sb, "deny_once")
    assert "denied" in result
    assert session_id not in _session_permission_once
