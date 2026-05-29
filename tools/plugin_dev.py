"""Tool for the agent to scaffold and develop new plugins."""
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
    description: str = "开发新插件：生成插件目录结构、添加路由、添加静态文件、安装插件。"

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
                            "enum": ["scaffold", "install"],
                            "description": "要执行的操作：scaffold=生成新插件脚手架，install=安装插件",
                        },
                        "plugin_name": {
                            "type": "string",
                            "description": "插件名称（如 my-plugin、hello-world）",
                        },
                        "description": {
                            "type": "string",
                            "description": "插件描述",
                        },
                        "version": {
                            "type": "string",
                            "description": "插件版本号（默认 1.0.0）",
                        },
                        "author": {
                            "type": "string",
                            "description": "插件作者",
                        },
                        "init_code": {
                            "type": "string",
                            "description": "__init__.py 的完整代码，必须包含 init_plugin(context) 函数",
                        },
                        "routes_code": {
                            "type": "string",
                            "description": "可选的 routes.py 代码（API 路由）",
                        },
                        "menu_section": {
                            "type": "string",
                            "description": "侧边栏菜单位置（如 tools、training、data）",
                        },
                        "menu_label": {
                            "type": "string",
                            "description": "侧边栏菜单显示名称",
                        },
                        "menu_icon": {
                            "type": "string",
                            "description": "侧边栏菜单图标 emoji",
                        },
                        "has_static": {
                            "type": "boolean",
                            "description": "是否需要静态文件目录",
                        },
                    },
                    "required": ["action", "plugin_name"],
                },
            },
        }

    def _scaffold(self, **kwargs) -> str:
        name = kwargs.get("plugin_name", "").strip()
        if not name:
            return "插件名称不能为空"
        mod_name = _to_python_module(name)
        plugin_dir = os.path.join("plugins", name)
        if os.path.exists(plugin_dir):
            return f"插件目录 {plugin_dir} 已存在"

        desc = kwargs.get("description", f"{name} 插件")
        version = kwargs.get("version", "1.0.0")
        author = kwargs.get("author", "Open-AGC")
        menu_section = kwargs.get("menu_section", "")
        menu_label = kwargs.get("menu_label", "")
        menu_icon = kwargs.get("menu_icon", "🔌")
        has_static = kwargs.get("has_static", False)

        os.makedirs(plugin_dir, exist_ok=True)

        # -- plugin.json --
        manifest = {
            "name": name,
            "version": version,
            "description": desc,
            "author": author,
            "python_module": mod_name,
        }
        if menu_section and menu_label:
            manifest["menu"] = {
                "section": menu_section,
                "label": menu_label,
                "icon": menu_icon,
                "views": [
                    {"id": f"{mod_name}-main", "label": menu_label}
                ],
            }

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

        # -- static/ --
        if has_static:
            static_dir = os.path.join(plugin_dir, "static")
            os.makedirs(static_dir, exist_ok=True)
            with open(os.path.join(static_dir, "index.html"), "w", encoding="utf-8") as f:
                f.write(self._default_static_html(name, menu_label or name))

        summary_parts = [
            f"✅ 插件 {name} 脚手架已生成",
            f"  目录: {plugin_dir}/",
            f"  plugin.json — 清单文件",
            f"  __init__.py — 插件入口",
        ]
        if routes_code:
            summary_parts.append("  routes.py — API 路由")
        if has_static:
            summary_parts.append("  static/ — 静态文件目录")
        summary_parts.append("")
        summary_parts.append("使用 `action=install` 安装插件使其生效。")
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

    def _default_static_html(self, name: str, label: str) -> str:
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{label}</title>
<link rel="stylesheet" href="/static/dist/open-agc.css">
</head>
<body>
<div class="plugin-container">
  <h1>{label}</h1>
  <p>{name} 插件已加载。</p>
</div>
</body>
</html>"""

    def _install(self, name: str) -> str:
        plugin_dir = os.path.join("plugins", name)
        if not os.path.exists(plugin_dir):
            return f"插件目录 {plugin_dir} 不存在，请先执行 scaffold"

        if not os.path.exists(os.path.join(plugin_dir, "plugin.json")):
            return f"缺少 plugin.json，无法安装"

        if not os.path.exists(os.path.join(plugin_dir, "__init__.py")):
            return f"缺少 __init__.py，无法安装"

        # Trigger plugin reload via import
        import sys
        plugins_parent = os.path.abspath("plugins")
        if plugins_parent not in sys.path:
            sys.path.insert(0, plugins_parent)

        mod_name = _to_python_module(name)
        try:
            # Try importing the plugin module
            import importlib
            full_mod = f"plugins.{name}.{mod_name}"
            # Actually the module name is just the mod_name within plugins package
            # First try direct import
            spec = importlib.util.spec_from_file_location(
                mod_name, os.path.join(plugin_dir, "__init__.py")
            )
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                if hasattr(mod, "init_plugin"):
                    return f"插件 {name} 代码验证通过。重启服务后生效（或重新扫描 `/api/plugins/scan`）。"
                return f"插件 {name} 缺少 init_plugin 函数"
        except Exception as e:
            return f"插件 {name} 加载失败: {e}"

    def execute(self, action: str, **kwargs) -> str:
        try:
            name = kwargs.get("plugin_name", "").strip()
            if action == "scaffold":
                return self._scaffold(**kwargs)
            elif action == "install":
                return self._install(name)
            else:
                return f"未知操作: {action}"
        except Exception as e:
            return f"操作失败: {e}"
