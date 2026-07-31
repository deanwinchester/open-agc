"""Tool for the agent to scaffold and develop new plugins."""
import importlib.util
import json
import os
from typing import Any, Dict

from tools.base import BaseTool


def _to_python_module(name: str) -> str:
    """Convert a plugin name like 'my-plugin' to a valid Python module name 'my_plugin'."""
    return name.replace("-", "_").replace(" ", "_").lower()


class DevelopPluginTool(BaseTool):
    """Scaffold and develop new plugins for the Open-AGC system."""

    name: str = "develop_plugin"
    description: str = (
        "开发新插件：生成脚手架（plugin.json、__init__.py、Vue 视图入口 static/vue-entry.js）、"
        "校验插件代码或验收检查（verify：语法/契约/已知缺陷扫描，交付前必做）。"
        "前端契约：plugin.json 声明 \"vue_entry\": \"vue-entry.js\"，入口文件是插件"
        " static/ 下的原生 ES module，default export 为 setup(ctx)，返回"
        " {views: [{path, title, icon?, component}]}；component 必须用 ctx.Vue.defineComponent"
        " 创建（模板字符串由主应用编译，el-* 组件可直接使用）；每个视图自动挂载到路由"
        " /plugins/<插件名>/<path> 并出现在侧边栏插件区。修改插件代码或 vue-entry.js 后，"
        "调用 POST /api/plugins/scan（或设置页「扫描新插件」按钮）即可热更新，无需重启服务。"
        "用户要求扩展系统插件功能时用。"
    )

    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["scaffold", "install", "verify"],
                            "description": "scaffold=生成新插件脚手架；install=校验插件代码；verify=验收检查（语法/契约/已知缺陷，交付前必做）。",
                        },
                        "plugin_name": {
                            "type": "string",
                            "description": "插件名称（如 my-plugin、hello-world）。",
                        },
                        "description": {
                            "type": "string",
                            "description": "插件描述。",
                        },
                        "version": {
                            "type": "string",
                            "description": "插件版本号（默认 1.0.0）。",
                        },
                        "author": {
                            "type": "string",
                            "description": "插件作者。",
                        },
                        "init_code": {
                            "type": "string",
                            "description": "__init__.py 的完整代码，必须包含 init_plugin(context) 函数。",
                        },
                        "routes_code": {
                            "type": "string",
                            "description": "可选，routes.py 代码（API 路由）。",
                        },
                        "menu_label": {
                            "type": "string",
                            "description": "侧边栏插件分区显示名称（写入 plugin.json 的 menu.label）。",
                        },
                        "menu_icon": {
                            "type": "string",
                            "description": "侧边栏菜单图标 emoji（默认 🔌）。",
                        },
                        "has_static": {
                            "type": "boolean",
                            "description": "是否生成 static/vue-entry.js 示例视图（默认 true，推荐；插件 UI 契约参考样例）。",
                        },
                    },
                    "required": ["action", "plugin_name"],
                },
            },
        }

    def _plugins_base(self) -> str:
        """Return the base directory for user-developed plugins (data/ for Docker persistence)."""
        from core.paths import get_user_plugins_dir
        return get_user_plugins_dir()

    def _scaffold(self, **kwargs) -> str:
        name = kwargs.get("plugin_name", "").strip()
        if not name:
            return "插件名称不能为空"
        mod_name = _to_python_module(name)
        plugin_dir = os.path.join(self._plugins_base(), name)
        if os.path.exists(plugin_dir):
            return f"插件目录 {plugin_dir} 已存在"

        desc = kwargs.get("description", f"{name} 插件")
        version = kwargs.get("version", "1.0.0")
        author = kwargs.get("author", "Open-AGC")
        menu_label = kwargs.get("menu_label", "")
        menu_icon = kwargs.get("menu_icon", "🔌")
        # has_static 语义：是否生成 static/vue-entry.js 示例视图（新 SPA 前端契约）
        has_static = kwargs.get("has_static", True)
        if has_static is None:
            has_static = True

        os.makedirs(plugin_dir, exist_ok=True)

        # -- plugin.json --
        manifest = {
            "name": name,
            "version": version,
            "description": desc,
            "author": author,
            "python_module": mod_name,
        }
        if has_static:
            # 新 SPA 插件 UI 契约：入口为 static/vue-entry.js（default export setup(ctx)）
            manifest["vue_entry"] = "vue-entry.js"
        if menu_label:
            # menu 仅保留 label/icon，用于侧边栏插件分区标题与图标
            manifest["menu"] = {"label": menu_label, "icon": menu_icon}

        with open(os.path.join(plugin_dir, "plugin.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        # -- __init__.py (generated or provided) --
        init_code = kwargs.get("init_code", "")
        if init_code:
            with open(os.path.join(plugin_dir, "__init__.py"), "w", encoding="utf-8") as f:
                f.write(init_code)
        else:
            self._write_default_init(plugin_dir, name, mod_name, has_static)

        # -- routes.py --
        routes_code = kwargs.get("routes_code", "")
        if routes_code:
            with open(os.path.join(plugin_dir, "routes.py"), "w", encoding="utf-8") as f:
                f.write(routes_code)

        # -- static/vue-entry.js --
        if has_static:
            static_dir = os.path.join(plugin_dir, "static")
            os.makedirs(static_dir, exist_ok=True)
            self._write_vue_entry(static_dir, name, menu_label or name)

        summary_parts = [
            f"✅ 插件 {name} 脚手架已生成",
            f"  目录: {plugin_dir}/",
            f"  plugin.json — 清单文件（含 vue_entry 前端入口声明）",
            f"  __init__.py — 插件后端入口",
        ]
        if routes_code:
            summary_parts.append("  routes.py — API 路由")
        if has_static:
            summary_parts.append("  static/vue-entry.js — 前端视图入口（契约参考样例，可直接改）")
        summary_parts.append("")
        summary_parts.append(
            "下一步：编辑代码后调用 POST /api/plugins/scan（或设置页「扫描新插件」）"
            "即可热更新生效，无需重启服务。视图路由为 /plugins/%s/main。" % name
        )
        summary_parts.append(
            "验收（必做）：scan 生效后调用 develop_plugin(action=\"verify\", "
            "plugin_name=\"%s\") 做静态验收（语法/契约/已知缺陷扫描），全部通过后再交付给用户。" % name
        )
        return "\n".join(summary_parts)

    def _write_default_init(self, plugin_dir: str, name: str, mod_name: str, has_static: bool) -> None:
        static_block = ""
        if has_static:
            static_block = """
    static_dir = os.path.join(plugin_dir, "static")
    if os.path.isdir(static_dir):
        instance.static_dir = static_dir"""

        # Template with __NAME__ as placeholder to avoid f-string/format confusion
        tmpl = '''"""
__NAME__ plugin — auto-generated by develop_plugin tool.
"""
import os
from fastapi import APIRouter
from core.plugin_manager import PluginContext, PluginInstance


def init_plugin(context: PluginContext) -> PluginInstance:
    """Initialize the __NAME__ plugin."""
    router = APIRouter()
    plugin_dir = context.plugin_dir

    # 调用系统默认大模型（跟随「设置」页的默认模型与密钥，禁止自行硬编码
    # API Key / base_url / 模型名）：
    # from core.llm_client import LLMClient
    # llm = LLMClient()
    # resp, model_used = llm.chat(messages=[{"role": "user", "content": "你好"}])
    # text = resp.choices[0].message.content

    @router.get("/hello")
    async def hello():
        return {"message": "Hello from __NAME__!"}

    instance = PluginInstance(
        router=router,
        router_prefix=f"/api/plugin/__NAME__",
        state={"version": "1.0.0"},
    )''' + static_block + '''
    return instance
'''
        code = tmpl.replace("__NAME__", name)
        with open(os.path.join(plugin_dir, "__init__.py"), "w", encoding="utf-8") as f:
            f.write(code)

    def _write_vue_entry(self, static_dir: str, name: str, label: str) -> None:
        """Write static/vue-entry.js — 新 SPA 插件 UI 契约的可用参考样例。

        契约：default export setup(ctx) → { views: [{path, title, icon?, component}] }，
        视图挂载到 /plugins/<name>/<path>。本模板同时是 agent 开发插件 UI 的布局基准
        （整页容器 + 居中内容区 max-width 860px + el-card），避免「右侧样式坏」问题。
        """
        # Plain string + placeholder replace（模板内含大量 {}，不能用 f-string/format）
        tmpl = '''// ============================================================
// __LABEL__（__NAME__）— Open-AGC 插件前端入口（Vue3 SPA 契约）
//
// 契约要点（与 dev-docs/API契约.md「插件 Vue 视图契约」一致）：
//   1. plugin.json 声明 "vue_entry": "vue-entry.js"，本文件经插件静态目录暴露：
//      /static/plugins/<name>/vue-entry.js
//   2. 本文件是原生 ES module，default export 为 setup(ctx)，
//      返回 { views: [{ path, title, icon?, component }] }（也可 async 返回）。
//   3. component 必须用 ctx.Vue.defineComponent 创建（与主应用同一 Vue 实例；
//      模板字符串由主应用运行时编译，Element Plus 的 el-* 组件可直接使用，
//      主题 CSS 变量与主应用共享）。
//   4. 每个 view 挂载为路由 /plugins/<name>/<path>，并出现在侧边栏插件区。
//   5. 修改本文件或插件 Python 代码后：调用 POST /api/plugins/scan
//      （或设置页「扫描新插件」按钮）即可热更新，无需重启服务。
//
// ctx 可用能力：
//   ctx.pluginName                  插件名
//   ctx.Vue                         Vue 命名空间（defineComponent/ref/computed/onMounted/...）
//   ctx.apiFetch                    主应用 API client，**本身就是函数**：apiFetch(url, options?) → Promise<JSON>
//                                   ⚠️ 不是 apiFetch.request(...)——它没有 .request 方法，那样写必报错
//   ctx.ElMessage / ctx.ElMessageBox  Element Plus 反馈组件
//   ctx.wsOn(type, fn)              订阅主应用 WebSocket 事件（返回退订函数）
//   ctx.navigate(path)              router.push 封装（插件内跳转）
//
// 主题规约：默认贴近主应用风格（浅色背景、el-* 组件 + 共享 CSS 变量
// var(--el-*)，不要自定义页面底色/暗色主题/全局字体）；仅当用户明确
// 要求自定义主题时例外。
// ============================================================

// 注入本插件的局部样式（class 统一加 __NAME__- 前缀，避免污染全局）
function injectStyles() {
  const id = '__NAME__-styles';
  if (document.getElementById(id)) return;
  const style = document.createElement('style');
  style.id = id;
  style.textContent = `
.__NAME__-page { padding: 24px; box-sizing: border-box; }
.__NAME__-inner { max-width: 860px; margin: 0 auto; }
.__NAME__-title { margin: 0 0 4px; font-size: 20px; font-weight: 600; }
.__NAME__-desc { margin: 0 0 16px; color: var(--el-text-color-secondary); font-size: 13px; }
`;
  document.head.appendChild(style);
}

export default function setup(ctx) {
  const { Vue, apiFetch, ElMessage } = ctx;
  injectStyles();

  const MainView = Vue.defineComponent({
    name: '__NAME__-main',
    setup() {
      const message = Vue.ref('加载中…');
      const loading = Vue.ref(false);

      async function load() {
        loading.value = true;
        try {
          // 插件后端路由默认挂在 /api/plugin/<name>/ 前缀下（见 __init__.py）
          const data = await apiFetch('/api/plugin/__NAME__/hello');
          message.value = (data && data.message) || '后端无返回';
        } catch (err) {
          message.value = '请求失败：' + err.message;
        } finally {
          loading.value = false;
        }
      }

      function notify() {
        ElMessage.success('__LABEL__ 运行正常');
      }

      Vue.onMounted(load);
      return { message, loading, notify };
    },
    // 布局规范：外层整页容器 + 居中内容区（max-width 860px）+ el-card，
    // 与主应用其余页面视觉保持一致；不要裸写无容器的模板
    template: `
      <div class="__NAME__-page">
        <div class="__NAME__-inner">
          <h2 class="__NAME__-title">__LABEL__</h2>
          <p class="__NAME__-desc">__NAME__ 插件 · 示例视图</p>
          <el-card shadow="never" v-loading="loading">
            <p>{{ message }}</p>
            <el-button type="primary" @click="notify">测试通知</el-button>
          </el-card>
        </div>
      </div>
    `,
  });

  return {
    views: [
      { path: 'main', title: '__LABEL__', component: MainView },
    ],
  };
}
'''
        code = tmpl.replace("__NAME__", name).replace("__LABEL__", label)
        with open(os.path.join(static_dir, "vue-entry.js"), "w", encoding="utf-8") as f:
            f.write(code)

    def _install(self, name: str) -> str:
        plugin_dir = os.path.join(self._plugins_base(), name)
        if not os.path.exists(plugin_dir):
            return f"插件目录 {plugin_dir} 不存在，请先执行 scaffold"

        if not os.path.exists(os.path.join(plugin_dir, "plugin.json")):
            return f"缺少 plugin.json，无法安装"

        if not os.path.exists(os.path.join(plugin_dir, "__init__.py")):
            return f"缺少 __init__.py，无法安装"

        # Trigger plugin reload via import
        import sys
        plugins_parent = os.path.abspath(self._plugins_base())
        if plugins_parent not in sys.path:
            sys.path.insert(0, sys.path.pop(0) if sys.path[0] == plugins_parent else plugins_parent)
            sys.path.insert(0, plugins_parent)

        mod_name = _to_python_module(name)
        try:
            # Actually the module name is just the mod_name within plugins package
            # First try direct import
            spec = importlib.util.spec_from_file_location(
                mod_name, os.path.join(plugin_dir, "__init__.py")
            )
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                if hasattr(mod, "init_plugin"):
                    return (f"插件 {name} 代码验证通过。调用 POST /api/plugins/scan "
                            f"（或设置页「扫描新插件」）即可热更新生效，无需重启服务。")
                return f"插件 {name} 缺少 init_plugin 函数"
        except Exception as e:
            return f"插件 {name} 加载失败: {e}"

    def _esbuild(self):
        """定位 esbuild 二进制（仓库 node_modules/.bin）；找不到返回 None。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for cand in ("esbuild.cmd", "esbuild"):
            p = os.path.join(root, "node_modules", ".bin", cand)
            if os.path.isfile(p):
                return p
        return None

    def _check_js_syntax(self, entry_path: str):
        """esbuild 语法检查；返回 (ok, detail)。esbuild 缺失时跳过（ok=None）。"""
        esbuild = self._esbuild()
        if not esbuild:
            return None, "esbuild 不可用，跳过语法检查"
        import subprocess
        try:
            proc = subprocess.run(
                [esbuild, entry_path, "--log-level=error"],
                capture_output=True, text=True, timeout=30)
        except Exception as e:
            return None, f"语法检查执行失败: {e}"
        if proc.returncode == 0:
            return True, ""
        return False, (proc.stderr or proc.stdout or "语法错误").strip()[:300]

    _BAD_PATTERNS = [
        (r"apiFetch\s*\.\s*request\s*\(",
         "apiFetch 本身就是函数，应直接 apiFetch(url, ...) —— 写 apiFetch.request 必报 "
         "'apiFetch.request is not a function'（生产实证）"),
        (r"ctx\s*\.\s*request\s*\(", "ctx 上没有 request 方法，请用 ctx.apiFetch"),
    ]
    _DARK_THEME_RE = r"background(?:-color)?\s*:\s*#(?:0[0-9a-fA-F]{2,5}|1[0-9a-fA-F]{2,5})\b"

    def _verify(self, name: str) -> str:
        """验收检查（用户要求：开发完成后必须有测试环节，避免带病交付）。

        检查项：plugin.json 有效且声明 vue_entry、vue-entry.js 存在且语法
        通过（esbuild）、已知错误用法扫描（apiFetch.request 等）、暗色自定义
        主题告警（默认应贴近主应用浅色风格，用户明确要求除外）、__init__.py
        含 init_plugin、插件当前已加载。返回逐项 ✅/⚠️/❌ 报告。"""
        import re
        plugin_dir = os.path.join(self._plugins_base(), name)
        if not os.path.isdir(plugin_dir):
            return f"插件目录 {plugin_dir} 不存在"
        lines = []
        errors = 0
        warnings = 0

        def check(label, ok, detail=""):
            nonlocal errors
            if ok is False:
                errors += 1
            mark = "✅" if ok else ("➖" if ok is None else "❌")
            lines.append(f"{mark} {label}{(' — ' + detail) if detail else ''}")

        def warn(label, detail=""):
            nonlocal warnings
            warnings += 1
            lines.append(f"⚠️ {label}{(' — ' + detail) if detail else ''}")

        # 1) plugin.json
        manifest = None
        manifest_path = os.path.join(plugin_dir, "plugin.json")
        if os.path.isfile(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
            except Exception:
                manifest = None
        check("plugin.json 有效", manifest is not None)

        # 2) vue_entry 前端契约
        vue_entry = (manifest or {}).get("vue_entry", "")
        if not vue_entry:
            check("vue_entry 声明", None, "未声明（纯后端插件可忽略）")
        else:
            entry_path = os.path.join(plugin_dir, "static", vue_entry)
            check("vue-entry 文件存在", os.path.isfile(entry_path), vue_entry)
            if os.path.isfile(entry_path):
                ok, detail = self._check_js_syntax(entry_path)
                check("vue-entry 语法检查（esbuild）", ok, detail)
                try:
                    src = open(entry_path, encoding="utf-8").read()
                except Exception:
                    src = ""
                check("default export 存在",
                      bool(re.search(r"export\s+default", src)))
                code_lines = [l for l in src.splitlines()
                              if not l.strip().startswith(("//", "*", "/*"))]
                code = "\n".join(code_lines)
                for pat, msg in self._BAD_PATTERNS:
                    if re.search(pat, code):
                        check("已知错误用法扫描", False, msg)
                        break
                else:
                    check("已知错误用法扫描", True)
                if re.search(self._DARK_THEME_RE, code):
                    warn("主题风格", "检测到暗色自定义底色——默认应贴近主应用浅色风格"
                         "（el-* + 共享 CSS 变量），仅当用户明确要求自定义主题时保留")

        # 3) __init__.py init_plugin
        init_path = os.path.join(plugin_dir, "__init__.py")
        has_init = False
        if os.path.isfile(init_path):
            try:
                has_init = "init_plugin" in open(init_path, encoding="utf-8").read()
            except Exception:
                has_init = False
        check("__init__.py 含 init_plugin", has_init)

        # 4) 加载状态（scan 后应已加载）
        try:
            from core.plugin_manager import get_plugin
            info = get_plugin(name)
            check("插件当前已加载", bool(info and info.instance),
                  "未加载——需先调用 POST /api/plugins/scan")
        except Exception as e:
            check("插件当前已加载", None, f"状态查询失败: {e}")

        verdict = "通过" if errors == 0 else f"未通过（{errors} 项错误）"
        if warnings:
            verdict += f"，{warnings} 项警告"
        lines.insert(0, f"插件 {name} 验收{verdict}")
        if errors:
            lines.append("请逐项修复后重新 verify，全部通过再交付用户。")
        return "\n".join(lines)

    def execute(self, action: str, **kwargs) -> str:
        try:
            name = kwargs.get("plugin_name", "").strip()
            if action == "scaffold":
                return self._scaffold(**kwargs)
            elif action == "install":
                return self._install(name)
            elif action == "verify":
                if not name:
                    return "插件名称不能为空"
                return self._verify(name)
            else:
                return f"未知操作: {action}"
        except Exception as e:
            return f"操作失败: {e}"
