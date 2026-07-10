import subprocess
import os
import sys
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
# Track backgrounded shell processes for monitoring
_background_process_info: dict = {}  # {task_id: {"pid": int, "output_file": str, "command": str, "started_at": float}}
_background_process_lock = threading.Lock()
# Orphan pool: processes backgrounded before a task_id was assigned
# {orphan_id: {"pid": int, "output_file": str, "command": str, "started_at": float, "session_id": int}}
_orphan_process_info: dict = {}
_orphan_process_lock = threading.Lock()
_orphan_counter = 0

# Interactive process stdin pipes for shell_send tool
_interactive_procs: dict = {}  # {pid: stdin_writeable}
_interactive_procs_lock = threading.Lock()

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
                        },
                        "detach": {
                            "type": "boolean",
                            "description": "设为 true 以启动常驻服务（服务器/守护进程），命令不会阻塞任务，系统不会等待它结束。适用于启动 ComfyUI、Web 服务等长期运行的程序。默认 false。",
                            "default": False
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
            # Windows
            (r'taskkill\b.*(?:/im\s+python\b|/im\s+python3?\b|/fi\s+["\']?\s*imagename\s+eq\s+python\b)', "禁止终止 python.exe（会杀死 Open-AGC 自身）"),
            (r'taskkill\b.*/pid\s+' + str(server_pid), "禁止终止 Open-AGC 进程自身"),
            (r'tskill\s+python\b', "禁止终止 python 进程"),
            (r'wmic\s+process\s+where.*delete', "禁止通过 WMI 终止进程"),
            # Linux: pkill/killall with -f flag targeting server processes
            (r'(?:^|[|&;]\s*)(?:sudo\s+)?pkill\s+(?:-[^\s]*)?\s*(?:python|uvicorn|open.agc)\b', "禁止终止 python/uvicorn 进程（会杀死 Open-AGC 自身）"),
            (r'(?:^|[|&;]\s*)(?:sudo\s+)?killall\s+(?:-[^\s]*)?\s*(?:python|uvicorn)\b', "禁止终止 python/uvicorn 进程（会杀死 Open-AGC 自身）"),
            (r'(?:^|[|&;]\s*)(?:sudo\s+)?(?:kill\s+-9\s+' + str(server_pid) + r'|kill\s+(?:-9\s+)?' + str(server_pid) + r')', "禁止终止 Open-AGC 进程自身"),
            (r'(?:^|[|&;]\s*)(?:sudo\s+)?pkill\s+(?:-[^\s]*\s+)?-f\s+["\']?(?:uvicorn|open.agc|api\.server)["\']?', "禁止通过进程名模式终止服务器进程"),
            # Cross-platform: dangerous patterns
            (r'(?:^|[|&;]\s*)(?:sudo\s+)?kill\s+(?:-\d+\s+)?\$\(.*(?:pgrep|pidof|ps\s+aux).*\)', "禁止通过命令替换方式批量终止进程"),
            (r'(?:^|[|&;]\s*)(?:sudo\s+)?pgrep\s+(?:-[^\s]*\s+)?(?:python|uvicorn)\s*\|', "禁止通过管道方式终止 python/uvicorn 进程"),
            (r'fuser\s+-\d*\s*k\b', "禁止通过 fuser 终止占用端口的进程"),
            (r'(?:stop|kill|terminate|restart)\s+(?:uvicorn|open.agc|api\.server)\b', "禁止停止服务器进程"),
            (r'systemctl\s+(?:stop|kill|restart)\s+(?:open.agc|uvicorn)\b', "禁止通过 systemctl 停止服务器"),
        ]

        for pattern, reason in suicidal_patterns:
            if re.search(pattern, cmd_lower):
                return (
                    f"⛔ 该命令被阻止执行：{reason}\n\n"
                    f"被阻止的命令: {command[:200]}\n"
                    f"如果你需要终止某个特定程序，请使用更精确的进程名或 PID，"
                    f"而非终止全部 python 进程。"
                )

        # Check PID-based kill on Linux (kill PID) against server process family
        pid_match = re.search(r'(?:^|[|&;\s])(?:sudo\s+)?kill\s+(?:-\d+\s+)?(\d+)', cmd_lower)
        if pid_match:
            target_pid = int(pid_match.group(1))
            try:
                from api.state import check_protected_pid
                if target_pid > 0 and check_protected_pid(target_pid):
                    return (
                        f"⛔ 该命令被阻止执行：PID {target_pid} 是 Open-AGC 服务进程或其父进程，"
                        f"终止它会导致服务崩溃。\n\n"
                        f"被阻止的命令: {command[:200]}\n"
                        f"如果你需要终止某个特定程序，请确认其 PID 不属于 Open-AGC 服务。"
                    )
            except ImportError:
                pass

        # Check PID-based kill on Windows (taskkill)
        pid_match = re.search(r'taskkill\b.*/pid\s+(\d+)', cmd_lower, re.IGNORECASE)
        if pid_match:
            target_pid = int(pid_match.group(1))
            try:
                from api.state import check_protected_pid
                if target_pid > 0 and check_protected_pid(target_pid):
                    return (
                        f"⛔ 该命令被阻止执行：PID {target_pid} 是 Open-AGC 服务进程或其子进程，"
                        f"终止它会导致服务崩溃。\n\n"
                        f"被阻止的命令: {command[:200]}\n"
                        f"如果你需要终止某个特定程序，请确认其 PID 不属于 Open-AGC 服务。"
                    )
            except ImportError:
                pass

        return ""

    def execute(self, **kwargs) -> str:
        import json
        from core.paths import get_data_path

        command = kwargs.get("command")
        if not command:
            return "Error: No command provided."

        timeout = kwargs.get("timeout", 120)
        detach = kwargs.get("detach", False)
        timeout = min(max(timeout, 1), 600)  # Clamp 1-600s
        # Auto-extend timeout for package managers (pip/uv/npm all download large files)
        if not kwargs.get("timeout") and _looks_like_download(command, ""):
            timeout = 180  # 10 min for package manager commands
        # For detach mode, use short timeout — just enough to detect startup errors
        if detach:
            timeout = min(timeout, 30)
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
        permission_whitelist = kwargs.get("_permission_whitelist", set())
        allowed, perm_msg, perm_cat, perm_desc = check_command_permission(command, config, session_whitelist=permission_whitelist)
        if not allowed:
            from tools.base import SandboxBlocked
            raise SandboxBlocked(command, sandbox_dir="permission", tool_name="execute_shell",
                                 category=perm_cat, description=perm_desc)

        # Check network domain whitelist — raise SandboxBlocked for popup
        network_whitelist = kwargs.get("_network_whitelist", set())
        for url in extract_urls_from_command(command):
            from urllib.parse import urlparse
            domain = urlparse(url).hostname or ""
            if domain in network_whitelist:
                continue  # Session-approved domain
            domain_ok, domain_msg = _check_domain_allowed(url, config)
            if not domain_ok:
                from tools.base import SandboxBlocked
                raise SandboxBlocked(url, sandbox_dir="network", tool_name="execute_shell")

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
                if sys.platform == "win32":
                    popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                proc = subprocess.Popen(command, **popen_kwargs)
                # Register the background process for monitoring
                task_id = kwargs.get("_task_id") or kwargs.get("task_id", 0)
                if task_id and task_id != 0:
                    with _background_process_lock:
                        _background_process_info[str(task_id)] = {
                            "pid": proc.pid,
                            "output_file": "",
                            "command": command[:200],
                            "started_at": _t0,
                            "timeout": 0,
                        "alive": True,
                        }
                return f"[Background] Command started with PID {proc.pid}." 
            else:
                # ── File-based streaming output ──
                import uuid
                out_dir = _get_shell_output_dir()
                out_name = f"{int(time.time())}_{uuid.uuid4().hex[:8]}.log"
                out_path = os.path.join(out_dir, out_name)
                out_file = open(out_path, "wb")  # binary mode: preserve raw output from child process

                popen_kwargs: Dict = {
                    "shell": True,
                    "cwd": cwd,
                    "stdin": subprocess.PIPE,
                    "stdout": out_file,
                    "stderr": subprocess.STDOUT,
                }
                if sys.platform == "win32":
                    popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                _t0 = time.time()
                proc = subprocess.Popen(command, **popen_kwargs)

                global _current_process
                with _current_process_lock:
                    _current_process = proc

                # Background thread: poll output file and emit progress
                poll_stop = threading.Event()
                last_pos = 0

                def _decode_shell_output(path: str, start: int, end: int) -> str:
                    """Read shell output bytes and decode as UTF-8 with fallback to system encoding."""
                    with open(path, "rb") as rf:
                        rf.seek(start)
                        raw = rf.read(end - start)
                    try:
                        return raw.decode("utf-8")
                    except UnicodeDecodeError:
                        # Fallback: system locale encoding (e.g. cp936 on Chinese Windows)
                        import locale
                        try:
                            return raw.decode(locale.getpreferredencoding(), errors="replace")
                        except Exception:
                            return raw.decode("utf-8", errors="replace")

                def _poll_output():
                    nonlocal last_pos
                    while not poll_stop.is_set():
                        time.sleep(0.5)
                        try:
                            fsize = os.path.getsize(out_path)
                            if fsize > last_pos:
                                new_text = _decode_shell_output(out_path, last_pos, fsize)
                                last_pos = fsize
                                if new_text and progress_cb:
                                    # Keep raw text (with \r) for frontend progress display.
                                    # _clean_cr is only used for final output reads.
                                    if not new_text.strip():
                                        continue
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
                        output_size = os.path.getsize(out_path) if os.path.exists(out_path) else 0

                        # If ZERO output after full timeout: process is hung (e.g. sudo waiting
                        # for password with no TTY, deadlocked pipe). Kill it instead of lying.
                        if output_size == 0:
                            try:
                                os.kill(proc.pid, getattr(signal, "SIGKILL", 9))
                            except OSError:
                                pass
                            elapsed = round(time.time() - _t0, 1)
                            return (
                                f"[HUNG] 进程在 {elapsed}s 内无任何输出，已终止。\n"
                                f"可能原因：sudo 需要密码但无终端输入、命令卡在交互式提示、或进程死锁。\n"
                                f"建议：sudo 命令请添加 -S 从 stdin 读密码，或 -n 跳过密码，"
                                f"或使用 `echo password | sudo -S command`。\n"
                                f"命令: {command[:200]}"
                            )

                        # Register as background process for system monitoring
                        task_id = kwargs.get("_task_id") or kwargs.get("task_id", 0)
                        if not task_id or task_id == 0:
                            # No valid task_id yet — put in orphan pool for late binding
                            global _orphan_counter, _orphan_process_info, _orphan_process_lock
                            session_id = kwargs.get("_session_id", 1) or 1
                            with _orphan_process_lock:
                                _orphan_counter += 1
                                oid = f"orphan_{int(_t0)}_{_orphan_counter}"
                                _orphan_process_info[oid] = {
                                    "pid": proc.pid,
                                    "output_file": out_path,
                                    "command": command[:200],
                                    "started_at": _t0,
                                    "timeout": timeout,
                                    "session_id": session_id,
                                }
                        else:
                            with _background_process_lock:
                                _background_process_info[str(task_id)] = {
                                    "pid": proc.pid,
                                    "output_file": out_path,
                                    "command": command[:200],
                                    "started_at": _t0,
                                    "timeout": timeout,
                                    "alive": True,
                                }
                        tail = _read_tail(out_path, 3000)
                        hint = ""
                        server_tag = ""
                        if detach or _looks_like_server(command, tail) or _detect_background_launcher(command):
                            server_tag = f"\n[SERVER_PROCESS] pid={proc.pid}"
                            tag = "（用户指定）" if detach else ""
                            hint += (
                                f"\n检测到这是一个常驻服务进程{tag}。系统不会自动等待它结束，"
                                f"任务不会被阻塞。如需手动停止服务，请在任务管理中终止。\n"
                            )
                        if _looks_like_download(command, tail):
                            hint = (
                                "\n⚠️ 这看起来像是一个下载/安装任务。建议使用 queue_download 工具下载大文件，"
                                "支持断点续传且不会阻塞。命令仍在后台运行中。\n"
                            )
                        # ── Interactive prompt detection ──
                        # Check the last 200 bytes of output for common interactive prompts
                        _interactive_prompts = [
                            b'mysql> ', b'sqlite> ', b'psql> ',
                            b'>>> ', b'In [',
                            b'ress: ',
                            b' :',  # pager (colon alone can match many things, check carefully)
                        ]
                        try:
                            with open(out_path, "rb") as _rf:
                                _rf.seek(max(0, output_size - 200))
                                _tail_bytes = _rf.read(200)
                            # Check specific prompts first, then >  (llama.cpp/Ollama CLI)
                            _is_interactive = any(p in _tail_bytes for p in _interactive_prompts)
                            if not _is_interactive and b'\n> ' in _tail_bytes:
                                _is_interactive = True
                            # Also check if last non-empty line is a single >
                            if not _is_interactive:
                                _last_lines = tail.strip().split('\n')
                                _last_nonempty = next((l.strip() for l in reversed(_last_lines) if l.strip()), '')
                                if _last_nonempty == '>':
                                    _is_interactive = True
                            if _is_interactive:
                                if proc.stdin:
                                    with _interactive_procs_lock:
                                        _interactive_procs[proc.pid] = proc.stdin
                                return (
                                    f"[Interactive] PID {proc.pid}\n"
                                    f"命令 {command[:100]} 可能已进入交互模式。\n"
                                    f"最新输出:\n{tail}\n"
                                )
                        except Exception:
                            pass
                        return (
                            f"[Still Running] 命令仍在执行中（已耗时 {round(time.time() - _t0, 1)}s）。\n"
                            f"当前输出 ({_read_file_size(out_path)} bytes):\n"
                            f"{tail}\n"
                            f"{hint}"
                            f"{server_tag}"
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


def get_background_processes() -> dict:
    """Return dict of tracked background processes: {task_id: info}."""
    with _background_process_lock:
        return dict(_background_process_info)


def cleanup_background_process(task_id: str):
    """Remove a background process from tracking."""
    with _background_process_lock:
        _background_process_info.pop(str(task_id), None)


def get_orphan_processes() -> dict:
    """Return dict of orphan background processes: {orphan_id: info}."""
    with _orphan_process_lock:
        return dict(_orphan_process_info)


def cleanup_orphan_process(orphan_id: str):
    """Remove an orphan process from tracking."""
    with _orphan_process_lock:
        _orphan_process_info.pop(orphan_id, None)


def adopt_orphan_processes(task_id: int, session_id: int = None) -> int:
    """
    Move orphan processes matching the given task_id/session_id from the
    orphan pool into the main background_process_info dict.

    Returns the number of processes adopted.
    """
    adopted = 0
    now = time.time()
    with _orphan_process_lock:
        to_adopt = []
        for oid, info in list(_orphan_process_info.items()):
            # Match by session_id first (most reliable)
            if session_id is not None and info.get("session_id") == session_id:
                # Only adopt if the orphan is recent (< 10 min old)
                if now - info.get("started_at", 0) < 600:
                    to_adopt.append(oid)
            # Also match any very recent orphans (< 2 min) regardless of session
            elif now - info.get("started_at", 0) < 120:
                to_adopt.append(oid)

        for oid in to_adopt:
            info = _orphan_process_info.pop(oid)
            with _background_process_lock:
                _background_process_info[str(task_id)] = info
            adopted += 1

    if adopted:
        import sys
        print(f"[Shell] Adopted {adopted} orphan process(es) → task #{task_id}", file=sys.stderr, flush=True)
    return adopted


def _looks_like_download(command: str, output: str) -> bool:
    """Detect if a shell command looks like it's downloading/installing large files."""
    dl_patterns = [
        # Package managers
        r'\b(pip|pip3|uv pip|conda|mamba)\s+install',
        r'\b(uv sync|uv run|uv add|poetry install|poetry add|npm install|yarn install|pnpm install)\b',
        r'\b(brew|apt-get|apt |choco|scoop)\s+install',
        r'\b(rustup update|nvm install|sdk install|gvm install)\b',
        # Downloads
        r'\b(wget|curl)\s+.*(\.gguf|\.safetensors|\.bin|\.zip|\.tar|\.gz|\.xz|\.7z|\.dmg|\.pkg)',
        r'\bgit clone\b',
        r'\bgit pull\b',
        r'\b(rsync|scp)\s+-',
        r'Downloading|Downloaded|⠋|⠙|⠹|⠸|⠼|⠴|⠦|⠧|⠇|⠏',  # pip/uv spinner
        # Build systems (compilation can take minutes)
        r'\b(make|cmake --build|ninja|cargo build|go build|dotnet build|mvn install|gradle build)\b',
        r'\bnpx playwright install\b',
        r'\bplaywright install\b',
        # Container operations
        r'\bdocker\s+(build|pull|push|compose)\b',
        # AI/ML model downloads
        r'\bhuggingface-cli download\b',
        r'\bollama (pull|run)\b',
        # Large file operations
        r'\bffmpeg\s+-(?:i|ss)\b',
    ]
    for p in dl_patterns:
        if re.search(p, command, re.IGNORECASE) or re.search(p, output, re.IGNORECASE):
            return True
    # Also check for large download indicators
    if re.search(r'\d+\.?\d*\s*(GiB|MiB|GB|MB)', output):
        return True
    return False


def _looks_like_server(command: str, output: str) -> bool:
    """Detect if a shell command looks like a long-running server/daemon process."""
    server_patterns = [
        # Common server frameworks
        r'\buvicorn\b', r'\bgunicorn\b', r'\bwaitress\b', r'\bhypercorn\b',
        r'\bdaphne\b', r'\bflask run\b', r'\bdjango runserver\b', r'\bfastapi run\b',
        r'\bnode\s+(server|app|index|main)\.(js|ts|mjs)\b',
        r'\bnpm (start|run start|run dev|run serve|run server)\b',
        r'\byarn (start|run start)\b', r'\bpnpm (start|run start)\b',
        r'\bng serve\b', r'\bvue serve\b', r'\bnpx serve\b',
        r'\bpython\s+.*\b(main|server|app|bot|daemon)\.py\b',
        r'\bpython\s+-m\s+http\.server\b', r'\bpython\s+-m\s+https\.server\b',
        r'\bgunicorn\b', r'\bcelery\s+worker\b', r'\bairflow\s+(scheduler|webserver)\b',
        r'\bjupyter (notebook|lab|server)\b', r'\bstreamlit run\b', r'\bgradio\b',
        # Services — note: no trailing \b because paths often have _ after the name
        r'\bcomfyui', r'\boobabooga', r'\btext-generation-webui',
        r'\boobabooga', r'\bllamacpp', r'\bsglang', r'\bvllm',
        r'\boobabooga', r'\binvokeai', r'\bautomatic1111', r'\bforge',
        r'\bvladmandic', r'\bswarmui', r'\bkoboldcpp',
        # Service launcher batch files
        r'run_nvidia_gpu\.bat', r'run_cpu\.bat', r'run\.bat',
        r'start.*\.bat', r'start.*\.sh',
        # Container/VM
        r'\bdocker (run|compose up|start)\b',
        r'\bminikube\b', r'\bkubectl\b',
        # Generic daemon patterns
        r'\b--daemon\b', r'\b-D\s*$', r'\bdetach\b',
        # Server output keywords
        r'listening on', r'listening at', r'running on', r'running at',
        r'server started', r'started server', r'serving on', r'serving at',
        r'http://0\.0\.0\.0', r'http://localhost', r'port \d+',
        r'Uvicorn running on', r'Application startup complete',
        r'started on port', r'bound to',
    ]
    cmd_lower = command.lower()
    out_lower = output.lower()
    for p in server_patterns:
        if re.search(p, cmd_lower, re.IGNORECASE) or re.search(p, out_lower, re.IGNORECASE):
            return True
    # If output has timestamped log lines (HH:MM:SS) — strong indicator of a
    # running server/daemon, since this function is only called after timeout
    # (the process has been running for 120s+ producing log-like output)
    lines = output.strip().split('\n')
    timestamped = sum(1 for l in lines if re.search(r'\d{1,2}:\d{2}:\d{2}', l))
    if timestamped >= 1:
        return True
    return False


def _detect_background_launcher(command: str) -> bool:
    """Detect if a command is explicitly launching a background/daemon process
    using OS-level mechanisms (start, &, nohup) regardless of the process name.
    Works alongside _looks_like_server which checks process names and output."""
    cmd_lower = command.lower().strip()
    # Windows: `start /min cmd /c "..."` or `start "" "program"`
    if re.search(r'\bstart\s+(/[\w]+\s+)?(cmd|""|".")?\s*/(c|min|max|b|wait)', cmd_lower):
        return True
    if re.search(r'\bstart\s+(/[\w]+\s+)?["\']', cmd_lower):
        return True
    # Unix: & at end of command (background)
    if re.search(r'&\s*$', cmd_lower):
        return True
    # Unix: nohup, setsid, disown
    if re.search(r'\b(nohup|setsid|disown)\b', cmd_lower):
        return True
    return False


def _clean_cr(text: str) -> str:
    """Process carriage returns: keep only final content after \\r overwrites.

    Handles:
      - \\r (standalone): clear current line (progress bar overwrite)
      - \\r\\n (CRLF): newline, keep line content
    """
    lines = []
    cur = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '\r':
            if i + 1 < len(text) and text[i + 1] == '\n':
                lines.append(''.join(cur) + '\n')
                cur = []
                i += 2
            else:
                cur = []
                i += 1
        elif ch == '\n':
            cur.append(ch)
            lines.append(''.join(cur))
            cur = []
            i += 1
        else:
            cur.append(ch)
            i += 1
    if cur:
        lines.append(''.join(cur))
    return ''.join(lines)


def _read_tail(path: str, max_chars: int) -> str:
    """Read the tail of a file, up to max_chars."""
    try:
        fsize = os.path.getsize(path)
        if fsize == 0:
            return "(no output)"
        with open(path, "rb") as f:
            raw = f.read()
        # Try UTF-8 first, then system locale
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            import locale
            try:
                text = raw.decode(locale.getpreferredencoding(), errors="replace")
            except Exception:
                text = raw.decode("utf-8", errors="replace")
        text = _clean_cr(text)
        if len(text) <= max_chars:
            return text
        return text[-max_chars:]
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
