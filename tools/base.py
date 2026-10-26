from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
import inspect
import os


class SandboxBlocked(Exception):
    """Raised when a tool tries to access a path outside the sandbox and not yet authorized."""
    def __init__(self, path: str, sandbox_dir: str = "", tool_name: str = "",
                 category: str = "", description: str = ""):
        # Only apply abspath to filesystem paths, not URLs
        if sandbox_dir in ("network", "permission") or path.startswith(("http://", "https://", "ftp://")):
            self.path = path
        else:
            self.path = os.path.abspath(path)
        self.sandbox_dir = sandbox_dir
        self.tool_name = tool_name
        self.category = category
        self.description = description
        super().__init__(f"Sandbox blocked: {path}")


class BaseTool(BaseModel):
    """
    Base class for all tools in Open-AGC.
    """
    name: str = Field(description="The name of the tool, matching function calling schema.")
    description: str = Field(description="A clear description of what the tool does.")

    def get_openai_schema(self) -> Dict[str, Any]:
        raise NotImplementedError("Subclasses must implement get_openai_schema")

    def execute(self, **kwargs) -> Any:
        raise NotImplementedError("Subclasses must implement execute")

    @staticmethod
    def check_sandbox(path: str, sandbox_dir: str = None,
                      config: dict = None, tool_name: str = "",
                      session_whitelist: set = None) -> None:
        """Check whether a path is allowed. Raises SandboxBlocked if denied.

        Allowed: inside sandbox_dir, or in config.allowed_paths,
        or in session_whitelist (one-time approvals).
        Denied always: paths in config.denied_paths.
        """
        if not sandbox_dir and not config:
            return

        import json
        from api.deliverables_registry import resolve_under_root
        if sandbox_dir is None:
            from core.paths import resolve_sandbox_dir
            sandbox_dir = resolve_sandbox_dir(config.get("sandbox_dir"))

        # Allow paths directly under sandbox_dir, resolving links before checking.
        if resolve_under_root(sandbox_dir, path) is not None:
            return

        # Deny paths in the explicit denied_paths list
        denied_paths = config.get("denied_paths", []) if config else []
        if isinstance(denied_paths, str):
            try:
                denied_paths = json.loads(denied_paths)
            except Exception:
                denied_paths = []
        for dp in denied_paths:
            if dp and resolve_under_root(os.path.expandvars(dp), path) is not None:
                raise SandboxBlocked(path, sandbox_dir, tool_name)

        # Allow paths in the explicit allowed_paths list
        allowed_paths = config.get("allowed_paths", []) if config else []
        if isinstance(allowed_paths, str):
            try:
                allowed_paths = json.loads(allowed_paths)
            except Exception:
                allowed_paths = []
        for ap in allowed_paths:
            if ap and resolve_under_root(os.path.expandvars(ap), path) is not None:
                return

        # 应用自身技能目录（data/skills，沙箱外）默认放行：agent 读技能结构/
        # 新建技能都要动这里（生产实证：exe 安装版新建技能时 read_file 技能
        # 目录被拦弹授权框——目录是本应用数据区，放行无安全风险）
        try:
            from core.paths import get_skills_dir
            if resolve_under_root(get_skills_dir(), path) is not None:
                return
        except (ValueError, OSError):
            pass
        except Exception:
            pass

        # Allow paths in the session whitelist (one-time approvals)
        if session_whitelist:
            for wp in session_whitelist:
                if resolve_under_root(os.path.expandvars(wp), path) is not None:
                    return

        # Not in sandbox, not in allowed_paths, not in whitelist → block
        import sys as _sys
        print(f"[Sandbox] BLOCKED: path={repr(path)} whitelist={repr(list(session_whitelist or []))} "
              f"allowed_paths={repr(config.get('allowed_paths', []) if config else [])}",
              file=_sys.stderr, flush=True)
        raise SandboxBlocked(path, sandbox_dir, tool_name)
