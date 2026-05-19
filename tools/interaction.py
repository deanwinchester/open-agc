from typing import Any, Dict, List, Optional
from pydantic import Field
from tools.base import BaseTool

class AskUserQuestionTool(BaseTool):
    name: str = "ask_user_question"
    description: str = (
        "Ask the user a question to clarify requirements, request permission, or get missing information. "
        "Use this tool whenever you are unsure how to proceed or need explicit user confirmation. "
        "The agent will pause execution until the user answers."
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
                        "question_text": {
                            "type": "string",
                            "description": "The exact question to display to the user."
                        },
                        "options": {
                            "type": "array",
                            "items": {
                                "type": "string"
                            },
                            "description": "(Optional) A list of predefined options for the user to choose from. Leave empty if you want a free-text answer."
                        }
                    },
                    "required": ["question_text"]
                }
            }
        }

    def execute(self, **kwargs) -> str:
        agent_ctx = kwargs.get("_agent_context")
        if not agent_ctx:
            return "Error: Cannot ask user because agent context is missing."
            
        question_text = kwargs.get("question_text")
        options = kwargs.get("options")
        
        # This will block the thread until the user replies via WebSocket
        answer = agent_ctx.wait_for_user_input(question_text, options)

        return f"User replied: {answer}"


class TaskPaused(Exception):
    """Raised when the agent voluntarily pauses itself for background tasks."""
    def __init__(self, reason: str = "", pid: int = None, output_file: str = ""):
        self.reason = reason
        self.pid = pid
        self.output_file = output_file
        super().__init__(f"Task paused: {reason}")


class PauseAndWaitTool(BaseTool):
    name: str = "pause_and_wait"
    description: str = (
        "暂停当前任务并等待后台进程完成。用于长时间运行的命令（下载模型、安装依赖、训练模型等）。"
        "系统会保存当前上下文，在后台任务完成后自动恢复执行。"
    )

    def execute(self, reason: str = "", pid: int = None,
                output_file: str = "", description: str = "", **kwargs) -> str:
        raise TaskPaused(
            reason=reason or description or "任务进入后台",
            pid=pid,
            output_file=output_file
        )

    def get_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "pause_and_wait",
                "description": (
                    "Pause the current task and yield to background execution. "
                    "Use this when a command returns [Still Running] (long downloads, "
                    "model training, package installation). The system will save context "
                    "and automatically resume when the background task completes."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": "Brief description of the background task for the user."
                        },
                        "pid": {
                            "type": "integer",
                            "description": "PID of the background process to monitor."
                        },
                        "output_file": {
                            "type": "string",
                            "description": "Path to the output file for reading results on resume."
                        }
                    },
                    "required": ["reason"]
                }
            }
        }


class SearchHistoryTool(BaseTool):
    """Search the agent's conversation history for URLs, tool calls, and past results."""
    name: str = "search_history"
    description: str = (
        "检索当前会话历史中的工具调用记录和结果。用于查找之前获取的 URL、文件路径、"
        "下载链接等数据，避免重复浏览或搜索。当用户要求重试或再下载时，先用此工具检查历史。"
    )

    def execute(self, query: str = "", search_type: str = "all", **kwargs) -> str:
        agent_ctx = kwargs.get("_agent_context")
        if not agent_ctx:
            return "Error: Cannot search history without agent context."

        messages = getattr(agent_ctx, 'messages', [])
        if not messages:
            return "No conversation history available."

        results = []
        q_lower = query.lower() if query else ""

        for i, msg in enumerate(messages):
            # Search tool_call arguments
            if search_type in ("all", "tool_calls") and msg.get("role") == "assistant":
                tcs = msg.get("tool_calls", [])
                for tc in tcs:
                    if isinstance(tc, dict):
                        fn = tc.get("function", {})
                        name = fn.get("name", "")
                        args_str = fn.get("arguments", "{}")
                        if not q_lower or q_lower in args_str.lower() or q_lower in name.lower():
                            try:
                                import json
                                args = json.loads(args_str) if isinstance(args_str, str) else args_str
                            except Exception:
                                args = {}
                            url = args.get("url", "")
                            filename = args.get("filename", "")
                            path = args.get("path", "")
                            results.append(
                                f"[{name}] url={url} filename={filename} path={path} "
                                f"query={args.get('query','')[:80]}"
                            )

            # Search tool results
            if search_type in ("all", "results") and msg.get("role") == "tool":
                content = str(msg.get("content", ""))
                name = msg.get("name", "")
                if not q_lower or q_lower in content.lower() or q_lower in name.lower():
                    # Extract URLs from content
                    import re
                    urls = re.findall(r'(?:https?|ftp)://[^\s\'"<>]+', content)
                    preview = content[:300].replace('\n', ' ')
                    results.append(
                        f"[{name} result] urls={urls[:3]} preview={preview}"
                    )

            if len(results) >= 20:
                break

        if not results:
            return f"No matching history found for '{query}'. Try a different search term."

        return "Found in conversation history:\n" + "\n".join(results[-15:])

    def get_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "search_history",
                "description": (
                    "Search current conversation history for URLs, tool calls, file paths, "
                    "download links, and past results. Use this BEFORE re-browsing or re-searching "
                    "when the user asks you to retry or re-download. It finds data you've already obtained."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search term (e.g., movie name, URL keyword, filename)."
                        },
                        "search_type": {
                            "type": "string",
                            "enum": ["all", "tool_calls", "results"],
                            "description": "What to search: 'all' (default), 'tool_calls' (arguments only), 'results' (output only)."
                        }
                    },
                    "required": ["query"]
                }
            }
        }
