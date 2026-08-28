import subprocess
import os
import sys
import time
import re
import json
import signal
import threading
import tempfile
from typing import Any, Dict, Optional, Callable
from pydantic import Field

from tools.base import BaseTool, SandboxBlocked
from core.process import kill_tree, pid_alive

# Module-level process tracking for interrupt support
_current_process: Optional[subprocess.Popen] = None
_current_process_lock = threading.Lock()
# Track backgrounded shell processes for monitoring.
# 一任务多进程：{task_id: {pid: info}}——同一任务可登记多个后台进程，
# 新进程不再覆盖旧条目（旧结构一任务一槽位，重启/重试会产生失联野进程）。
_background_process_info: dict = {}  # {task_id: {pid: {"pid": int, "output_file": str, "command": str, "started_at": float, ...}}}
_background_process_lock = threading.Lock()
# Orphan pool: processes backgrounded before a task_id was assigned
# {orphan_id: {"pid": int, "output_file": str, "command": str, "started_at": float, "session_id": int}}
_orphan_process_info: dict = {}
_orphan_process_lock = threading.Lock()
_orphan_counter = 0

# 持久化注册表路径（lazy 解析；测试可覆盖）。每次注册/注销写-through，
# 服务重启后按 pid 存活 + create_time 校验复活，不再"重启失忆"。
_BG_STORE_PATH: Optional[str] = None


def _background_store_path() -> str:
    global _BG_STORE_PATH
    if _BG_STORE_PATH is None:
        from core.paths import get_data_path
        _BG_STORE_PATH = get_data_path("background_processes.json")
    return _BG_STORE_PATH


def _persist_background_processes_locked():
    """把主表整体写入 data/background_processes.json（低频操作，整文件写）。

    调用方必须持有 _background_process_lock。写失败只告警不抛出——
    持久化是兜底，不能反过来影响进程执行本身。
    """
    try:
        path = _background_store_path()
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(_background_process_info, f, ensure_ascii=False, indent=1)
        os.replace(tmp_path, path)
    except Exception as e:
        print(f"[Shell] Failed to persist background processes: {e}")

# Interactive process stdin pipes for shell_send tool
_interactive_procs: dict = {}  # {pid: stdin_writeable}
_interactive_procs_lock = threading.Lock()

# Output directory for streaming shell logs
SHELL_OUTPUT_DIR = None


def _mask(text: str) -> str:
    """Mask known secret values (passwords / credential URIs) in text.

    Local import to avoid circular imports; never raises — on any error the
    original text is returned unchanged.
    """
    try:
        from core.secrets import mask_secrets
        return mask_secrets(text)
    except Exception:
        return text


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


def _sudo_safe_env() -> dict:
    """Return a copy of os.environ with GUI askpass helpers disabled.

    On desktop Linux (e.g. UOS), sudo may invoke a GUI askpass helper when no
    TTY is available, making the process hang silently with zero output. Drop
    SUDO_ASKPASS/SSH_ASKPASS and point SUDO_ASKPASS at /bin/false so sudo fails
    fast instead of hanging invisibly. Applied to sudo commands only.
    """
    env = os.environ.copy()
    env.pop("SUDO_ASKPASS", None)
    env.pop("SSH_ASKPASS", None)
    env["SUDO_ASKPASS"] = "/bin/false"
    return env


def _python_utf8_env(env: dict) -> dict:
    """补 PYTHONIOENCODING=utf-8（用户已显式设置则不覆盖）。

    中文 Windows 上 python 子进程 stdio 默认按 cp936 输出，与 cmd 内建命令的
    GBK、python_repl 的 UTF-8 混杂进同一日志文件，读取侧只能逐行猜编码。
    规范 python 系子进程 stdio 写 UTF-8 后，混合面收敛为「cmd 内建 GBK +
    其余 UTF-8」两类，_decode_mixed 按行解码即可正确显示。该键对 sudo
    安全 env 同样安全（不影响提权语义）。

    不注入 PYTHONUTF8：UTF-8 模式会把裸 open() 的默认编码从 cp936 改成
    UTF-8，第三方脚本读写既有 GBK 文件（旧数据/.bat/.reg）会出新乱码或
    UnicodeDecodeError，代价超过收益；PYTHONIOENCODING 只规范 stdio 三道
    流，不碰 open() 默认值。
    """
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _decode_mixed(raw: bytes) -> str:
    """按行解码混合编码的原始字节：逐行 strict UTF-8，失败的行退回系统区域编码。

    进程日志文件是子进程 stdout 的原始字节（open(out_path, "wb") 直写）。
    中文 Windows 上 cmd 内建命令输出 GBK、python_repl 脚本输出 UTF-8，同一
    文件逐行混杂是常态；整块二选一解码必乱一半，故按行 strict UTF-8，失败
    的行用 locale.getpreferredencoding()（如 cp936）decode。行尾保留 \n；
    二进制垃圾行（两种编码都失败率高）保底 errors="replace"，不抛异常。
    行分隔字节（\\n/\\r 等）均 < 0x40，不可能是 GBK 尾字节或 UTF-8 后续
    字节，按行切分不会切断多字节字符。
    """
    if not raw:
        return ""
    import locale
    try:
        fallback_enc = locale.getpreferredencoding() or "utf-8"
    except Exception:
        fallback_enc = "utf-8"
    parts = []
    for line in raw.splitlines(keepends=True):
        try:
            parts.append(line.decode("utf-8"))
        except UnicodeDecodeError:
            try:
                parts.append(line.decode(fallback_enc, errors="replace"))
            except Exception:
                parts.append(line.decode("utf-8", errors="replace"))
    return "".join(parts)


class _LineBuffer:
    """增量字节流的行/段缓冲：只切出完整段解码，半段字节留到下一轮。

    进度轮询每 0.5s 按字节区间增量读取日志文件，任意切分会把多字节字符
    切成两半，整块错解成乱码。feed() 只消费到最后一个 \\n 或 \\r 为止
    （\\r 是 tqdm/pip 类进度条刷新符，前端依赖增量 \\r 模拟进度，原始
    \\r 字节必须保留，不能只认 \\n 否则进度条到进程结束才出现）；
    \\n/\\r 均 < 0x40，不可能是 GBK 尾字节或 UTF-8 后续字节，不会切断
    多字节字符。flush() 在进程结束时冲刷残余。_pending 超过
    _MAX_PENDING 时按现状发出，防无 \\n/\\r 巨量单行无界累积。
    """

    _MAX_PENDING = 64 * 1024  # 64KB

    def __init__(self):
        self._pending = bytearray()

    def feed(self, data: bytes) -> str:
        """追加新字节，返回已凑齐完整段部分的解码文本（可能为空）。"""
        if data:
            self._pending.extend(data)
        cut = max(self._pending.rfind(b"\n"), self._pending.rfind(b"\r"))
        if cut >= 0:
            chunk = bytes(self._pending[:cut + 1])
            del self._pending[:cut + 1]
            return _decode_mixed(chunk)
        if len(self._pending) > self._MAX_PENDING:
            # 无 \n/\r 的巨量单行：按现状发出，内存有界 + 长行进度可见
            return self.flush()
        return ""

    def flush(self) -> str:
        """冲刷残余半行（进程结束时调用）。"""
        if not self._pending:
            return ""
        chunk = bytes(self._pending)
        self._pending.clear()
        return _decode_mixed(chunk)


class ShellTool(BaseTool):
    name: str = "execute_shell"
    description: str = ("在本机执行 bash 命令。sudo 弹密码框（密码不入会话，失败提示需要密码时重试一次即可触发）；"
                        "交互程序用 shell_send 续聊。")

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
                            "description": "bash 命令，如 'ls -la'。"
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "超时秒数（默认 120，最长 600）。",
                            "default": 120
                        },
                        "detach": {
                            "type": "boolean",
                            "description": "true 启动常驻服务不阻塞（如 Web 服务）；默认 false。",
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
        # Union with the session-level shared whitelist: fresh agent instances
        # and sub-agents may carry an empty/stale instance-level set.
        try:
            from api.state import _session_permission_whitelists
            _sid = kwargs.get("_session_id")
            _shared_wl = _session_permission_whitelists.get(_sid) if _sid is not None else None
            if _shared_wl:
                permission_whitelist = set(permission_whitelist) | _shared_wl
        except Exception:
            pass
        # One-shot permission grant（approve_once「授权本次」）：该确切命令被
        # 授权一次——消费掉（用完即焚）并跳过类别检查，下一条同类命令重新弹窗。
        _consumed_once = False
        try:
            from api.state import _session_permission_once
            _sid_once = kwargs.get("_session_id")
            if _sid_once is not None:
                _once_set = _session_permission_once.get(_sid_once)
                if _once_set and command in _once_set:
                    _once_set.discard(command)
                    _consumed_once = True
        except Exception:
            pass
        if not _consumed_once:
            allowed, perm_msg, perm_cat, perm_desc = check_command_permission(command, config, session_whitelist=permission_whitelist)
            if not allowed:
                raise SandboxBlocked(command, sandbox_dir="permission", tool_name="execute_shell",
                                     category=perm_cat, description=perm_desc)

        # -- Sudo handling: use sudo -S to read password from stdin --
        # Password comes from user via popup (never from LLM). Written to proc.stdin after Popen.
        _sudo_password = kwargs.get("_sudo_password", "")
        if not _sudo_password:
            # Fall back to the session-level shared cache — sub-agents and fresh
            # agent instances don't carry the instance-level cache, but share
            # the same session_id.
            try:
                from api.state import _session_sudo_passwords
                _sid = kwargs.get("_session_id")
                if _sid is not None:
                    _sudo_password = _session_sudo_passwords.get(_sid, "") or ""
            except Exception:
                pass
        _is_sudo = re.match(r'^\s*(sudo\s+)', command)
        if _is_sudo:
            if _sudo_password:
                command = command[:_is_sudo.start(1)] + 'sudo -S ' + command[_is_sudo.end(1):]
            else:
                command = command[:_is_sudo.start(1)] + 'sudo -n ' + command[_is_sudo.end(1):]
            print(f"[ShellTool] Sudo command rewritten: {command[:120]}...")

        # Check network domain whitelist — raise SandboxBlocked for popup
        network_whitelist = kwargs.get("_network_whitelist", set())
        for url in extract_urls_from_command(command):
            from urllib.parse import urlparse
            domain = urlparse(url).hostname or ""
            if domain in network_whitelist:
                continue  # Session-approved domain
            domain_ok, domain_msg = _check_domain_allowed(url, config)
            if not domain_ok:
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

        # ── Secrets substitution ──
        # Replace {{secret:name.field}} placeholders with real values ONLY in the
        # command handed to the child process. `command` (with placeholders) is
        # what gets logged, tracked, shown in popups and returned — credentials
        # never leave this process in any message or output.
        try:
            from core.secrets import substitute_refs
            exec_command = substitute_refs(command)
        except Exception:
            exec_command = command

        is_background = _is_background_command(command)

        try:
            _t0 = time.time()
            if is_background:
                popen_kwargs: Dict = {
                    "shell": True,
                    "cwd": cwd,
                    "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL,
                }
                if _is_sudo:
                    popen_kwargs["env"] = _sudo_safe_env()
                else:
                    popen_kwargs["env"] = os.environ.copy()
                _python_utf8_env(popen_kwargs["env"])
                if sys.platform == "win32":
                    popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                proc = subprocess.Popen(exec_command, **popen_kwargs)
                # Register the background process for monitoring (multi-process
                # per task: keyed by pid, does not overwrite earlier entries)
                task_id = kwargs.get("_task_id") or kwargs.get("task_id", 0)
                if task_id and task_id != 0:
                    register_background_process(task_id, {
                        "pid": proc.pid,
                        "output_file": "",
                        "command": command[:200],
                        "started_at": _t0,
                        "timeout": 0,
                        "alive": True,
                    })
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
                if _is_sudo:
                    popen_kwargs["env"] = _sudo_safe_env()
                else:
                    popen_kwargs["env"] = os.environ.copy()
                _python_utf8_env(popen_kwargs["env"])
                if sys.platform == "win32":
                    popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                _t0 = time.time()
                proc = subprocess.Popen(exec_command, **popen_kwargs)

                # Feed sudo password via stdin (for sudo -S). Password never appears in
                # command line, process listing, or LLM context. stdin closed after writing.
                if _is_sudo and _sudo_password and proc.stdin:
                    try:
                        proc.stdin.write((_sudo_password + "\n").encode())
                        proc.stdin.flush()
                    except Exception:
                        pass
                    finally:
                        try:
                            proc.stdin.close()
                        except Exception:
                            pass

                global _current_process
                with _current_process_lock:
                    _current_process = proc

                # Background thread: poll output file and emit progress
                poll_stop = threading.Event()
                last_pos = 0
                line_buf = _LineBuffer()

                def _read_new_bytes(path: str, start: int, end: int) -> bytes:
                    with open(path, "rb") as rf:
                        rf.seek(start)
                        return rf.read(end - start)

                def _emit_progress(text: str, fsize: int):
                    if not text or not progress_cb or not text.strip():
                        return
                    elapsed = time.time() - _t0
                    # Truncate to last 2000 chars for progress
                    preview = (text[-2000:] if len(text) > 2000
                               else text)
                    # Live echo to the frontend must be masked too
                    preview = _mask(preview)
                    progress_cb({
                        "event": "shell_output",
                        "text": preview,
                        "elapsed": round(elapsed, 1),
                        "total_bytes": fsize,
                    })

                def _poll_output():
                    nonlocal last_pos
                    while not poll_stop.is_set():
                        time.sleep(0.5)
                        try:
                            fsize = os.path.getsize(out_path)
                            if fsize > last_pos:
                                # Keep raw text (with \r) for frontend progress display.
                                # _clean_cr is only used for final output reads.
                                # 行/段缓冲：切到最后一个 \n 或 \r 为止（\r 是
                                # tqdm 类进度刷新符），半段字节（可能切在多字节
                                # 字符中间）留到下一轮，避免整块错解成乱码。
                                new_text = line_buf.feed(_read_new_bytes(out_path, last_pos, fsize))
                                last_pos = fsize
                                _emit_progress(new_text, fsize)
                        except Exception:
                            pass
                    # 进程结束：最后一轮新字节里 feed 切出的完整行/段与残余
                    # 半行（最后一行可能无 \n 结尾）都要发出，不可只发残余
                    try:
                        fsize = os.path.getsize(out_path)
                        text = ""
                        if fsize > last_pos:
                            text = line_buf.feed(_read_new_bytes(out_path, last_pos, fsize))
                            last_pos = fsize
                        _emit_progress(text + line_buf.flush(), fsize)
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
                                f"建议：重新执行该命令，系统将弹出密码输入框。\n"
                                f"命令: {command[:200]}"
                            )

                        # Register as background process for system monitoring
                        task_id = kwargs.get("_task_id") or kwargs.get("task_id", 0)
                        if not task_id or task_id == 0:
                            # No valid task_id yet — put in orphan pool for late binding
                            register_orphan_process({
                                "pid": proc.pid,
                                "output_file": out_path,
                                "command": command[:200],
                                "started_at": _t0,
                                "timeout": timeout,
                            }, session_id=kwargs.get("_session_id", 1) or 1)
                        else:
                            register_background_process(task_id, {
                                "pid": proc.pid,
                                "output_file": out_path,
                                "command": command[:200],
                                "started_at": _t0,
                                "timeout": timeout,
                                "alive": True,
                            })
                        tail = _mask(_read_tail(out_path, 3000))
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
                        # Check the last 512 bytes of output; only whole-line
                        # prompt patterns count (see _INTERACTIVE_LINE_PATTERNS).
                        try:
                            with open(out_path, "rb") as _rf:
                                _rf.seek(max(0, output_size - 512))
                                _tail_bytes = _rf.read(512)
                            _is_interactive = _detect_interactive_prompt(_tail_bytes)
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
                full_output = _mask(_read_tail(out_path, 30000))
                elapsed = round(time.time() - _t0, 1)

                result = ""
                if cwd:
                    result += f"[Sandbox: {cwd}]\n"
                result += full_output
                result += f"\nExit Code: {proc.returncode}  |  Time: {elapsed}s"

                # -- Sudo failure detection --
                # sudo -n was used (no cached password) and failed: trigger the
                # authorization popup (SandboxBlocked, category='sudo') instead of
                # returning a text hint no code acts upon. The agent's retry loop
                # re-runs the command with the cached password via sudo -S.
                if _is_sudo and not _sudo_password and proc.returncode != 0:
                    if re.search(r'(?:a password is required|no password was provided|sorry, try again)', full_output, re.IGNORECASE):
                        raise SandboxBlocked(command, sandbox_dir="permission",
                                             tool_name="execute_shell",
                                             category="sudo",
                                             description="需要 sudo 密码")
                return result

        except SandboxBlocked:
            # Authorization popup trigger — must propagate to the agent's retry
            # loop, not be swallowed into an error string.
            raise
        except Exception as e:
            with _current_process_lock:
                _current_process = None
            return f"Error executing shell command: {_mask(str(e))}"


def register_background_process(task_id, info: dict) -> None:
    """Register a background process under its task, keyed by pid.

    一任务多进程：同一 task_id 的多个进程各自登记在 pid 槽位下，互
    不覆盖（旧结构 {task_id: info} 会被后启动的进程整体顶掉，产生
    失联野进程）。注册即写-through 到 data/background_processes.json。
    """
    pid = info.get("pid")
    if not task_id or not pid:
        return
    with _background_process_lock:
        _background_process_info.setdefault(str(task_id), {})[str(pid)] = info
        _persist_background_processes_locked()


def get_background_processes() -> dict:
    """Return dict of tracked background processes: {task_id: {pid: info}}."""
    with _background_process_lock:
        return {tid: dict(procs) for tid, procs in _background_process_info.items()}


def get_background_processes_for_task(task_id) -> dict:
    """Return {pid: info} of all tracked background processes for one task."""
    with _background_process_lock:
        return dict(_background_process_info.get(str(task_id), {}))


def cleanup_background_process(task_id: str, pid=None):
    """Remove background process tracking.

    pid 为 None 时清掉该任务整组；指定 pid 时只清对应条目（组空则连
    任务键一起删）。清理后同步持久化。
    """
    with _background_process_lock:
        procs = _background_process_info.get(str(task_id))
        if not procs:
            return
        if pid is None:
            _background_process_info.pop(str(task_id), None)
        else:
            procs.pop(str(pid), None)
            if not procs:
                _background_process_info.pop(str(task_id), None)
        _persist_background_processes_locked()


def cleanup_background_pid(pid) -> bool:
    """Remove any tracked entry (across all tasks) matching this pid."""
    removed = False
    with _background_process_lock:
        for tid in list(_background_process_info.keys()):
            procs = _background_process_info[tid]
            if str(pid) in procs:
                procs.pop(str(pid), None)
                removed = True
            if not procs:
                _background_process_info.pop(tid, None)
        if removed:
            _persist_background_processes_locked()
    return removed


def kill_background_process_for_task(task_id, max_rounds: int = 3) -> list:
    """Kill ALL tracked background process trees for a task and drop the group.

    Used when a task is interrupted: every process registered under
    ``_background_process_info[task_id]`` must die with the task instead of
    running wild. 有界重试：每轮锁内取当前全部条目、锁外杀、锁内只清本轮
    捕获的条目——旧实现"锁内拷贝→锁外杀→整组清理"在并发注册窗口内会把
    新登记的 pid 连带删除却不杀（再造野进程）；窗口内新登记的 pid 会被
    下一轮抓到照杀，直到无剩余或达上限（更迟的登记由
    ``reap_dead_background_processes`` 僵尸回收兜底）。A kill failure on
    one pid does not stop the others; the first exception is re-raised AFTER
    cleanup so the caller can report the failure honestly (callers must
    wrap——杀进程失败不得阻断中断流程本身). Returns the list of killed pids
    (empty when the task had no tracked process).
    """
    killed = []
    first_err: Optional[Exception] = None
    for _round in range(max(1, max_rounds)):
        with _background_process_lock:
            procs = dict(_background_process_info.get(str(task_id), {}))
        if not procs:
            break
        round_keys = list(procs.keys())
        for info in procs.values():
            pid = info.get("pid")
            if not pid:
                continue
            try:
                kill_tree(pid)
                killed.append(pid)
            except Exception as e:
                if first_err is None:
                    first_err = e
        # 只清本轮捕获的条目；窗口内新登记的留给下一轮
        with _background_process_lock:
            cur = _background_process_info.get(str(task_id))
            if cur:
                for k in round_keys:
                    cur.pop(k, None)
                if not cur:
                    _background_process_info.pop(str(task_id), None)
            _persist_background_processes_locked()
    if first_err is not None:
        raise first_err
    return killed


def find_task_for_pid(pid) -> Optional[tuple]:
    """反查主表：返回 (task_id, info)；pid 不在主表返回 None。"""
    with _background_process_lock:
        for tid, procs in _background_process_info.items():
            if str(pid) in procs:
                return tid, dict(procs[str(pid)])
    return None


def find_orphan_for_pid(pid) -> Optional[tuple]:
    """反查 orphan 池：返回 (orphan_id, info)；pid 不在池中返回 None。"""
    with _orphan_process_lock:
        for oid, info in _orphan_process_info.items():
            if info.get("pid") == pid:
                return oid, dict(info)
    return None


def reap_dead_background_processes(alive_fn=None, exclude_task_ids=None) -> list:
    """惰性回收：清掉注册表（主表 + orphan 池）中 pid 已死的条目。

    BgMonitor 的逐任务检查只覆盖 backgrounded 任务——其他状态（running/
    interrupted/completed）任务名下的进程条目死后无人清理，永远显示
    "运行中"（僵尸条目）。读取路径与监控循环每轮调用本函数兜底。
    exclude_task_ids 用于 BgMonitor 排除本轮正在处理的 backgrounded
    任务（其死条目由监控分支自己判定，关系"全死才恢复"的触发）。
    返回被清条目（附 task_id/orphan_id），并逐条打日志：输出文件还在
    → 保留路径；已删 → 标记（想查日志的用户有据可循）。清表后持久化。
    """
    check = alive_fn or pid_alive
    excluded = {str(t) for t in (exclude_task_ids or ())}
    with _background_process_lock:
        snapshot = {tid: dict(procs) for tid, procs in _background_process_info.items()}
    dead = []
    for tid, procs in snapshot.items():
        if tid in excluded:
            continue
        for pid_key, info in procs.items():
            pid = info.get("pid")
            if not pid or not check(pid):
                dead.append((tid, pid_key, info))
    with _orphan_process_lock:
        orphan_snapshot = dict(_orphan_process_info)
    dead_orphans = []
    for oid, info in orphan_snapshot.items():
        pid = info.get("pid")
        if not pid or not check(pid):
            dead_orphans.append((oid, info))
    if not dead and not dead_orphans:
        return []
    reaped = []
    with _background_process_lock:
        for tid, pid_key, info in dead:
            cur = _background_process_info.get(tid)
            if not cur or pid_key not in cur:
                continue
            cur.pop(pid_key, None)
            if not cur:
                _background_process_info.pop(tid, None)
            reaped.append({**info, "task_id": tid})
        if dead:
            _persist_background_processes_locked()
    with _orphan_process_lock:
        for oid, info in dead_orphans:
            if _orphan_process_info.pop(oid, None) is not None:
                reaped.append({**info, "task_id": None, "orphan_id": oid})
    for entry in reaped:
        of = entry.get("output_file", "")
        if of and os.path.exists(of):
            state = f"output kept: {of}"
        elif of:
            state = f"output already deleted: {of}"
        else:
            state = "no output file"
        print(f"[Shell] Reaped dead background process pid={entry.get('pid')} "
              f"task={entry.get('task_id') or entry.get('orphan_id')}: {state}")
    return reaped


def detach_background_process(task_id: str, pid, session_id: int = None) -> Optional[str]:
    """把任务下某个进程条目移入 orphan 池（打 "detached" 标记），返回 orphan_id。

    用于 BgMonitor 的"输出冻结解除追踪"：进程继续跑，但从任务的监控
    视野中脱离——不丢弃条目，保持进程可见、可杀。detached 条目不会
    被 adopt_orphan_processes 重新认领回任务（见该函数内的跳过逻辑）。
    """
    global _orphan_counter
    with _background_process_lock:
        procs = _background_process_info.get(str(task_id))
        info = procs.pop(str(pid), None) if procs else None
        if procs is not None and not procs:
            _background_process_info.pop(str(task_id), None)
        if info is None:
            return None
        _persist_background_processes_locked()
    info = dict(info)
    info["detached"] = True
    info["task_id"] = str(task_id)
    if session_id is not None:
        info["session_id"] = session_id
    with _orphan_process_lock:
        _orphan_counter += 1
        oid = f"detached_{task_id}_{pid}_{_orphan_counter}"
        _orphan_process_info[oid] = info
    return oid


def restore_background_processes() -> int:
    """服务启动时从 data/background_processes.json 复活进程注册表。

    条目复活的条件：pid 当前存活，且 psutil create_time 与记录的
    started_at 误差 < 60s（防 pid 复用误判——重启后 pid 可能已被
    无关进程占用）。不满足的条目直接剔除。返回复活数量。
    """
    try:
        path = _background_store_path()
        if not os.path.exists(path):
            return 0
        with open(path, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except Exception as e:
        print(f"[Shell] Failed to load background process store: {e}")
        return 0
    if not isinstance(saved, dict):
        return 0
    import psutil
    restored = 0
    dropped = 0
    with _background_process_lock:
        for tid, procs in saved.items():
            if not isinstance(procs, dict):
                continue
            if "pid" in procs:
                # 兼容旧版扁平格式 {task_id: info}
                procs = {str(procs.get("pid")): procs}
            for pid_key, info in procs.items():
                if not isinstance(info, dict):
                    dropped += 1
                    continue
                try:
                    pid = int(info.get("pid") or pid_key)
                except (TypeError, ValueError):
                    dropped += 1
                    continue
                ok = False
                try:
                    if pid_alive(pid):
                        create_time = psutil.Process(pid).create_time()
                        started_at = float(info.get("started_at") or 0)
                        if abs(create_time - started_at) < 60:
                            ok = True
                except Exception:
                    ok = False
                if ok:
                    _background_process_info.setdefault(str(tid), {})[str(pid)] = info
                    restored += 1
                else:
                    dropped += 1
        if restored or dropped:
            # 回写：文件只剩存活条目（同时完成旧格式升级）
            _persist_background_processes_locked()
    if restored:
        print(f"[Shell] Restored {restored} background process(es) from previous run")
    return restored


def register_orphan_process(info: dict, session_id: int = None) -> str:
    """Register a background process with no task_id yet (late-binding pool).

    返回 orphan_id；pid 缺失时不登记（无法监控/终止的条目没有意义）。
    """
    global _orphan_counter
    pid = info.get("pid")
    if not pid:
        return ""
    with _orphan_process_lock:
        _orphan_counter += 1
        oid = f"orphan_{int(info.get('started_at') or time.time())}_{_orphan_counter}"
        entry = dict(info)
        entry["session_id"] = session_id or entry.get("session_id", 1) or 1
        _orphan_process_info[oid] = entry
    return oid


def get_orphan_processes() -> dict:
    """Return dict of orphan background processes: {orphan_id: info}."""
    with _orphan_process_lock:
        return dict(_orphan_process_info)


def cleanup_orphan_process(orphan_id: str):
    """Remove an orphan process from tracking."""
    with _orphan_process_lock:
        _orphan_process_info.pop(orphan_id, None)


def cleanup_orphan_pid(pid) -> bool:
    """Remove any orphan entry matching this pid. Returns True if removed."""
    removed = False
    with _orphan_process_lock:
        for oid, info in list(_orphan_process_info.items()):
            if info.get("pid") == pid:
                _orphan_process_info.pop(oid, None)
                removed = True
    return removed


def adopt_orphan_processes(task_id: int, session_id: int = None) -> int:
    """
    Move orphan processes matching the given task_id/session_id from the
    orphan pool into the main background_process_info dict.

    一任务多进程：认领按 pid 入槽，多个 orphan 全部保留（旧写法互相
    覆盖只剩最后一个）。带 "detached" 标记的条目是 BgMonitor 主动脱离
    监控的冻结进程，不参与认领，避免刚 detach 又被认领回去。

    Returns the number of processes adopted.
    """
    adopted = 0
    now = time.time()
    with _orphan_process_lock:
        to_adopt = []
        for oid, info in list(_orphan_process_info.items()):
            if info.get("detached"):
                continue
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
            pid = info.get("pid")
            if not pid:
                continue
            with _background_process_lock:
                _background_process_info.setdefault(str(task_id), {})[str(pid)] = info
                _persist_background_processes_locked()
            adopted += 1

    if adopted:
        import sys
        print(f"[Shell] Adopted {adopted} orphan process(es) → task #{task_id}", file=sys.stderr, flush=True)
    return adopted


def _is_background_command(command: str) -> bool:
    """Detect commands that self-background and return immediately.

    Only two cases count as background:
      1. The Windows `start` builtin as the FIRST token of the command
         (e.g. `start chrome`, `start /min cmd /c ...`). `start` is a cmd.exe
         builtin that launches a detached process and returns immediately,
         so waiting for it would be pointless.
      2. A trailing `&` (Unix shell background operator).

    Explicitly NOT background:
      - `npm start` / `yarn start` — `start` is an npm subcommand argument,
        not the Windows builtin; the process runs in the foreground.
      - `start.py` / `python start.py` — a filename, not the bare builtin.
      - `echo start` / `"start"` mid-command — `start` not in first position.
      - `cmd /c start notepad` — first token is `cmd`; the cmd process itself
        returns immediately, so foreground handling is correct anyway.
    """
    stripped = command.strip()
    if not stripped:
        return False
    # Windows `start` builtin: must be the first token, followed by
    # whitespace or end-of-command. `start.py` / `startx` do not match.
    if re.match(r'^start(?:\s|$)', stripped, re.IGNORECASE):
        return True
    # Unix background operator
    if stripped.endswith('&'):
        return True
    return False


# Whole-line interactive prompt patterns, matched against the last non-empty
# line of a still-running process's output. Deliberately precise so progress
# output like "Progress: 50%" or "Address: 1.2.3.4" is NOT flagged.
_INTERACTIVE_LINE_PATTERNS = [
    re.compile(r'(?:mysql|sqlite|psql|mongo|redis|gdb|irb)>\s*$', re.IGNORECASE),  # DB/GDB CLIs
    re.compile(r'>>>\s*$'),                    # Python REPL prompt
    re.compile(r'^\.\.\.\s*$'),                # Python continuation prompt (whole line only)
    re.compile(r'^In \[\d*\]:\s*$'),           # IPython
    re.compile(r'^>\s*$'),                     # llama.cpp / Ollama CLI bare prompt
    re.compile(r'(?:password|passphrase|login|username)[^:\n]*:\s*$', re.IGNORECASE),  # login prompts
    re.compile(r'\S+@\S+:[^\s]*[$#]\s*$'),     # bash-style prompt: user@host:~$ / root@host:~#
]


def _detect_interactive_prompt(tail_bytes: bytes) -> bool:
    """Return True if the output tail ends at an interactive prompt.

    Only whole-line prompt patterns are accepted (checked against the last
    non-empty line), so progress/log output containing "Progress: 50%",
    "Address: ..." or "key: value" no longer triggers a false positive.
    """
    try:
        text = tail_bytes.decode("utf-8", errors="replace")
    except Exception:
        return False
    # Treat \r as a line break too (progress bars overwrite via \r).
    last = ""
    for line in reversed(text.replace("\r", "\n").split("\n")):
        if line.strip():
            last = line.strip()
            break
    if not last:
        return False
    return any(p.search(last) for p in _INTERACTIVE_LINE_PATTERNS)


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
        # 日志文件可能逐行混杂 UTF-8/GBK（cmd 内建 vs python 子进程），
        # 按行解码避免整块二选一时必乱一半
        text = _decode_mixed(raw)
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
