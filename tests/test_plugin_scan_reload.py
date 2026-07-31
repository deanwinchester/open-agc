# -*- coding: utf-8 -*-
"""插件 scan 热更新回归：scan 全量重导阻塞事件循环（秒级）导致 WS 断连、
进度停更、页面重载时插件菜单拉取失败变空（生产实证：agent 开发插件连续
4 次 scan 均伴随 WS disconnect）。修复：签名未变插件保留不重导 +
scan 整体移执行器线程 + 前端注册失败重试。"""
import asyncio
import os
import sys
import time

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import core.plugin_manager as pm  # noqa: E402
import api.routes.routes_plugins as rp  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_plugins(monkeypatch, tmp_path):
    """隔离插件全局状态，不碰真实 _loaded_plugins/签名表/插件目录。"""
    monkeypatch.setattr(pm, "_loaded_plugins", {})
    monkeypatch.setattr(pm, "_plugin_dir_signatures", {})
    yield


def _make_plugin_dir(base, name, marker="v1"):
    d = base / name
    d.mkdir()
    (d / "plugin.json").write_text(
        '{"name": "%s", "version": "1.0.0"}' % name, encoding="utf-8")
    (d / "__init__.py").write_text(
        "from core.plugin_manager import PluginInstance\n"
        "def init_plugin(context):\n"
        f"    return PluginInstance(state={{'v': '{marker}'}})\n",
        encoding="utf-8")
    return d


class TestDirSignature:
    def test_signature_changes_with_content(self, tmp_path):
        d = _make_plugin_dir(tmp_path, "p1")
        s1 = pm.dir_signature(str(d))
        assert s1 > 0
        time.sleep(0.02)
        (d / "extra.py").write_text("x = 1\n", encoding="utf-8")
        s2 = pm.dir_signature(str(d))
        assert s2 >= s1
        assert pm.dir_signature(str(d)) == s2  # 无变化则稳定


class TestLoadUnloadSignature:
    def test_load_stores_and_unload_clears(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pm, "_get_plugin_state", lambda *a, **k: {"enabled": True})
        _make_plugin_dir(tmp_path, "p2")
        info = pm.load_plugin("p2", plugins_dir=str(tmp_path))
        assert info is not None
        assert pm.get_loaded_signature("p2") is not None
        pm.unload_plugin("p2")
        assert pm.get_loaded_signature("p2") is None


class TestSelectivePreserve:
    def test_unchanged_preserved_changed_reloaded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pm, "_get_plugin_state", lambda *a, **k: {"enabled": True})
        d1 = _make_plugin_dir(tmp_path, "keep-me")
        d2 = _make_plugin_dir(tmp_path, "change-me")
        pm.load_plugin("keep-me", plugins_dir=str(tmp_path))
        pm.load_plugin("change-me", plugins_dir=str(tmp_path))
        # 制造 change-me 的内容变更（mtime 前进）
        time.sleep(0.02)
        newf = d2 / "changed.py"
        newf.write_text("y = 2\n", encoding="utf-8")
        os.utime(newf, (time.time() + 5, time.time() + 5))
        unchanged = rp._plugins_unchanged_since_load()
        assert "keep-me" in unchanged
        assert "change-me" not in unchanged


class TestScanOffloaded:
    def test_scan_runs_in_executor(self, monkeypatch):
        """scan 端点必须移出事件循环（同步 purge/reimport 是秒级阻塞）。"""
        used = {"executor": False, "func": None}
        loop = asyncio.new_event_loop()

        async def _fake_coro():
            return {"status": "ok"}

        def spy(executor, func, *args):
            used["executor"] = True
            used["func"] = func
            fut = loop.create_future()
            fut.set_result({"status": "ok", "offloaded": True})
            return fut

        monkeypatch.setattr(loop, "run_in_executor", spy)
        try:
            result = loop.run_until_complete(rp.scan_plugins())
        finally:
            loop.close()
        assert used["executor"], "scan_plugins 未移出事件循环"
        assert used["func"] is rp._do_scan_sync
        assert result["offloaded"] is True


class TestFrontendRetry:
    def test_registry_has_retry(self):
        src = open(os.path.join(PROJECT_ROOT, "vue-app", "src",
                                "plugins", "registry.js"), encoding="utf-8").read()
        assert "_INIT_RETRY_MAX" in src and "setTimeout" in src
