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
        if sandbox_dir is None:
            sandbox_dir = config.get("sandbox_dir",
                os.path.abspath(os.path.join(os.getcwd(), "workspace")))
        abs_path = os.path.abspath(path)

        # Allow paths directly under sandbox_dir
        try:
            if os.path.commonpath([sandbox_dir, abs_path]) == sandbox_dir:
                return
        except ValueError:
            pass

        # Deny paths in the explicit denied_paths list
        denied_paths = config.get("denied_paths", []) if config else []
        if isinstance(denied_paths, str):
            try:
                denied_paths = json.loads(denied_paths)
            except Exception:
                denied_paths = []
        for dp in denied_paths:
            if not dp:
                continue
            denied_abs = os.path.abspath(os.path.expandvars(dp))
            try:
                if os.path.commonpath([denied_abs, abs_path]) == denied_abs:
                    raise SandboxBlocked(path, sandbox_dir, tool_name)
            except ValueError:
                pass
            if abs_path == denied_abs or abs_path.startswith(denied_abs + os.sep):
                raise SandboxBlocked(path, sandbox_dir, tool_name)

        # Allow paths in the explicit allowed_paths list
        allowed_paths = config.get("allowed_paths", []) if config else []
        if isinstance(allowed_paths, str):
            try:
                allowed_paths = json.loads(allowed_paths)
            except Exception:
                allowed_paths = []
        for ap in allowed_paths:
            if not ap:
                continue
            allowed_abs = os.path.abspath(os.path.expandvars(ap))
            try:
                if os.path.commonpath([allowed_abs, abs_path]) == allowed_abs:
                    return
            except ValueError:
                pass
            if abs_path == allowed_abs or abs_path.startswith(allowed_abs + os.sep):
                return

        # Allow paths in the session whitelist (one-time approvals)
        if session_whitelist:
            for wp in session_whitelist:
                wp_abs = os.path.abspath(os.path.expandvars(wp))
                try:
                    if os.path.commonpath([wp_abs, abs_path]) == wp_abs:
                        return
                except ValueError:
                    pass
                if abs_path == wp_abs or abs_path.startswith(wp_abs + os.sep):
                    return

        # Not in sandbox, not in allowed_paths, not in whitelist → block
        raise SandboxBlocked(path, sandbox_dir, tool_name)
