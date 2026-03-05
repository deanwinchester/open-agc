import subprocess
import os
import tempfile
from typing import Any, Dict
from tools.base import BaseTool

class PythonREPLTool(BaseTool):
    name: str = "execute_python"
    description: str = "Execute Python code in an isolated environment and return the standard output/error."

    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": "The python code to execute."
                        }
                    },
                    "required": ["code"]
                }
            }
        }

    def execute(self, **kwargs) -> str:
        code = kwargs.get("code")
        if not code:
            return "Error: No python code provided."
            
        # Create a temporary file to run the python code cleanly
        # This helps with multi-line statements and proper traceback
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as temp:
            temp.write(code)
            temp_path = temp.name

        try:
            # Note: For production Open-AGC, this should run in a docker container or restricted environment.
            # Using current python environment for simplicity.
            result = subprocess.run(
                ["python3", temp_path],
                capture_output=True,
                text=True,
                timeout=60
            )
            
            output = ""
            if result.stdout:
                output += f"STDOUT:\n{result.stdout}\n"
            if result.stderr:
                output += f"STDERR:\n{result.stderr}\n"
            
            output += f"Exit Code: {result.returncode}"
            return output
            
        except subprocess.TimeoutExpired:
            return "Error: Python execution timed out after 60 seconds."
        except Exception as e:
            return f"Error executing python code: {str(e)}"
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
