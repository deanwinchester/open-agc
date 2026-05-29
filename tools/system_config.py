"""Tool for the agent to read/write system configuration, manage MCP, plugins, and skills."""
import json
import os
from typing import Any, Dict, Optional

from tools.base import BaseTool
from core.paths import get_data_path

CONFIG_PATH = get_data_path("config.json")


def _load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_config(config: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def _mask_keys(api_keys: dict) -> dict:
    masked = {}
    for k, v in api_keys.items():
        if v and len(v) > 6:
            masked[k] = f"{v[:3]}...{v[-3:]}"
        elif v:
            masked[k] = "***"
        else:
            masked[k] = ""
    return masked


class ConfigureSystemTool(BaseTool):
    """Read and modify system configuration, manage MCP servers, plugins, agent profiles, and skills."""

    name: str = "configure_system"
    description: str = "配置系统自身：读取/修改配置、管理 MCP 服务器、安装/卸载插件、管理 Agent 配置档、管理技能。"

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
                            "enum": [
                                "get_settings",
                                "update_setting",
                                "get_mcp_servers",
                                "add_mcp_server",
                                "remove_mcp_server",
                                "list_plugins",
                                "install_plugin",
                                "remove_plugin",
                                "toggle_plugin",
                                "list_agent_profiles",
                                "create_agent_profile",
                                "update_agent_profile",
                                "delete_agent_profile",
                                "list_skills",
                                "import_skill",
                                "delete_skill",
                            ],
                            "description": "要执行的操作",
                        },
                        "key": {
                            "type": "string",
                            "description": "update_setting 时使用的配置键名（如 default_model、sandbox_mode 等）",
                        },
                        "value": {
                            "type": "string",
                            "description": "update_setting 时使用的配置值（JSON 格式，如 \"gpt-4o\"、true、false、\"[]\"）",
                        },
                        "mcp_server_name": {
                            "type": "string",
                            "description": "MCP 服务器名称（add/remove 时使用）",
                        },
                        "mcp_command": {
                            "type": "string",
                            "description": "MCP 服务器启动命令",
                        },
                        "mcp_args": {
                            "type": "string",
                            "description": "MCP 服务器参数（JSON 字符串数组）",
                        },
                        "plugin_name": {
                            "type": "string",
                            "description": "插件名称",
                        },
                        "plugin_repo_url": {
                            "type": "string",
                            "description": "插件 git 仓库地址",
                        },
                        "profile_name": {
                            "type": "string",
                            "description": "Agent 配置档名称",
                        },
                        "profile_data": {
                            "type": "string",
                            "description": "Agent 配置档数据（JSON 字符串，含 prompt/model/temperature/max_tokens）",
                        },
                        "skill_filename": {
                            "type": "string",
                            "description": "技能文件名（以 .md 结尾）",
                        },
                        "skill_content": {
                            "type": "string",
                            "description": "技能 Markdown 全文",
                        },
                    },
                    "required": ["action"],
                },
            },
        }

    def _get_settings(self) -> str:
        config = _load_config()
        keys = _mask_keys(config.get("api_keys", {}))
        lines = ["## 当前系统配置", f"API Keys: {json.dumps(keys, ensure_ascii=False)}"]
        skip = {"api_keys"}
        for k, v in config.items():
            if k in skip:
                continue
            lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
        lines.append(f"\n配置文件路径: {CONFIG_PATH}")
        return "\n".join(lines)

    def _update_setting(self, key: str, value: str) -> str:
        config = _load_config()
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = value
        config[key] = parsed
        _save_config(config)
        return f"已更新配置 {key} = {json.dumps(parsed, ensure_ascii=False)}"

    def _get_mcp_servers(self) -> str:
        config = _load_config()
        servers = config.get("mcp_servers", {})
        if not servers:
            return "暂无 MCP 服务器配置"
        lines = ["## MCP 服务器列表"]
        for name, cfg in servers.items():
            lines.append(f"- {name}: command={cfg.get('command')}, args={cfg.get('args', [])}")
        return "\n".join(lines)

    def _add_mcp_server(self, name: str, command: str, args: str = "[]") -> str:
        config = _load_config()
        servers = config.get("mcp_servers", {})
        parsed_args = json.loads(args) if args else []
        servers[name] = {"command": command, "args": parsed_args}
        config["mcp_servers"] = servers
        _save_config(config)
        return f"MCP 服务器 {name} 已添加（command: {command}）"

    def _remove_mcp_server(self, name: str) -> str:
        config = _load_config()
        servers = config.get("mcp_servers", {})
        if name not in servers:
            return f"MCP 服务器 {name} 不存在"
        del servers[name]
        config["mcp_servers"] = servers
        _save_config(config)
        return f"MCP 服务器 {name} 已移除"

    def _list_plugins(self) -> str:
        from core.plugin_manager import discover_plugins
        plugins = discover_plugins(plugins_dir="plugins")
        if not plugins:
            return "暂无已安装的插件"
        lines = ["## 已安装插件"]
        for p in plugins:
            status = "✅ 已启用" if p.instance else "⏸️ 已禁用"
            lines.append(f"- {p.name} v{p.version} {status} — {p.description}")
        return "\n".join(lines)

    def _install_plugin(self, name: str, repo_url: str) -> str:
        from core.plugin_manager import install_from_git
        ok = install_from_git(name, repo_url)
        if ok:
            return f"插件 {name} 安装成功（需要重启服务后生效）"
        return f"插件 {name} 安装失败（可能已存在或仓库地址不正确）"

    def _remove_plugin(self, name: str) -> str:
        import shutil
        target = os.path.join("plugins", name)
        if not os.path.exists(target):
            return f"插件目录 {target} 不存在"
        shutil.rmtree(target)
        return f"插件 {name} 已删除"

    def _toggle_plugin(self, name: str) -> str:
        from core.plugin_manager import toggle_plugin
        result = toggle_plugin(name)
        enabled = result.get("enabled", False)
        status = "已启用" if enabled else "已禁用"
        return f"插件 {name} {status}"

    def _list_agent_profiles(self) -> str:
        config = _load_config()
        agents = config.get("agent_profiles", [])
        if isinstance(agents, str):
            agents = json.loads(agents) if agents else []
        if not agents:
            return "暂无 Agent 配置档"
        lines = ["## Agent 配置档"]
        for a in agents:
            lines.append(f"- {a.get('name')}: model={a.get('model','')} | temperature={a.get('temperature','')}")
        return "\n".join(lines)

    def _create_agent_profile(self, name: str, data: str) -> str:
        config = _load_config()
        agents = config.get("agent_profiles", [])
        if isinstance(agents, str):
            agents = json.loads(agents) if agents else []
        if any(a.get("name") == name for a in agents):
            return f"配置档 {name} 已存在"
        profile = json.loads(data)
        profile["name"] = name
        agents.append(profile)
        config["agent_profiles"] = agents
        _save_config(config)
        return f"Agent 配置档 {name} 已创建"

    def _update_agent_profile(self, name: str, data: str) -> str:
        config = _load_config()
        agents = config.get("agent_profiles", [])
        if isinstance(agents, str):
            agents = json.loads(agents) if agents else []
        updates = json.loads(data)
        for a in agents:
            if a.get("name") == name:
                a.update(updates)
                config["agent_profiles"] = agents
                _save_config(config)
                return f"Agent 配置档 {name} 已更新"
        return f"配置档 {name} 不存在"

    def _delete_agent_profile(self, name: str) -> str:
        config = _load_config()
        agents = config.get("agent_profiles", [])
        if isinstance(agents, str):
            agents = json.loads(agents) if agents else []
        agents = [a for a in agents if a.get("name") != name]
        config["agent_profiles"] = agents
        _save_config(config)
        return f"Agent 配置档 {name} 已删除"

    def _list_skills(self) -> str:
        from core.skill_manager import SkillManager
        manager = SkillManager()
        skills = manager.list_skills()
        if not skills:
            return "暂无技能"
        lines = ["## 技能列表"]
        for s in skills:
            lines.append(f"- {s.get('filename')}: {s.get('title', '')}")
        return "\n".join(lines)

    def _import_skill(self, filename: str, content: str) -> str:
        from core.skill_manager import SkillManager
        from core.skill_store import SkillStore
        manager = SkillManager()
        result = manager.import_skill(filename, content, force=True)
        if result["success"]:
            try:
                SkillStore().build_index()
            except Exception:
                pass
            return f"技能 {filename} 导入成功"
        return f"技能导入失败: {result.get('message', '')}"

    def _delete_skill(self, filename: str) -> str:
        from core.skill_store import SkillStore
        from core.paths import get_skills_dir
        path = os.path.join(get_skills_dir(), filename)
        if not os.path.exists(path):
            return f"技能 {filename} 不存在"
        os.remove(path)
        try:
            SkillStore().build_index()
        except Exception:
            pass
        return f"技能 {filename} 已删除"

    def execute(self, action: str, **kwargs) -> str:
        try:
            if action == "get_settings":
                return self._get_settings()
            elif action == "update_setting":
                return self._update_setting(kwargs.get("key", ""), kwargs.get("value", ""))
            elif action == "get_mcp_servers":
                return self._get_mcp_servers()
            elif action == "add_mcp_server":
                return self._add_mcp_server(
                    kwargs.get("mcp_server_name", ""),
                    kwargs.get("mcp_command", ""),
                    kwargs.get("mcp_args", "[]"),
                )
            elif action == "remove_mcp_server":
                return self._remove_mcp_server(kwargs.get("mcp_server_name", ""))
            elif action == "list_plugins":
                return self._list_plugins()
            elif action == "install_plugin":
                return self._install_plugin(
                    kwargs.get("plugin_name", ""), kwargs.get("plugin_repo_url", "")
                )
            elif action == "remove_plugin":
                return self._remove_plugin(kwargs.get("plugin_name", ""))
            elif action == "toggle_plugin":
                return self._toggle_plugin(kwargs.get("plugin_name", ""))
            elif action == "list_agent_profiles":
                return self._list_agent_profiles()
            elif action == "create_agent_profile":
                return self._create_agent_profile(
                    kwargs.get("profile_name", ""), kwargs.get("profile_data", "{}")
                )
            elif action == "update_agent_profile":
                return self._update_agent_profile(
                    kwargs.get("profile_name", ""), kwargs.get("profile_data", "{}")
                )
            elif action == "delete_agent_profile":
                return self._delete_agent_profile(kwargs.get("profile_name", ""))
            elif action == "list_skills":
                return self._list_skills()
            elif action == "import_skill":
                return self._import_skill(
                    kwargs.get("skill_filename", ""), kwargs.get("skill_content", "")
                )
            elif action == "delete_skill":
                return self._delete_skill(kwargs.get("skill_filename", ""))
            else:
                return f"未知操作: {action}"
        except Exception as e:
            return f"操作失败: {e}"
