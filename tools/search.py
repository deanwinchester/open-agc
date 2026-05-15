import os
import glob
import subprocess
from typing import Any, Dict
from tools.base import BaseTool

class GlobTool(BaseTool):
    name: str = "find_files"
    description: str = "Fast file pattern matching tool that works with any codebase size. Supports glob patterns like '**/*.py' or 'src/**/*.ts'. Returns matching file paths."

    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "The glob pattern to search for (e.g., '**/*.py', 'src/**/*.js')."
                        },
                        "path": {
                            "type": "string",
                            "description": "The base directory to start searching from. Defaults to the current working directory."
                        }
                    },
                    "required": ["pattern"]
                }
            }
        }

    def execute(self, **kwargs) -> str:
        import json
        from core.paths import get_data_path
        
        pattern = kwargs.get("pattern")
        base_path = kwargs.get("path", os.getcwd())
        
        if not pattern:
            return "Error: No pattern provided."
            
        # Sandbox Mode Enforcement
        config_path = get_data_path("config.json")
        sandbox_dir = base_path
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                if config.get("sandbox_mode", True):
                    sandbox_dir = config.get("sandbox_dir", os.path.abspath(os.path.join(os.getcwd(), "workspace")))
                    abs_path = os.path.abspath(base_path)
                    if os.path.commonpath([sandbox_dir, abs_path]) != sandbox_dir:
                        return f"Sandbox Security Error: Search access to path '{base_path}' is denied. It is outside the permitted sandbox directory ({sandbox_dir})."
            except Exception:
                pass

        try:
            # Change to the target directory to make globbing easier, then change back
            original_dir = os.getcwd()
            os.chdir(base_path)
            
            # Use recursive glob
            matches = glob.glob(pattern, recursive=True)
            
            os.chdir(original_dir)
            
            if not matches:
                return f"No files matched the pattern '{pattern}' in {base_path}."
                
            # Filter out directories
            files_only = [m for m in matches if os.path.isfile(os.path.join(base_path, m))]
            
            result = f"Found {len(files_only)} matching files in {base_path}:\n"
            result += "\n".join(files_only[:100])
            if len(files_only) > 100:
                result += f"\n... and {len(files_only) - 100} more files."
                
            return result
        except Exception as e:
            if 'original_dir' in locals():
                os.chdir(original_dir)
            return f"Error executing find_files: {str(e)}"

class GrepSearchTool(BaseTool):
    name: str = "search_file_content"
    description: str = "A powerful search tool to find content within files. Uses ripgrep if available, falling back to Python's re module. Supports regex."

    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "The regex pattern to search for."
                        },
                        "path": {
                            "type": "string",
                            "description": "The directory or file to search within. Defaults to current directory."
                        },
                        "include": {
                            "type": "string",
                            "description": "Optional glob pattern to filter files (e.g., '*.py')."
                        }
                    },
                    "required": ["pattern"]
                }
            }
        }

    def execute(self, **kwargs) -> str:
        import json
        from core.paths import get_data_path
        import shutil
        
        pattern = kwargs.get("pattern")
        target_path = kwargs.get("path", os.getcwd())
        include = kwargs.get("include")
        
        if not pattern:
            return "Error: No pattern provided."
            
        # Sandbox Mode Enforcement
        config_path = get_data_path("config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                if config.get("sandbox_mode", True):
                    sandbox_dir = config.get("sandbox_dir", os.path.abspath(os.path.join(os.getcwd(), "workspace")))
                    abs_path = os.path.abspath(target_path)
                    if os.path.commonpath([sandbox_dir, abs_path]) != sandbox_dir:
                        return f"Sandbox Security Error: Search access to path '{target_path}' is denied."
            except Exception:
                pass
                
        # Try ripgrep first
        rg_path = shutil.which("rg")
        if rg_path:
            cmd = [rg_path, "-n", pattern, target_path]
            if include:
                cmd.extend(["-g", include])
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if result.returncode == 0:
                    return result.stdout[:8000] + ("\n... [Truncated]" if len(result.stdout) > 8000 else "")
                elif result.returncode == 1:
                    return f"No matches found for '{pattern}'."
                else:
                    return f"ripgrep error: {result.stderr}"
            except subprocess.TimeoutExpired:
                return "Error: Search timed out."
            except Exception as e:
                pass # fallback
                
        # Fallback to python grep (if ripgrep not available)
        import re
        try:
            regex = re.compile(pattern)
            matches = []
            
            if os.path.isfile(target_path):
                files_to_search = [target_path]
            else:
                files_to_search = []
                for root, _, files in os.walk(target_path):
                    for file in files:
                        if include and not glob.fnmatch.fnmatch(file, include):
                            continue
                        files_to_search.append(os.path.join(root, file))
                        
            for file_path in files_to_search:
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        for i, line in enumerate(f, 1):
                            if regex.search(line):
                                matches.append(f"{file_path}:{i}:{line.rstrip()}")
                                if len(matches) > 500:
                                    matches.append("... [Too many matches, truncated]")
                                    break
                except UnicodeDecodeError:
                    pass # Skip binary files
                if len(matches) > 500:
                    break
                    
            if not matches:
                return f"No matches found for '{pattern}'."
            return "\n".join(matches)
        except Exception as e:
            return f"Error executing search_file_content: {str(e)}"
