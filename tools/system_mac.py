import subprocess
from .base import BaseTool

class MacSystemTool(BaseTool):
    """
    Interact with macOS system natively (Notifications, Clipboard, etc).
    """
    def __init__(self):
        super().__init__(
            name="mac_system_action",
            description="执行 macOS 原生系统操作（通知、剪贴板、系统信息）。仅 macOS 可用。"
        )

    def get_openai_schema(self) -> dict:
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
                            "enum": ["notify", "read_clipboard", "get_system_info"],
                            "description": "notify 发通知；read_clipboard 读剪贴板；get_system_info 系统信息。"
                        },
                        "message": {
                            "type": "string",
                            "description": "通知内容（action=notify 时必填）。"
                        },
                        "title": {
                            "type": "string",
                            "description": "通知标题（可选）。"
                        }
                    },
                    "required": ["action"]
                }
            }
        }

    def execute(self, action: str, message: str = "", title: str = "Panda (Open-AGC)", **kwargs) -> str:
        try:
            if action == "notify":
                if not message:
                    return "Error: message is required for notify action."
                # Use osascript to trigger a native macOS notification
                # Escape double quotes in message and title
                safe_msg = message.replace('"', '\\"')
                safe_title = title.replace('"', '\\"')
                script = f'display notification "{safe_msg}" with title "{safe_title}"'
                subprocess.run(['osascript', '-e', script], check=True)
                return "Notification sent successfully."
            elif action == "read_clipboard":
                result = subprocess.run(['pbpaste'], capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", check=True)
                return result.stdout
            elif action == "get_system_info":
                result = subprocess.run(['sw_vers'], capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", check=True)
                return result.stdout
            else:
                return f"Error: Unknown action {action}."
        except Exception as e:
            return f"Error executing mac system action: {str(e)}"
