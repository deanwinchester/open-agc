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
