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
