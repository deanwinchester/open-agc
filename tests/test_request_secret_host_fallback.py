# -*- coding: utf-8 -*-
"""request_secret 的同主机凭据回退测试。

生产实证：库里已有 server（host 192.0.2.10），agent 起名 server_ssh
又弹窗向用户要了一遍 SSH 密码。弹窗前应先查同主机条目。
"""
import pytest

from tools.base import SandboxBlocked
from tools.request_secret import RequestSecretTool


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    import core.secrets as sec
    monkeypatch.setattr(sec, "_path", lambda: str(tmp_path / "secrets.json"))
    sec.upsert_secret("server", host="192.0.2.10",
                      username="brand", password="x")
    return sec


def test_host_fallback_avoids_popup(vault):
    out = RequestSecretTool().execute(purpose="部署 Grafana",
                                      name="server_ssh", host="192.0.2.10")
    assert "server" in out
    assert "force=true" in out
    assert "{{secret:名称.password}}" in out


def test_force_bypasses_fallback_and_pops(vault):
    with pytest.raises(SandboxBlocked):
        RequestSecretTool().execute(purpose="换新凭据", name="server_ssh",
                                    host="192.0.2.10", force=True)


def test_unknown_host_still_pops(vault):
    with pytest.raises(SandboxBlocked):
        RequestSecretTool().execute(purpose="连别的机器", name="other",
                                    host="10.0.0.99")


def test_name_fallback_still_works(vault):
    out = RequestSecretTool().execute(purpose="x", name="server",
                                      host="192.0.2.10")
    assert "{{secret:server}}" in out
