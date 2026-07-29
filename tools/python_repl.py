import subprocess
import os
import tempfile
from typing import Any, Dict
from tools.base import BaseTool


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


def _register_surviving_descendants(root_pid: int, task_id, session_id) -> int:
    """枚举脚本 pid 的递归子孙进程中仍存活者，逐个登记到进程追踪表。

    execute_python 的临时脚本退出/被终止后，它用 subprocess.Popen 起的
    后台进程（ffmpeg、服务等）会继续运行——此前完全失联。有 task_id 走
    主表（随任务监控/终止），没有走 orphan 池（带 session_id，等迟到
    绑定）。脚本本身可能已退出，psutil 的 children() 对死 pid 不可用，
    因此拍全量 ppid 图后从 root_pid 向下 BFS（best effort：父链断掉的
    更深层后代无法找回）。返回登记数量；任何异常都吞掉——登记是兜底，
    不能反过来影响代码执行。
    """
    try:
        import psutil
        from tools.shell import register_background_process, register_orphan_process
        # 拍 ppid 全图（父进程可能已退出，无法走 parent.children()）
        children_map = {}
        for p in psutil.process_iter(["pid", "name", "create_time"]):
            try:
                ppid = p.ppid()
                if ppid:
                    children_map.setdefault(ppid, []).append(p)
            except Exception:
                continue
        surviving = []
        stack = list(children_map.get(root_pid, []))
        seen = {root_pid}
        while stack:
            p = stack.pop()
            if p.pid in seen:
                continue
            seen.add(p.pid)
            surviving.append(p)
            stack.extend(children_map.get(p.pid, []))
        count = 0
        for p in surviving:
            try:
                if p.status() == psutil.STATUS_ZOMBIE:
                    continue
                try:
                    cmd = " ".join(str(c) for c in p.cmdline())[:200]
                except Exception:
                    cmd = p.name()
                info = {
                    "pid": p.pid,
                    "output_file": "",
                    "command": f"[execute_python] {cmd or p.name()}",
                    "started_at": p.create_time(),
                    "timeout": 0,
                    "alive": True,
                    "source": "execute_python",
                }
                if task_id:
                    register_background_process(task_id, info)
                else:
                    register_orphan_process(info, session_id=session_id)
                count += 1
            except Exception:
                continue
        return count
    except Exception:
        return 0


class PythonREPLTool(BaseTool):
    name: str = "execute_python"
    description: str = "执行 Python 代码并返回 stdout/stderr。"

    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": "要执行的 Python 代码。"
                        }
                    },
                    "required": ["code"]
                }
            }
        }

    def _check_dangerous_python(self, code: str) -> str:
        """Block Python code that would kill the Open-AGC server process itself.

        Only blocks when the target is the server/parent (suicide) or when the
        command would kill ALL python processes (pkill/killall python).
        Ordinary kill commands targeting non-protected processes are allowed.
        """
        import re
        cmd_lower = code.lower()

        # ── Block only mass-kill commands that hit all python processes ──
        mass_kill_patterns = [
            (r'''["\']pkill["\'].*["\']python["\']''', "禁止 pkill python（会杀死所有 python 进程，包括 Open-AGC 自身）"),
            (r'''["\']killall["\'].*["\']python["\']''', "禁止 killall python（会杀死所有 python 进程）"),
            (r'''["\']pkill["\'].*-f.*["\'](?:uvicorn|open.agc|api\.server)["\']''', "禁止按进程名批量终止服务器进程"),
        ]
        for pattern, reason in mass_kill_patterns:
            if re.search(pattern, code, re.IGNORECASE):
                return f"⛔ 该 Python 代码被阻止执行：{reason}\n\n被阻止的代码：{code[:300]}\n"

        # ── Scan PIDs in kill commands ──
        # Extract PIDs from: subprocess.run(["kill", "-9", "PID"]) or os.kill(PID, ...)
        pid_sources = []
        # From subprocess.run/call/Popen with kill command
        pid_sources += re.findall(r'''["\']kill["\'][\s\S]{0,60}?["\'](\d+)["\']''', code, re.IGNORECASE)
        # From os.kill(PID, ...)
        pid_sources += re.findall(r'''os\.kill\s*\(\s*(\d+)''', code, re.IGNORECASE)

        if pid_sources:
            from api.state import check_protected_pid
            for pid_str in pid_sources:
                try:
                    target_pid = int(pid_str)
                    if target_pid > 0 and check_protected_pid(target_pid):
                        return (
                            f"⛔ 该 Python 代码被阻止执行：PID {target_pid} "
                            f"是 Open-AGC 服务进程或其父进程，终止它会导致服务崩溃。\n\n"
                            f"被阻止的代码：{code[:300]}\n"
                        )
                except Exception:
                    pass

        return ""

    def execute(self, **kwargs) -> str:
        code = kwargs.get("code")
        if not code:
            return "Error: No python code provided."

        # ── Self-preservation ──
        blocked = self._check_dangerous_python(code)
        if blocked:
            return blocked

        # ── Secrets substitution ──
        # Real values replace {{secret:name.field}} placeholders ONLY in the code
        # written to the temp file for the child process. The placeholder version
        # (`code`) is what appears in logs, context and messages.
        try:
            from core.secrets import substitute_refs
            exec_code = substitute_refs(code)
        except Exception:
            exec_code = code

        # Read sandbox config
        import json
        from core.paths import get_data_path
        cwd = None
        config_path = get_data_path("config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                if config.get("sandbox_mode", True):
                    sandbox_dir = config.get("sandbox_dir", os.path.abspath(os.path.join(os.getcwd(), "workspace")))
                    os.makedirs(sandbox_dir, exist_ok=True)
                    cwd = sandbox_dir
            except Exception:
                pass

        # Create a temporary file to run the python code cleanly
        # Keep it in the system temp folder to avoid triggering uvicorn WatchFiles
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as temp:
            temp.write(exec_code)
            temp_path = temp.name

        try:
            # Note: For production Open-AGC, this should run in a docker container or restricted environment.
            # Using current python environment for simplicity.
            import sys
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"

            proc = subprocess.Popen(
                [sys.executable, temp_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                cwd=cwd,
                env=env,
                # POSIX: detach into its own process group so the timeout
                # killpg below can't take down the Open-AGC server itself.
                **({} if sys.platform == "win32" else {"start_new_session": True}),
            )

            try:
                stdout, stderr = proc.communicate(timeout=60)
                output = ""
                if stdout:
                    output += f"STDOUT:\n{stdout}\n"
                if stderr:
                    output += f"STDERR:\n{stderr}\n"
                output += f"Exit Code: {proc.returncode}"

                # 脚本已退出——登记它遗留的存活子孙进程（Popen 起的后台
                # 进程不再失联，可在进程管理中查看/终止）
                _survivors = _register_surviving_descendants(
                    proc.pid,
                    kwargs.get("_task_id") or kwargs.get("task_id", 0),
                    kwargs.get("_session_id", 1) or 1)

                # Detect background service launches
                import re as _re_ps
                if _re_ps.search(r'\b(Popen|run\b.*start|subprocess\b.*start)', code):
                    output += ("\n[SERVER_PROCESS] Python代码启动了后台进程，"
                               "进程可能仍在运行。如需等待可调用 pause_and_wait。")
                if _survivors:
                    output += (f"\n[PROCESS_TRACKED] 已登记 {_survivors} 个存活子进程，"
                               "可在进程管理中查看/终止。")
                return _mask(output)

            except subprocess.TimeoutExpired:
                # Kill the entire process tree (avoids orphan ffmpeg etc.)
                try:
                    if sys.platform == "win32":
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                       capture_output=True, timeout=5)
                    else:
                        import signal
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass
                proc.kill()
                proc.wait(timeout=3)
                # taskkill /T 后仍可能有脱缰的子孙存活（detached 启动等），登记之
                _survivors = _register_surviving_descendants(
                    proc.pid,
                    kwargs.get("_task_id") or kwargs.get("task_id", 0),
                    kwargs.get("_session_id", 1) or 1)
                output = (proc.stdout.read() or "") if proc.stdout else ""
                output += "\n\nError: Python execution timed out after 60 seconds. "
                output += "后台进程（如 ffmpeg/Popen）的资源句柄阻止了脚本退出，已强制终止。"
                output += "\n如需启动长期运行的进程，请使用 execute_shell + detach=True，"
                output += "或在 Python 中将 stdout/stderr 重定向到 subprocess.DEVNULL。"
                if _survivors:
                    output += (f"\n[PROCESS_TRACKED] 已登记 {_survivors} 个存活子进程，"
                               "可在进程管理中查看/终止。")
                return _mask(output)

        except Exception as e:
            return f"Error executing python code: {_mask(str(e))}"
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
