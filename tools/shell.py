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
        import json
        from core.paths import get_data_path
        
        command = kwargs.get("command")
        if not command:
            return "Error: No command provided."
            
        timeout = kwargs.get("timeout", 60)
        
        # Sandbox Mode Enforcement
        cwd = None
        config_path = get_data_path("config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                
                if config.get("sandbox_mode", True):
                    sandbox_dir = config.get("sandbox_dir", os.path.abspath(os.path.join(os.getcwd(), "workspace")))
                    os.makedirs(sandbox_dir, exist_ok=True)
                    cwd = sandbox_dir
            except Exception as e:
                print(f"[ShellTool] Warning: failed to load sandbox config: {e}")
        
        try:
            result = subprocess.run(
                command,
                shell=True,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=cwd,
                timeout=timeout
            )
            
            output = ""
            if cwd:
                output += f"[Running in Sandbox: {cwd}]\n"
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
