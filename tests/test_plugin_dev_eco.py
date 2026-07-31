"""插件开发生态修复的回归测试：

A. develop_plugin 脚手架产出新 SPA 的 Vue 契约（vue_entry + static/vue-entry.js，无旧 views 数组）
B. unload_plugin 清理 sys.modules；unload_all + 重新 discover 后拿到新代码（热重载，无需重启）
D. _remove_plugin 移动到 data/plugins/_trash 回收站而非物理删除，且 _trash 不参与扫描
"""
import ast
import json
import os
import shutil
import subprocess
import sys

import pytest


# ── helpers ──────────────────────────────────────────────────

def _make_plugin_dir(base, name, version="1.0.0", extra_py=""):
    """Create a minimal loadable plugin under base/<name>. Returns plugin dir."""
    pdir = os.path.join(base, name)
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, "plugin.json"), "w", encoding="utf-8") as f:
        json.dump({"name": name, "version": version, "python_module": name}, f)
    init_src = f'''"""test plugin {name}"""
from core.plugin_manager import PluginInstance

VERSION = "{version}"

def init_plugin(context):
    return PluginInstance(state={{"version": VERSION}})
{extra_py}'''
    with open(os.path.join(pdir, "__init__.py"), "w", encoding="utf-8") as f:
        f.write(init_src)
    return pdir


def _loaded_versions(name):
    from core.plugin_manager import get_plugin
    info = get_plugin(name)
    return info.instance.state.get("version") if info and info.instance else None


# ── A. 脚手架 Vue 契约 ───────────────────────────────────────

class TestScaffoldVueContract:
    def test_manifest_has_vue_entry_and_no_legacy_views(self, tmp_path, monkeypatch):
        user_dir = str(tmp_path / "user_plugins")
        monkeypatch.setattr("core.paths.get_user_plugins_dir", lambda: user_dir)

        from tools.plugin_dev import DevelopPluginTool
        out = DevelopPluginTool().execute(
            "scaffold", plugin_name="eco-demo", menu_label="演示插件", menu_icon="🧪"
        )
        assert "脚手架已生成" in out

        with open(os.path.join(user_dir, "eco-demo", "plugin.json"), encoding="utf-8") as f:
            manifest = json.load(f)

        # 新契约：vue_entry 指向 static/ 下的 ES module
        assert manifest["vue_entry"] == "vue-entry.js"
        assert manifest["python_module"] == "eco_demo"
        # menu 只保留 label/icon；旧 SPA 的 views 数组与 section 必须消失
        assert manifest["menu"] == {"label": "演示插件", "icon": "🧪"}
        assert "views" not in manifest["menu"]
        assert "section" not in manifest["menu"]
        # 新入口存在，旧 index.html 不再生成
        static_dir = os.path.join(user_dir, "eco-demo", "static")
        assert os.path.isfile(os.path.join(static_dir, "vue-entry.js"))
        assert not os.path.exists(os.path.join(static_dir, "index.html"))

    def test_vue_entry_template_contents(self, tmp_path, monkeypatch):
        user_dir = str(tmp_path / "user_plugins")
        monkeypatch.setattr("core.paths.get_user_plugins_dir", lambda: user_dir)

        from tools.plugin_dev import DevelopPluginTool
        DevelopPluginTool().execute("scaffold", plugin_name="eco-tpl", menu_label="模板")

        entry = os.path.join(user_dir, "eco-tpl", "static", "vue-entry.js")
        with open(entry, encoding="utf-8") as f:
            src = f.read()

        # 契约要素：setup(ctx) / ctx.Vue / 布局容器 / apiFetch / ElMessage / 路由说明
        assert "export default function setup(ctx)" in src
        assert "ctx.Vue" in src
        assert "Vue.defineComponent" in src
        assert "el-card" in src            # Element Plus 卡片
        assert "max-width: 860px" in src   # 居中布局容器
        assert "apiFetch" in src
        assert "ElMessage" in src
        assert "/plugins/<name>/<path>" in src  # 注释中的路由契约说明
        assert "views" in src

    def test_vue_entry_template_parses_with_node(self, tmp_path, monkeypatch):
        node = shutil.which("node")
        if not node:
            pytest.skip("node 不可用")
        user_dir = str(tmp_path / "user_plugins")
        monkeypatch.setattr("core.paths.get_user_plugins_dir", lambda: user_dir)

        from tools.plugin_dev import DevelopPluginTool
        DevelopPluginTool().execute("scaffold", plugin_name="eco-node", menu_label="解析")

        entry = os.path.join(user_dir, "eco-node", "static", "vue-entry.js")
        # node --check 默认按 CommonJS 解析，ES module 需 .mjs 后缀
        mjs = str(tmp_path / "vue-entry-check.mjs")
        shutil.copyfile(entry, mjs)
        r = subprocess.run([node, "--check", mjs], capture_output=True, text=True)
        assert r.returncode == 0, f"node --check 失败: {r.stderr}"

    def test_has_static_false_omits_vue_entry(self, tmp_path, monkeypatch):
        user_dir = str(tmp_path / "user_plugins")
        monkeypatch.setattr("core.paths.get_user_plugins_dir", lambda: user_dir)

        from tools.plugin_dev import DevelopPluginTool
        DevelopPluginTool().execute("scaffold", plugin_name="eco-noui", has_static=False)
        with open(os.path.join(user_dir, "eco-noui", "plugin.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        assert "vue_entry" not in manifest
        assert not os.path.exists(os.path.join(user_dir, "eco-noui", "static"))

    def test_scaffold_tool_ast_and_description_mentions_contract(self):
        # 改动文件可被 ast 解析 + 工具描述向 agent 说明热更新契约
        import tools.plugin_dev as mod
        ast.parse(open(mod.__file__, encoding="utf-8").read())
        tool = mod.DevelopPluginTool()
        desc = tool.description
        assert "vue_entry" in desc
        assert "setup(ctx)" in desc
        assert "无需重启服务" in desc
        schema = tool.get_openai_schema()
        props = schema["function"]["parameters"]["properties"]
        assert "has_static" in props
        assert "menu_section" not in props  # 旧 SPA 契约参数已移除


# ── B. 热重载 ────────────────────────────────────────────────

class TestHotReload:
    def test_unload_purges_sys_modules(self, tmp_path):
        # basename 用合法且全测试文件唯一的标识符，走包式导入；不同用例的
        # 插件目录 basename 若相同，父包会被 sys.modules 缓存串到上一个用例的临时目录
        plugs = str(tmp_path / "uplugs_unload")
        pdir = _make_plugin_dir(plugs, "ecoplg")
        with open(os.path.join(pdir, "helper_mod.py"), "w", encoding="utf-8") as f:
            f.write('MARKER = "x"\n')
        with open(os.path.join(pdir, "__init__.py"), "a", encoding="utf-8") as f:
            f.write("from . import helper_mod  # noqa\n")

        from core.plugin_manager import load_plugin, unload_plugin
        try:
            info = load_plugin("ecoplg", plugs)
            assert info is not None
            resid = [m for m in sys.modules if m == "ecoplg" or ".ecoplg" in m]
            assert resid, "加载后 sys.modules 应有插件模块"
            assert any(m.endswith("ecoplg.helper_mod") for m in sys.modules)

            assert unload_plugin("ecoplg") is True
            left = [m for m in sys.modules if m == "ecoplg" or m.endswith(".ecoplg")
                    or ".ecoplg." in m]
            assert left == [], f"unload 后 sys.modules 仍有残留: {left}"
        finally:
            unload_plugin("ecoplg")

    def test_scan_style_reload_picks_up_new_code(self, tmp_path):
        """模拟 POST /api/plugins/scan 的流程：unload_all_plugins() 后重新 discover。"""
        plugs = str(tmp_path / "uplugs_reload")
        pdir = _make_plugin_dir(plugs, "ecoreload", version="1.0.0")

        from core.plugin_manager import (
            discover_plugins, unload_all_plugins, unload_plugin,
        )
        try:
            discover_plugins(plugs)
            assert _loaded_versions("ecoreload") == "1.0.0"

            # 修改插件代码（版本号 + 明显不同的内容长度，并清 __pycache__ 防 pyc 缓存）
            _make_plugin_dir(plugs, "ecoreload", version="2.0.0",
                             extra_py="CHANGED = True  # code changed after first load\n")
            shutil.rmtree(os.path.join(pdir, "__pycache__"), ignore_errors=True)

            # scan 端点做法：先卸载全部（含 sys.modules 清理）再重新 discover
            unload_all_plugins()
            discover_plugins(plugs)
            assert _loaded_versions("ecoreload") == "2.0.0", "重新扫描后应加载到新代码"
        finally:
            unload_plugin("ecoreload")

    def test_discover_skips_underscore_dirs(self, tmp_path):
        """_trash 等 _ 前缀目录不参与插件扫描。"""
        plugs = str(tmp_path / "uplugs_skip")
        _make_plugin_dir(plugs, "ecovisible")
        # 回收站里的插件（目录结构 <_trash>/<name>_<ts>/plugin.json）
        _make_plugin_dir(os.path.join(plugs, "_trash"), "ecovisible_dead")

        from core.plugin_manager import discover_plugins, list_all_plugins, unload_plugin
        try:
            found = discover_plugins(plugs)
            names = {p.name for p in found}
            assert "ecovisible" in names
            assert not any("dead" in n for n in names)
            listed = {p["name"] for p in list_all_plugins(plugs)}
            assert not any("dead" in n for n in listed)
        finally:
            unload_plugin("ecovisible")


# ── D. 删除保护（回收站） ────────────────────────────────────

class TestRemovePluginTrash:
    def test_remove_moves_to_trash_not_delete(self, tmp_path, monkeypatch):
        user_dir = str(tmp_path / "user_plugins")
        os.makedirs(user_dir, exist_ok=True)
        monkeypatch.setattr("core.paths.get_user_plugins_dir", lambda: user_dir)

        _make_plugin_dir(user_dir, "trashdemo-eco-xyz")

        from tools.system_config import ConfigureSystemTool
        out = ConfigureSystemTool()._remove_plugin("trashdemo-eco-xyz")

        # 原目录消失，但不是物理删除：回收站里有完整副本
        assert not os.path.exists(os.path.join(user_dir, "trashdemo-eco-xyz"))
        trash_root = os.path.join(user_dir, "_trash")
        entries = os.listdir(trash_root)
        assert len(entries) == 1 and entries[0].startswith("trashdemo-eco-xyz_")
        moved = os.path.join(trash_root, entries[0])
        assert os.path.isfile(os.path.join(moved, "plugin.json"))
        assert os.path.isfile(os.path.join(moved, "__init__.py"))
        # 返回文本告知回收位置
        assert "回收站" in out and moved in out

    def test_remove_missing_plugin_reports(self, tmp_path, monkeypatch):
        user_dir = str(tmp_path / "user_plugins")
        os.makedirs(user_dir, exist_ok=True)
        monkeypatch.setattr("core.paths.get_user_plugins_dir", lambda: user_dir)

        from tools.system_config import ConfigureSystemTool
        out = ConfigureSystemTool()._remove_plugin("no-such-plugin-eco")
        assert "不存在" in out


# ── 评审修复 1：幽灵路由（被删插件的 REST/静态挂载残留） ──────

class TestGhostRoutes:
    """mount_plugins 重挂载前统一剪除全部 /api/plugin/ 与 /static/plugins/ 前缀，
    被删/回收/init 失败的插件（不在新列表里）不得继续服务旧代码。"""

    def _make_info(self, name, marker, static_dir=None):
        from fastapi import APIRouter
        from core.plugin_manager import PluginInfo, PluginInstance

        router = APIRouter()

        @router.get("/hello")
        async def hello():
            return {"plugin": name, "marker": marker}

        inst = PluginInstance(router=router, router_prefix=f"/api/plugin/{name}",
                              static_dir=static_dir)
        return PluginInfo(name=name, version="1", instance=inst, plugin_dir="")

    def test_deleted_plugin_routes_return_404(self, tmp_path):
        pytest.importorskip("httpx")
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api.plugin_mount import mount_plugins

        static_dir = tmp_path / "ghost_static"
        static_dir.mkdir()
        (static_dir / "x.txt").write_text("ghost", encoding="utf-8")

        app = FastAPI()
        mount_plugins(app, [self._make_info("ghosteco", "v1", str(static_dir))],
                      logger=lambda *a: None)
        c = TestClient(app)
        assert c.get("/api/plugin/ghosteco/hello").status_code == 200
        assert c.get("/static/plugins/ghosteco/x.txt").status_code == 200

        # 插件被删/回收后不在新发现列表 → 重新挂载后其路由与静态挂载必须 404
        mount_plugins(app, [], logger=lambda *a: None)
        assert c.get("/api/plugin/ghosteco/hello").status_code == 404
        assert c.get("/static/plugins/ghosteco/x.txt").status_code == 404
        leftovers = [getattr(r, "path", "") for r in app.router.routes
                     if (getattr(r, "path", "") or "").startswith(("/api/plugin/", "/static/plugins/"))]
        assert leftovers == [], f"插件作用域路由应全部剪除: {leftovers}"

    def test_remount_replaces_with_new_code(self, tmp_path):
        pytest.importorskip("httpx")
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api.plugin_mount import mount_plugins

        app = FastAPI()
        mount_plugins(app, [self._make_info("swapecco", "v1")], logger=lambda *a: None)
        # 同名插件重新挂载（scan 重载）：新代码生效且不残留旧路由
        mount_plugins(app, [self._make_info("swapecco", "v2")], logger=lambda *a: None)
        c = TestClient(app)
        r = c.get("/api/plugin/swapecco/hello")
        assert r.status_code == 200 and r.json()["marker"] == "v2"
        paths = [getattr(r, "path", "") for r in app.router.routes]
        assert paths.count("/api/plugin/swapecco/hello") == 1, "不得出现重复路由"


# ── 评审修复 2：scan 时保留活动在跑的训练插件 ────────────────

def _make_fake_train_plugin(plugs_dir, active=True):
    """创建假 open-agc-train：engine 模块级单例，get_state() 含 active 字段。"""
    pdir = os.path.join(plugs_dir, "open-agc-train")
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, "plugin.json"), "w", encoding="utf-8") as f:
        json.dump({"name": "open-agc-train", "version": "1.0.0",
                   "python_module": "open-agc-train"}, f)
    with open(os.path.join(pdir, "engine.py"), "w", encoding="utf-8") as f:
        f.write(f'''_training_engine = None

class FakeEngine:
    def __init__(self):
        self._state = {{"active": {active!r}}}

    def get_state(self):
        return dict(self._state)

def get_training_engine():
    global _training_engine
    if _training_engine is None:
        _training_engine = FakeEngine()
    return _training_engine
''')
    with open(os.path.join(pdir, "__init__.py"), "w", encoding="utf-8") as f:
        f.write('''from . import engine
from core.plugin_manager import PluginInstance

def init_plugin(context):
    return PluginInstance(state={"engine": engine.get_training_engine()})
''')
    return pdir


class TestScanPreservesActiveTraining:
    """训练中执行 scan：open-agc-train 不卸载重载（engine 实例 id 不变），
    其余插件照常刷新；无活动任务时正常参与重载。"""

    def _scan_flow(self, plugs):
        from api.routes.routes_plugins import _plugins_to_preserve_on_scan
        from core.plugin_manager import discover_plugins, unload_all_plugins
        preserve = _plugins_to_preserve_on_scan()
        unload_all_plugins(except_names=preserve)
        discover_plugins(plugs)
        return preserve

    def test_active_training_engine_survives_scan(self, tmp_path):
        plugs = str(tmp_path / "uplugs_train_active")
        _make_fake_train_plugin(plugs, active=True)
        _make_plugin_dir(plugs, "othereco", version="1.0.0")

        from core.plugin_manager import discover_plugins, get_plugin, unload_plugin
        try:
            discover_plugins(plugs)
            engine_before = get_plugin("open-agc-train").instance.state["engine"]
            assert engine_before.get_state()["active"] is True

            # 另一插件改了代码（模拟 agent 迭代）
            _make_plugin_dir(plugs, "othereco", version="2.0.0",
                             extra_py="CHANGED = True  # reloaded\n")
            import shutil as _sh
            _sh.rmtree(os.path.join(plugs, "othereco", "__pycache__"), ignore_errors=True)

            preserve = self._scan_flow(plugs)
            assert "open-agc-train" in preserve

            # 训练插件保持旧实例（engine id 不变），其余插件拿到新代码
            assert get_plugin("open-agc-train").instance.state["engine"] is engine_before
            assert _loaded_versions("othereco") == "2.0.0"
        finally:
            unload_plugin("open-agc-train")
            unload_plugin("othereco")

    def test_idle_training_plugin_reloads_normally(self, tmp_path):
        plugs = str(tmp_path / "uplugs_train_idle")
        _make_fake_train_plugin(plugs, active=False)

        from core.plugin_manager import discover_plugins, get_plugin, unload_plugin
        try:
            discover_plugins(plugs)
            engine_before = get_plugin("open-agc-train").instance.state["engine"]

            preserve = self._scan_flow(plugs)
            assert preserve == set(), "无活动任务时不应保留"
            # 正常卸载重载：重新 init 拿到的是新模块里的新 engine 实例
            assert get_plugin("open-agc-train").instance.state["engine"] is not engine_before
        finally:
            unload_plugin("open-agc-train")


# ── 生产实证回归：rescan 后插件 vue-entry 被主 /static 挂载遮蔽 404 ──

class TestPluginRoutesNotShadowed:
    """启动顺序（server.py）：插件挂载(:169) → 主 /static 挂载(:459) →
    SPA catch-all(:528)，所以首发正常；但 rescan 重插时若只避 catch-all，
    插件静态挂载会落到主 /static 之后被遮蔽（实测 vue-entry 全 404）。
    修复：插入点取 catch-all 与主 /static 挂载中更早者。"""

    def _make_info(self, name, static_dir=None):
        from fastapi import APIRouter
        from core.plugin_manager import PluginInfo, PluginInstance

        router = APIRouter()

        @router.get("/hello")
        async def hello():
            return {"plugin": name}

        inst = PluginInstance(router=router, router_prefix=f"/api/plugin/{name}",
                              static_dir=static_dir)
        return PluginInfo(name=name, version="1", instance=inst, plugin_dir="")

    def _build_app_like_server(self, tmp_path, plugin_static):
        """按 server.py 的真实顺序建 app：插件 → 主静态 → catch-all。"""
        from fastapi import FastAPI
        from fastapi.staticfiles import StaticFiles
        from api.plugin_mount import mount_plugins

        app = FastAPI()
        mount_plugins(app, [self._make_info("demo", str(plugin_static))],
                      logger=lambda *a: None)
        main_static = tmp_path / "main_static"
        main_static.mkdir()
        (main_static / "home.txt").write_text("home", encoding="utf-8")
        app.mount("/static", StaticFiles(directory=str(main_static)), name="static")

        @app.get("/{full_path:path}")
        async def spa_fallback(full_path: str):
            return {"spa": full_path}

        return app

    def test_rescan_keeps_plugin_routes_before_shadows(self, tmp_path):
        pytest.importorskip("httpx")
        from fastapi.testclient import TestClient
        from api.plugin_mount import mount_plugins

        plugin_static = tmp_path / "p_static"
        plugin_static.mkdir()
        (plugin_static / "vue-entry.js").write_text("export default 1", encoding="utf-8")
        app = self._build_app_like_server(tmp_path, plugin_static)
        c = TestClient(app)
        # 启动态正常
        assert c.get("/api/plugin/demo/hello").status_code == 200
        assert c.get("/static/plugins/demo/vue-entry.js").status_code == 200

        # rescan 重挂载：插件 API 与静态都必须仍在主 /static 与 catch-all 之前
        mount_plugins(app, [self._make_info("demo", str(plugin_static))],
                      logger=lambda *a: None)
        assert c.get("/api/plugin/demo/hello").status_code == 200, "API 被 catch-all 遮蔽"
        assert c.get("/static/plugins/demo/vue-entry.js").status_code == 200, \
            "插件静态被主 /static 挂载遮蔽"
        # 主静态与 SPA 不受影响
        assert c.get("/static/home.txt").status_code == 200
        assert c.get("/some/spa/route").json() == {"spa": "some/spa/route"}
