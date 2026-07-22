import os
import glob
import subprocess
from typing import Any, Dict, List, Optional, Tuple
from tools.base import BaseTool, SandboxBlocked

class GlobTool(BaseTool):
    name: str = "find_files"
    description: str = "按 glob 模式快速查找文件路径。按文件名找文件时用它；按内容查找用 search_file_content。受沙箱限制。"

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
                            "description": "glob 模式，如 '**/*.py'。"
                        },
                        "path": {
                            "type": "string",
                            "description": "起始目录，默认当前目录。"
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
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                if config.get("sandbox_mode", True):
                    whitelist = kwargs.get("_session_whitelist", None)
                    self.check_sandbox(base_path, config=config, session_whitelist=whitelist)
            except SandboxBlocked:
                raise
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

def _grep_target_files(target_path: str, include: Optional[str]) -> List[str]:
    """Collect files to search (single file or recursive walk with glob filter)."""
    if os.path.isfile(target_path):
        return [target_path]
    files: List[str] = []
    for root, _, names in os.walk(target_path):
        for name in names:
            if include and not glob.fnmatch.fnmatch(name, include):
                continue
            files.append(os.path.join(root, name))
    return files

def _read_text_lines(file_path: str) -> Optional[List[str]]:
    """Return file lines without line endings; None for binary/unreadable files."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read().splitlines()
    except (UnicodeDecodeError, OSError):
        return None

def _cap_lines(lines: List[str], head_limit: int) -> str:
    """Cap output at head_limit lines, annotating truncation (both search paths)."""
    if len(lines) > head_limit:
        hidden = len(lines) - head_limit
        return "\n".join(lines[:head_limit]) + \
            f"\n... [Truncated: {hidden} more lines not shown; raise head_limit to see more]"
    return "\n".join(lines)

class GrepSearchTool(BaseTool):
    name: str = "search_file_content"
    description: str = "按 regex 搜索文件内容，支持上下文行与统计模式。按内容定位时用；找文件用 find_files。受沙箱限制。"

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
                            "description": "regex 搜索模式。"
                        },
                        "path": {
                            "type": "string",
                            "description": "目录或文件，默认当前目录。"
                        },
                        "include": {
                            "type": "string",
                            "description": "文件名过滤 glob，如 '*.py'。"
                        },
                        "context_lines": {
                            "type": "integer",
                            "description": "匹配行前后各带 N 行上下文，默认 0。"
                        },
                        "output_mode": {
                            "type": "string",
                            "enum": ["content", "files_with_matches", "count"],
                            "description": "输出模式：content 行内容（默认）| files_with_matches 仅文件 | count 各文件匹配数。"
                        },
                        "head_limit": {
                            "type": "integer",
                            "description": "返回行数上限，默认 50；超出截断并标注。"
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
        output_mode = kwargs.get("output_mode", "content")
        
        if not pattern:
            return "Error: No pattern provided."
        if output_mode not in ("content", "files_with_matches", "count"):
            return (f"Error: Invalid output_mode '{output_mode}'. "
                    "Use content | files_with_matches | count.")
        try:
            context_lines = max(0, int(kwargs.get("context_lines", 0) or 0))
        except (TypeError, ValueError):
            return "Error: context_lines must be an integer."
        try:
            head_limit = max(1, int(kwargs.get("head_limit", 50) or 50))
        except (TypeError, ValueError):
            return "Error: head_limit must be an integer."
            
        # Sandbox Mode Enforcement
        config_path = get_data_path("config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                if config.get("sandbox_mode", True):
                    whitelist = kwargs.get("_session_whitelist", None)
                    self.check_sandbox(target_path, config=config, session_whitelist=whitelist)
            except SandboxBlocked:
                raise
            except Exception:
                pass
                
        # Try ripgrep first
        rg_path = shutil.which("rg")
        if rg_path:
            cmd = [rg_path, "-n", "--with-filename"]
            if output_mode == "files_with_matches":
                cmd.append("--files-with-matches")
            elif output_mode == "count":
                cmd.append("--count")
            elif context_lines > 0:
                cmd.extend(["-C", str(context_lines)])
            if include:
                cmd.extend(["-g", include])
            cmd.extend(["--", pattern, target_path])
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if result.returncode == 0:
                    return _cap_lines(result.stdout.splitlines(), head_limit)
                elif result.returncode == 1:
                    return f"No matches found for '{pattern}'."
                else:
                    return f"ripgrep error: {result.stderr}"
            except subprocess.TimeoutExpired:
                return "Error: Search timed out."
            except Exception:
                pass # fallback
                
        # Fallback to python grep (same semantics as the ripgrep path:
        # match lines 'file:N:text', context lines 'file-N-text',
        # non-contiguous context groups separated by '--').
        import re
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"Error: Invalid regex '{pattern}': {e}"
        try:
            files_to_search = _grep_target_files(target_path, include)
            out_lines: List[str] = []
            
            if output_mode == "files_with_matches":
                for fp in files_to_search:
                    lines = _read_text_lines(fp)
                    if lines is None:
                        continue
                    if any(regex.search(line) for line in lines):
                        out_lines.append(fp)
            elif output_mode == "count":
                for fp in files_to_search:
                    lines = _read_text_lines(fp)
                    if lines is None:
                        continue
                    n = sum(1 for line in lines if regex.search(line))
                    if n:
                        out_lines.append(f"{fp}:{n}")
            else:
                first_group = True
                for fp in files_to_search:
                    lines = _read_text_lines(fp)
                    if lines is None:
                        continue
                    match_idx = [i for i, line in enumerate(lines) if regex.search(line)]
                    if not match_idx:
                        continue
                    if context_lines > 0:
                        match_set = set(match_idx)
                        # Merge overlapping/adjacent context windows (rg -C semantics)
                        groups: List[List[int]] = []
                        for i in match_idx:
                            s = max(0, i - context_lines)
                            e = min(len(lines) - 1, i + context_lines)
                            if groups and s <= groups[-1][1] + 1:
                                groups[-1][1] = max(groups[-1][1], e)
                            else:
                                groups.append([s, e])
                        for s, e in groups:
                            if not first_group:
                                out_lines.append("--")
                            first_group = False
                            for i in range(s, e + 1):
                                sep = ":" if i in match_set else "-"
                                out_lines.append(f"{fp}{sep}{i + 1}{sep}{lines[i]}")
                    else:
                        for i in match_idx:
                            out_lines.append(f"{fp}:{i + 1}:{lines[i]}")
                            
            if not out_lines:
                return f"No matches found for '{pattern}'."
            return _cap_lines(out_lines, head_limit)
        except Exception as e:
            return f"Error executing search_file_content: {str(e)}"
