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

    def test_env_password_seeds_config_once(self, monkeypatch):
        """OPEN_AGC_ACCESS_PASSWORD 只在 config.json 未配置时播种写入；
        已配置则不动（config.json 为唯一事实源，判断口径一致）。"""
        import core.access_control as ac
        store = {}
        monkeypatch.setenv("OPEN_AGC_ACCESS_PASSWORD", "env-pass")
        # 未配置 → 播种
        assert ac.seed_access_password_from_env(
            load_fn=lambda: dict(store),
            save_fn=lambda c: store.update(c)) is True
        assert store["access_password"] == "env-pass"
        # 已配置 → 不再覆盖
        store["access_password"] = "user-pass"
        assert ac.seed_access_password_from_env(
            load_fn=lambda: dict(store),
            save_fn=lambda c: store.update(c)) is False
        assert store["access_password"] == "user-pass"
        # 判定只读 config
        assert ac.get_access_password({"access_password": "user-pass"}) == "user-pass"
        # 无环境变量 → 不播种
        monkeypatch.delenv("OPEN_AGC_ACCESS_PASSWORD")
        assert ac.seed_access_password_from_env(
            load_fn=lambda: {}, save_fn=lambda c: None) is False

    def test_compose_documents_env_password(self):
        src = open(os.path.join(PROJECT_ROOT, "docker-compose.yml"),
                   encoding="utf-8").read()
        assert "OPEN_AGC_ACCESS_PASSWORD" in src

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
