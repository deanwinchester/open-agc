"""Request a credential from the user via the sandbox authorization popup.

This tool never sees plaintext: it raises SandboxBlocked(category='secret'),
the frontend shows a form, the user submits it, ws.py passes the form fields
(in-memory only) into the agent's result_holder, and the agent-side handler
(agent/agent.py::_handle_secret_collection) writes the entry into the local
vault (core.secrets). The LLM only ever receives a {{secret:name}} reference.
"""
from typing import Any, Dict

from tools.base import BaseTool, SandboxBlocked

SECRET_TYPES = ("generic", "api_key", "mongodb", "mysql")


def confirmation_text(name: str, secret_type: str = "generic", host: str = "") -> str:
    """Tool-result text after a successful save — reference only, no plaintext."""
    return (f"已保存为 {{{{secret:{name}}}}}（{secret_type or 'generic'}@{host or '-'}）。"
            f"请用 {{{{secret:{name}.password}}}} 或 {{{{secret:{name}.uri}}}} 引用，"
            f"不要在上下文中包含明文。")


class RequestSecretTool(BaseTool):
    name: str = "request_secret"
    description: str = (
        "向用户收集凭据（密码 / API Key / 数据库账号密码等）并存入本地凭证库。"
        "任务需要凭据且凭证库中没有时使用；凭据由用户在弹窗中填写，"
        "工具返回引用名（{{secret:名称}}），全程不接触明文。"
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
                        "purpose": {
                            "type": "string",
                            "description": "用途说明，展示在弹窗中给用户看（如 '连接生产 MongoDB 导出数据'）。",
                        },
                        "name": {
                            "type": "string",
                            "description": "建议的凭据名称（可选，仅字母/数字/_/-；留空则由用户填写或系统自动生成）。",
                        },
                        "secret_type": {
                            "type": "string",
                            "enum": list(SECRET_TYPES),
                            "description": "凭据类型，默认 generic。",
                        },
                        "host": {
                            "type": "string",
                            "description": "主机/服务地址（可选）。",
                        },
                    },
                    "required": ["purpose"],
                },
            },
        }

    def execute(self, purpose: str = "", name: str = "", secret_type: str = "generic",
                host: str = "", **kwargs) -> str:
        agent_ctx = kwargs.get("_agent_context")

        # Retry path: the user just submitted the popup form and the agent-side
        # handler saved the entry (see _handle_secret_collection) — confirm
        # without prompting again. Carries metadata only, never the password.
        saved = getattr(agent_ctx, "_last_saved_secret", None) if agent_ctx is not None else None
        if isinstance(saved, dict) and saved.get("name"):
            try:
                agent_ctx._last_saved_secret = None
            except Exception:
                pass
            return confirmation_text(saved["name"], saved.get("type"), saved.get("host"))

        # Vault fallback: this name already exists (saved earlier / by another
        # agent instance) — tell the caller to use the reference, don't re-prompt.
        if name:
            try:
                from core.secrets import get_secret
                entry = get_secret(name)
            except Exception:
                entry = None
            if entry:
                return confirmation_text(name, entry.get("type"), entry.get("host"))

        if not (purpose or "").strip():
            return "Error: purpose 必填——请在弹窗中告诉用户该凭据的用途。"

        # sandbox_dir='permission' keeps `path` (the suggested name) free of
        # abspath rewriting; category='secret' routes to the secret form.
        raise SandboxBlocked(
            name or "", sandbox_dir="permission", tool_name=self.name,
            category="secret", description=purpose.strip(),
        )
