# -*- coding: utf-8 -*-
"""Docker 部署访问控制一致性回归：
- entrypoint 必须 --no-proxy-headers（与 launcher/gui_app 一致，防 XFF 伪造）
- compose 默认只绑 127.0.0.1（bridge NAT 下公网禁止只能靠映射面保证）"""
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestDockerAccessControl:
    def test_entrypoint_no_proxy_headers(self):
        src = open(os.path.join(PROJECT_ROOT, "docker-entrypoint.sh"),
                   encoding="utf-8").read()
        assert "--no-proxy-headers" in src

    def test_compose_binds_localhost_only_by_default(self):
        src = open(os.path.join(PROJECT_ROOT, "docker-compose.yml"),
                   encoding="utf-8").read()
        assert "127.0.0.1:8000:8000" in src
        # 不得裸绑 0.0.0.0（"8000:8000" 无主机地址前缀的形式）
        ports_section = src.split("ports:")[1].split("volumes:")[0]
        active = [l for l in ports_section.splitlines()
                  if l.strip().startswith("-") and not l.strip().startswith("#")]
        assert all("127.0.0.1" in l for l in active), \
            f"compose 默认端口绑定必须只有 127.0.0.1: {active}"
