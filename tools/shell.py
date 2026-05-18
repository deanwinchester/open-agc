import subprocess
import os
import time
import re
import threading
from typing import Any, Dict, Optional, Callable
from pydantic import Field

from tools.base import BaseTool

# Module-level process tracking for interrupt support
_current_process: Optional[subprocess.Popen] = None
_current_process_lock = threading.Lock()


def interrupt_shell() -> bool:
    """Kill the currently running shell process, if any. Returns True if a process was killed."""
    global _current_process
    with _current_process_lock:
        if _current_process is not None and _current_process.poll() is None:
            _current_process.kill()
            _current_process = None
            return True
    return False


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

    @staticmethod
    def _check_dangerous(command: str) -> str:
        """Block commands that would kill the Open-AGC server process itself."""
        cmd_lower = command.lower().strip()
        server_pid = os.getpid()

        # Patterns that kill all python / the server process
        suicidal_patterns = [
            # Windows: taskkill targeting python.exe or the server PID
            (r'taskkill\b.*(?:/im\s+python\b|/im\s+python3?\b)', "禁止终止 python.exe（会杀死 Open-AGC 自身）"),
            (r'taskkill\b.*/pid\s+' + str(server_pid), "禁止终止 Open-AGC 进程自身"),
            (r'tskill\s+python\b', "禁止终止 python 进程"),
            (r'wmic\s+process\s+where.*delete', "禁止通过 WMI 终止进程"),
            # Unix: kill/pkill/killall targeting python or the server PID
            (r'(?:^|[|&;]\s*)(?:sudo\s+)?(?:kill\s+-9\s+' + str(server_pid) + r'|pkill\s+(?:-9\s+)?python|killall\s+(?:-9\s+)?python)', "禁止终止 python 进程（会杀死 Open-AGC 自身）"),
            # Generic: stop/kill the uvicorn or server process
            (r'(?:stop|kill|terminate)\s+(?:uvicorn|open-agc|server)', "禁止停止服务器进程"),
        ]

        for pattern, reason in suicidal_patterns:
            if re.search(pattern, cmd_lower):
                return (
                    f"⛔ 该命令被阻止执行：{reason}\n\n"
                    f"被阻止的命令: {command[:200]}\n"
                    f"如果你需要终止某个特定程序，请使用更精确的进程名或 PID，"
                    f"而非终止全部 python 进程。"
                )

        return ""

    def execute(self, **kwargs) -> str:
        import json
        from core.paths import get_data_path

        command = kwargs.get("command")
        if not command:
            return "Error: No command provided."

        timeout = kwargs.get("timeout", 60)
        interrupt_check: Optional[Callable[[], bool]] = kwargs.get("interrupt_check")

        # ── Self-preservation: block commands that would kill the server ──
        blocked = self._check_dangerous(command)
        if blocked:
            return blocked

        # Sandbox Mode Enforcement
        cwd = None
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
                    cwd = sandbox_dir
            except Exception as e:
                print(f"[ShellTool] Warning: failed to load sandbox config: {e}")

        # Detect background commands (Windows `start`, Unix trailing `&`)
        # These keep pipes open forever, causing subprocess to block indefinitely.
        is_background = bool(re.search(r'(?:^|\s+)(?:start\b)(?:\s|$)', command.strip(), re.IGNORECASE)
                             or command.strip().endswith('&'))

        try:
            popen_kwargs: Dict = {
                "shell": True,
                "cwd": cwd,
            }
            if is_background:
                # DEVNULL prevents pipe inheritance — the background process
                # won't keep our pipes open and Popen returns immediately.
                popen_kwargs["stdout"] = subprocess.DEVNULL
                popen_kwargs["stderr"] = subprocess.DEVNULL
            else:
                popen_kwargs["stdout"] = subprocess.PIPE
                popen_kwargs["stderr"] = subprocess.PIPE
                popen_kwargs["text"] = True
                popen_kwargs["encoding"] = "utf-8"
                popen_kwargs["errors"] = "replace"

            proc = subprocess.Popen(command, **popen_kwargs)

            # Track globally so interrupt_shell() can kill this process
            global _current_process
            with _current_process_lock:
                _current_process = proc

            # Polling loop — checks interrupt flag between 500 ms slices
            deadline = time.time() + (timeout or 60)
            stdout = ""
            stderr = ""

            while time.time() < deadline:
                if interrupt_check and interrupt_check():
                    proc.kill()
                    with _current_process_lock:
                        _current_process = None
                    try:
                        stdout, stderr = proc.communicate(timeout=2)
                    except Exception:
                        pass
                    output = ""
                    if cwd:
                        output += f"[Running in Sandbox: {cwd}]\n"
                    if stdout:
                        output += f"STDOUT:\n{stdout}\n"
                    if stderr:
                        output += f"STDERR:\n{stderr}\n"
                    output += "Exit Code: -1 (Interrupted by user)"
                    return output

                try:
                    stdout, stderr = proc.communicate(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    continue

            # Timed out while process is still running
            if proc.poll() is None:
                proc.kill()
                with _current_process_lock:
                    _current_process = None
                try:
                    stdout, stderr = proc.communicate(timeout=2)
                except Exception:
                    pass
                msg = f"Error: Command timed out after {timeout} seconds."
                if stdout or stderr:
                    summary = (stdout or "")[:200] + (stderr or "")[:200]
                    msg += f"\nPartial output:\n{summary}"
                return msg

            with _current_process_lock:
                if _current_process is proc:
                    _current_process = None

            output = ""
            if cwd:
                output += f"[Running in Sandbox: {cwd}]\n"
            if stdout:
                output += f"STDOUT:\n{stdout}\n"
            if stderr:
                output += f"STDERR:\n{stderr}\n"
            output += f"Exit Code: {proc.returncode}"
            return output

        except Exception as e:
            with _current_process_lock:
                _current_process = None
            return f"Error executing shell command: {str(e)}"
