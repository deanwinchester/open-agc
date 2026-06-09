from typing import Any, Dict
from tools.base import BaseTool
import json

class CompactContextTool(BaseTool):
    name: str = "compact_context"
    description: str = "主动折叠和压缩早期的上下文对话历史。当长篇输出或历史任务记录让你感到困惑时，调用此工具清理上下文。"

    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": "为什么要清理上下文（例如：'之前的构建日志太长干扰了现在的注意力'）"
                        }
                    },
                    "required": ["reason"],
                },
            },
        }

    def execute(self, reason: str, _agent_context=None, **kwargs) -> Any:
        if not _agent_context:
            return "执行失败：缺少 Agent 上下文。"

        try:
            original_len = len(_agent_context.messages)
            
            # 使用 force=True 强制执行深度折叠
            if hasattr(_agent_context, "_fold_tool_calls"):
                folded = _agent_context._fold_tool_calls(_agent_context.messages, force=True)
                if len(folded) < original_len:
                    _agent_context.messages = folded
                    return f"系统已成功清理上下文，移除了 {original_len - len(folded)} 条旧消息记录。清理原因：{reason}。现在你可以用更轻量的上下文继续工作。"
                else:
                    return "当前上下文已经很精简，或可折叠的回合数不足，无需进一步压缩。"
            else:
                return "执行失败：Agent 环境不支持 _fold_tool_calls。"
        except Exception as e:
            return f"清理上下文时发生错误: {str(e)}"
