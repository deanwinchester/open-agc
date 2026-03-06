import os
from typing import Any, Dict
from tools.base import BaseTool

class ReadFileTool(BaseTool):
    name: str = "read_file"
    description: str = "Read the contents of a file at a given path."

    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "The absolute or relative path to the file."
                        }
                    },
                    "required": ["path"]
                }
            }
        }

    def execute(self, **kwargs) -> str:
        import json
        from core.paths import get_data_path
        
        path = kwargs.get("path")
        if not path:
            return "Error: No file path provided."
            
        # Sandbox Mode Enforcement
        config_path = get_data_path("config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                
                if config.get("sandbox_mode", True):
                    sandbox_dir = config.get("sandbox_dir", os.path.abspath(os.path.join(os.getcwd(), "workspace")))
                    os.makedirs(sandbox_dir, exist_ok=True)
                    abs_path = os.path.abspath(path)
                    
                    # Ensure path is within sandbox_dir
                    if os.path.commonpath([sandbox_dir, abs_path]) != sandbox_dir:
                        return f"Sandbox Security Error: Access to path '{path}' is denied. It is outside the permitted sandbox directory ({sandbox_dir})."
            except Exception as e:
                print(f"[ReadFileTool] Warning checking sandbox config: {e}")
                
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            return f"--- Content of {path} ---\n{content}"
        except Exception as e:
            return f"Error reading file {path}: {str(e)}"

class WriteFileTool(BaseTool):
    name: str = "write_file"
    description: str = "Write content to a file. Overwrites if it exists, creates if it does not."

    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "The absolute or relative path to the file."
                        },
                        "content": {
                            "type": "string",
                            "description": "The full text content to write to the file."
                        }
                    },
                    "required": ["path", "content"]
                }
            }
        }

    def execute(self, **kwargs) -> str:
        import json
        from core.paths import get_data_path
        
        path = kwargs.get("path")
        content = kwargs.get("content", "")
        if not path:
            return "Error: No file path provided."
            
        # Sandbox Mode Enforcement
        config_path = get_data_path("config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                
                if config.get("sandbox_mode", True):
                    sandbox_dir = config.get("sandbox_dir", os.path.abspath(os.path.join(os.getcwd(), "workspace")))
                    os.makedirs(sandbox_dir, exist_ok=True)
                    abs_path = os.path.abspath(path)
                    
                    # Ensure path is within sandbox_dir
                    if os.path.commonpath([sandbox_dir, abs_path]) != sandbox_dir:
                        return f"Sandbox Security Error: Write access to path '{path}' is denied. It is outside the permitted sandbox directory ({sandbox_dir})."
            except Exception as e:
                print(f"[WriteFileTool] Warning checking sandbox config: {e}")
                
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Successfully written to {path}."
        except Exception as e:
            return f"Error writing file {path}: {str(e)}"
