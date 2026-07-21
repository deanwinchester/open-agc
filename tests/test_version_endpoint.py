"""版本端点升级提示的回归测试：本地版本高于线上时不得提示升级。"""
import asyncio

import pytest

from api.routes import routes_system


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


@pytest.mark.parametrize("current,latest,expected", [
    ("1.0.2rc12", "1.0.2rc11", False),   # 本地比线上新 → 不提示
    ("1.0.2rc11", "1.0.2rc12", True),    # 线上比本地新 → 提示
    ("1.0.2rc12", "1.0.2rc12", False),   # 相同 → 不提示
    ("1.0.2rc12", None, False),          # 查不到线上 → 不提示
])
def test_update_available_compares_versions(monkeypatch, current, latest, expected):
    monkeypatch.setattr(routes_system, "get_version", lambda: current)
    import core.auto_upgrade as auto_upgrade
    # AutoUpgrader.__init__ 从 core.auto_upgrade 命名空间读 get_version，需一并替换
    monkeypatch.setattr(auto_upgrade, "get_version", lambda: current)
    monkeypatch.setattr(auto_upgrade.AutoUpgrader, "fetch_latest_release",
                        lambda self: setattr(self, "latest_version", latest) or latest)
    result = _run(routes_system.get_api_version())
    assert result["update_available"] is expected
    assert result["current"] == current
