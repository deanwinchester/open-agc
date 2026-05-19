import subprocess
import os
import time
import re
import threading
import tempfile
from typing import Any, Dict, Optional, Callable
from pydantic import Field

from tools.base import BaseTool

# Module-level process tracking for interrupt support
_current_process: Optional[subprocess.Popen] = None
_current_process_lock = threading.Lock()

# Output directory for streaming shell logs
SHELL_OUTPUT_DIR = None


def _get_shell_output_dir():
    global SHELL_OUTPUT_DIR
    if SHELL_OUTPUT_DIR is None:
        from core.paths import get_data_path
        SHELL_OUTPUT_DIR = get_data_path("shell_output")
        os.makedirs(SHELL_OUTPUT_DIR, exist_ok=True)
    return SHELL_OUTPUT_DIR


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
                            "description": "Optional timeout in seconds (default 120, max 600).",
                            "default": 120
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

        suicidal_patterns = [
            (r'taskkill\b.*(?:/im\s+python\b|/im\s+python3?\b)', "禁止终止 python.exe（会杀死 Open-AGC 自身）"),
            (r'taskkill\b.*/pid\s+' + str(server_pid), "禁止终止 Open-AGC 进程自身"),
            (r'tskill\s+python\b', "禁止终止 python 进程"),
            (r'wmic\s+process\s+where.*delete', "禁止通过 WMI 终止进程"),
            (r'(?:^|[|&;]\s*)(?:sudo\s+)?(?:kill\s+-9\s+' + str(server_pid) + r'|pkill\s+(?:-9\s+)?python|killall\s+(?:-9\s+)?python)', "禁止终止 python 进程（会杀死 Open-AGC 自身）"),
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

        timeout = kwargs.get("timeout", 120)
        timeout = min(max(timeout, 1), 600)  # Clamp 1-600s
        # Auto-extend timeout for package managers (pip/uv/npm all download large files)
        if not kwargs.get("timeout") and _looks_like_download(command, ""):
            timeout = 600  # 10 min for package manager commands
        interrupt_check: Optional[Callable[[], bool]] = kwargs.get("interrupt_check")
        progress_cb: Optional[Callable] = kwargs.get("_progress_cb")

        # ── Self-preservation ──
        blocked = self._check_dangerous(command)
        if blocked:
            return blocked

        # ── Permission check for destructive commands ──
        config_path = get_data_path("config.json")
        config = {}
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
            except Exception:
                pass
        from tools.permissions import check_command_permission, extract_urls_from_command, _check_domain_allowed
        allowed, perm_msg = check_command_permission(command, config)
        if not allowed:
            return perm_msg

        # Check network domain whitelist for commands with URLs
        for url in extract_urls_from_command(command):
            domain_ok, domain_msg = _check_domain_allowed(url, config)
            if not domain_ok:
                return f"⛔ 网络访问受限: {domain_msg}\n命令: {command[:200]}"

        # Sandbox Mode Enforcement
        cwd = None
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

        is_background = bool(re.search(r'(?:^|\s+)(?:start\b)(?:\s|$)', command.strip(), re.IGNORECASE)
                             or command.strip().endswith('&'))

        try:
            if is_background:
                popen_kwargs: Dict = {
                    "shell": True,
                    "cwd": cwd,
                    "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL,
                }
                proc = subprocess.Popen(command, **popen_kwargs)
                return f"[Background] Command started with PID {proc.pid}."
            else:
                # ── File-based streaming output ──
                import uuid
                out_dir = _get_shell_output_dir()
                out_name = f"{int(time.time())}_{uuid.uuid4().hex[:8]}.log"
                out_path = os.path.join(out_dir, out_name)
                out_file = open(out_path, "w", encoding="utf-8", errors="replace")

                popen_kwargs: Dict = {
                    "shell": True,
                    "cwd": cwd,
                    "stdout": out_file,
                    "stderr": subprocess.STDOUT,
                    "text": True,
                }
                _t0 = time.time()
                proc = subprocess.Popen(command, **popen_kwargs)

                global _current_process
                with _current_process_lock:
                    _current_process = proc

                # Background thread: poll output file and emit progress
                poll_stop = threading.Event()
                last_pos = 0

                def _poll_output():
                    nonlocal last_pos
                    while not poll_stop.is_set():
                        time.sleep(0.5)
                        try:
                            fsize = os.path.getsize(out_path)
                            if fsize > last_pos:
                                with open(out_path, "r", encoding="utf-8", errors="replace") as rf:
                                    rf.seek(last_pos)
                                    new_text = rf.read(fsize - last_pos)
                                    last_pos = fsize
                                    if new_text and progress_cb:
                                        elapsed = time.time() - _t0
                                        # Truncate to last 2000 chars for progress
                                        preview = (new_text[-2000:] if len(new_text) > 2000
                                                   else new_text)
                                        progress_cb({
                                            "event": "shell_output",
                                            "text": preview,
                                            "elapsed": round(elapsed, 1),
                                            "total_bytes": fsize,
                                        })
                        except Exception:
                            pass

                poll_thread = threading.Thread(target=_poll_output, daemon=True)
                poll_thread.start()

                # Main loop: wait for process, checking interrupt
                deadline = time.time() + timeout
                try:
                    while proc.poll() is None and time.time() < deadline:
                        if interrupt_check and interrupt_check():
                            proc.kill()
                            poll_stop.set()
                            poll_thread.join(timeout=2)
                            break
                        time.sleep(0.3)

                    # If still running after timeout, don't kill — return partial
                    if proc.poll() is None:
                        poll_stop.set()
                        poll_thread.join(timeout=2)
                        out_file.close()
                        tail = _read_tail(out_path, 3000)
                        hint = ""
                        if _looks_like_download(command, tail):
                            hint = (
                                "\n⚠️ 这看起来像是一个下载/安装任务。建议使用 queue_download 工具下载大文件，"
                                "支持断点续传且不会阻塞。命令仍在后台运行中。\n"
                            )
                        return (
                            f"[Still Running] 命令仍在执行中（已耗时 {round(time.time() - _t0, 1)}s）。\n"
                            f"当前输出 ({_read_file_size(out_path)} bytes):\n"
                            f"{tail}\n"
                            f"{hint}"
                            f"输出文件: {out_path}\n"
                            f"进程将继续在后台运行。"
                        )

                except Exception:
                    proc.kill()
                    poll_stop.set()
                    poll_thread.join(timeout=2)

                poll_stop.set()
                poll_thread.join(timeout=3)
                out_file.close()

                with _current_process_lock:
                    if _current_process is proc:
                        _current_process = None

                # Read full output
                full_output = _read_tail(out_path, 30000)
                elapsed = round(time.time() - _t0, 1)

                result = ""
                if cwd:
                    result += f"[Sandbox: {cwd}]\n"
                result += full_output
                result += f"\nExit Code: {proc.returncode}  |  Time: {elapsed}s"
                return result

        except Exception as e:
            with _current_process_lock:
                _current_process = None
            return f"Error executing shell command: {str(e)}"


def _looks_like_download(command: str, output: str) -> bool:
    """Detect if a shell command looks like it's downloading/installing large files."""
    dl_patterns = [
        r'\b(pip|pip3|uv pip|conda|mamba)\s+install',
        r'\b(uv sync|uv run|poetry install|npm install|yarn install)\b',
        r'\b(wget|curl)\s+.*(\.gguf|\.safetensors|\.bin|\.zip|\.tar)',
        r'\bgit clone\b',
        r'\bapt-get\s+install|brew\s+install|choco\s+install',
        r'Downloading|Downloaded|⠋|⠙|⠹|⠸|⠼|⠴|⠦|⠧|⠇|⠏',  # pip/uv spinner
    ]
    for p in dl_patterns:
        if re.search(p, command, re.IGNORECASE) or re.search(p, output, re.IGNORECASE):
            return True
    # Also check for large download indicators
    if re.search(r'\d+\.?\d*\s*(GiB|MiB|GB|MB)', output):
        return True
    return False


def _read_tail(path: str, max_chars: int) -> str:
    """Read the tail of a file, up to max_chars."""
    try:
        fsize = os.path.getsize(path)
        if fsize == 0:
            return "(no output)"
        if fsize <= max_chars:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(max(0, fsize - max_chars))
            return "...(truncated)\n" + f.read()
    except Exception as e:
        return f"(error reading output: {e})"


def _read_file_size(path: str) -> str:
    try:
        size = os.path.getsize(path)
        if size < 1024:
            return f"{size}B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f}KB"
        return f"{size / 1024 / 1024:.1f}MB"
    except Exception:
        return "?"
