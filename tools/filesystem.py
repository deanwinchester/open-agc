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
                        },
                        "offset": {
                            "type": "integer",
                            "description": "Optional. The line number to start reading from (1-indexed). Defaults to 1."
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Optional. The maximum number of lines to read. Defaults to reading the whole file (up to a reasonable limit)."
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
                    agent_ctx = kwargs.get("_agent_context")
                    if agent_ctx and getattr(agent_ctx, "sandbox_dir", None):
                        sandbox_dir = agent_ctx.sandbox_dir
                    else:
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
                lines = f.readlines()
                
            offset = kwargs.get("offset", 1)
            limit = kwargs.get("limit")
            
            # 1-indexed to 0-indexed
            start_idx = max(0, offset - 1)
            end_idx = min(len(lines), start_idx + limit) if limit is not None else len(lines)
            
            subset = lines[start_idx:end_idx]
            
            # Format with line numbers (cat -n style)
            formatted_lines = []
            for i, line in enumerate(subset, start=start_idx + 1):
                formatted_lines.append(f"{i:4d} | {line.rstrip('\\n')}")
                
            content = "\n".join(formatted_lines)
            header = f"--- Content of {path} (Lines {start_idx + 1} to {end_idx} of {len(lines)}) ---"
            return f"{header}\n{content}"
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
                    agent_ctx = kwargs.get("_agent_context")
                    if agent_ctx and getattr(agent_ctx, "sandbox_dir", None):
                        sandbox_dir = agent_ctx.sandbox_dir
                    else:
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

class EditFileTool(BaseTool):
    name: str = "edit_file"
    description: str = "Performs exact string replacements in a file. Best for localized edits. You must have read the file recently before editing."

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
                            "description": "The absolute or relative path to the file to edit."
                        },
                        "old_string": {
                            "type": "string",
                            "description": "The exact string to replace. Must be unique in the file to succeed unless replace_all is true. Do NOT include the line number prefixes (e.g. ' 12 | ') from the read tool output."
                        },
                        "new_string": {
                            "type": "string",
                            "description": "The string to replace old_string with. Preserve correct indentation."
                        },
                        "replace_all": {
                            "type": "boolean",
                            "description": "If true, replaces all occurrences of old_string. If false (default), old_string must be unique in the file."
                        }
                    },
                    "required": ["path", "old_string", "new_string"]
                }
            }
        }

    def execute(self, **kwargs) -> str:
        import json
        from core.paths import get_data_path
        
        path = kwargs.get("path")
        old_string = kwargs.get("old_string")
        new_string = kwargs.get("new_string")
        replace_all = kwargs.get("replace_all", False)
        
        if not path or not old_string or new_string is None:
            return "Error: Missing required arguments (path, old_string, new_string)."
            
        # Sandbox Mode Enforcement
        config_path = get_data_path("config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                
                if config.get("sandbox_mode", True):
                    agent_ctx = kwargs.get("_agent_context")
                    if agent_ctx and getattr(agent_ctx, "sandbox_dir", None):
                        sandbox_dir = agent_ctx.sandbox_dir
                    else:
                        sandbox_dir = config.get("sandbox_dir", os.path.abspath(os.path.join(os.getcwd(), "workspace")))
                    abs_path = os.path.abspath(path)
                    
                    if os.path.commonpath([sandbox_dir, abs_path]) != sandbox_dir:
                        return f"Sandbox Security Error: Edit access to path '{path}' is denied. It is outside the permitted sandbox directory ({sandbox_dir})."
            except Exception as e:
                pass
                
        if not os.path.exists(path):
            return f"Error: File '{path}' does not exist."
            
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
                
            occurrences = content.count(old_string)
            if occurrences == 0:
                return f"Error: 'old_string' not found in file '{path}'. Make sure you copied it exactly, without the line number prefixes."
                
            if occurrences > 1 and not replace_all:
                return f"Error: 'old_string' appears {occurrences} times in the file. Please provide a more unique old_string (include more context lines) or set replace_all=true."
                
            new_content = content.replace(old_string, new_string)
            
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)
                
            msg = f"Successfully edited {path}."
            if replace_all:
                msg += f" Replaced {occurrences} occurrences."
            return msg
        except Exception as e:
            return f"Error editing file {path}: {str(e)}"
