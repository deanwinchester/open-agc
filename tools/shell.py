import subprocess
import os
from typing import Any, Dict
from pydantic import Field

from tools.base import BaseTool

class ShellTool(BaseTool):
    name: str = "execute_shell"
    description: str = "Execute a bash environment shell command on the local machine."

    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The bash command to execute (e.g., 'ls -la', 'python script.py')"
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Optional timeout in seconds.",
                            "default": 30
                        }
                    },
                    "required": ["command"]
                }
            }
        }

    def execute(self, **kwargs) -> str:
        command = kwargs.get("command")
        if not command:
            return "Error: No command provided."
            
        timeout = kwargs.get("timeout", 60)
        
        try:
            result = subprocess.run(
                command,
                shell=True,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout
            )
            
            output = ""
            if result.stdout:
                output += f"STDOUT:\n{result.stdout}\n"
            if result.stderr:
                output += f"STDERR:\n{result.stderr}\n"
                
            output += f"Exit Code: {result.returncode}"
            return output
            
        except subprocess.TimeoutExpired:
            return f"Error: Command timed out after {timeout} seconds."
        except Exception as e:
            return f"Error executing shell command: {str(e)}"
