from typing import Any, Dict
from tools.base import BaseTool
import json

class CompactContextTool(BaseTool):
    name: str = "compact_context"
    description: str = (
        "用 LLM 把早期对话历史压缩成结构化摘要（含需求、决策、文件修改、待办）。"
        "历史记录过长干扰当前任务时调用；最近消息保留原文。"
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
                        "reason": {
                            "type": "string",
                            "description": "清理原因，如 '之前的构建日志太长干扰了注意力'。"
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

            # Use LLM-based compaction (Claude Code style)
            if hasattr(_agent_context, "_llm_compact_messages"):
                compacted, did_compact = _agent_context._llm_compact_messages(
                    _agent_context.messages
                )
                if did_compact and len(compacted) < original_len:
                    _agent_context.messages = compacted
                    saved = original_len - len(compacted)
                    return (
                        f"✅ 上下文已使用 LLM 总结压缩。\n"
                        f"压缩前：{original_len} 条消息 → 压缩后：{len(compacted)} 条消息\n"
                        f"清理原因：{reason}\n\n"
                        f"早期对话已替换为结构化摘要（含用户需求、关键决策、文件修改、待办事项），"
                        f"最近的消息已保留原文。你可以基于摘要继续工作。"
                    )
                elif hasattr(_agent_context, "_fold_tool_calls"):
                    # Fallback to folding if LLM didn't reduce size
                    folded = _agent_context._fold_tool_calls(
                        _agent_context.messages, force=True
                    )
                    if len(folded) < original_len:
                        _agent_context.messages = folded
                        return (
                            f"系统已使用折叠方式清理上下文，"
                            f"移除了 {original_len - len(folded)} 条旧消息记录。"
                            f"清理原因：{reason}"
                        )
                    else:
                        return "当前上下文已经很精简，无需进一步压缩。"
                else:
                    return "当前上下文已经很精简，无需进一步压缩。"
            else:
                return "执行失败：Agent 环境不支持 _llm_compact_messages。"
        except Exception as e:
            return f"清理上下文时发生错误: {str(e)}"
