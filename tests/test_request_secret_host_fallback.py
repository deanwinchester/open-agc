# -*- coding: utf-8 -*-
"""request_secret 的同主机凭据回退测试。

生产实证：库里已有 aiserver（host 192.168.148.200），agent 起名 cns_ssh
又弹窗向用户要了一遍 SSH 密码。弹窗前应先查同主机条目。
"""
import pytest

from tools.base import SandboxBlocked
from tools.request_secret import RequestSecretTool


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    import core.secrets as sec
    monkeypatch.setattr(sec, "_path", lambda: str(tmp_path / "secrets.json"))
    sec.upsert_secret("aiserver", host="192.168.148.200",
                      username="cns", password="x")
    return sec


def test_host_fallback_avoids_popup(vault):
    out = RequestSecretTool().execute(purpose="部署 Grafana",
                                      name="cns_ssh", host="192.168.148.200")
    assert "aiserver" in out
    assert "force=true" in out
    assert "{{secret:名称.password}}" in out


def test_force_bypasses_fallback_and_pops(vault):
    with pytest.raises(SandboxBlocked):
        RequestSecretTool().execute(purpose="换新凭据", name="cns_ssh",
                                    host="192.168.148.200", force=True)


def test_unknown_host_still_pops(vault):
    with pytest.raises(SandboxBlocked):
        RequestSecretTool().execute(purpose="连别的机器", name="other",
                                    host="10.0.0.99")


def test_name_fallback_still_works(vault):
    out = RequestSecretTool().execute(purpose="x", name="aiserver",
                                      host="192.168.148.200")
    assert "{{secret:aiserver}}" in out
