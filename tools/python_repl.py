import subprocess
import os
import tempfile
from typing import Any, Dict
from tools.base import BaseTool

class PythonREPLTool(BaseTool):
    name: str = "execute_python"
    description: str = "Execute Python code in an isolated environment and return the standard output/error."

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
                            "description": "The python code to execute."
                        }
                    },
                    "required": ["code"]
                }
            }
        }

    def _check_dangerous_python(self, code: str) -> str:
        """Block Python code that would kill the Open-AGC server process itself."""
        import re
        cmd_lower = code.lower()

        # Check for kill commands in subprocess.run/call/Popen/os.system
        kill_patterns = [
            # subprocess.run with kill
            (r'''["\']kill["\']\s*,\s*["\']-?\d*9?["\']''', "Python 代码中禁止执行 kill -9 命令"),
            (r'''["\']kill["\'][\s\S]{0,20}["\']-\d+["\']''', "Python 代码中禁止执行 kill 命令"),
            (r'''["\']killall["\']\s*,\s*["\'][^"\']*python[^"\']*["\']''', "禁止通过 Python 终止 python 进程"),
            (r'''["\']pkill["\']''', "禁止通过 Python 执行 pkill 命令"),
            (r'''os\.kill\s*\(\s*\d+''', "禁止通过 Python 的 os.kill 终止进程"),
            (r'''signal\.sigkill''', "禁止通过 Python 发送 SIGKILL 信号"),
            (r'''subprocess\.run\(.*["\']kill["\']''', "禁止在 Python 中调用 kill 命令"),
            (r'''subprocess\.Popen\(.*["\']kill["\']''', "禁止在 Python 中调用 kill 命令"),
            (r'''os\.system\s*\(.*kill''', "禁止在 Python 中通过 os.system 执行 kill 命令"),
            (r'''os\.popen\s*\(.*kill''', "禁止在 Python 中通过 os.popen 执行 kill 命令"),
        ]

        for pattern, reason in kill_patterns:
            if re.search(pattern, code):
                return (
                    f"⛔ 该 Python 代码被阻止执行：{reason}\n\n"
                    f"被阻止的代码被截断显示：{code[:300]}\n"
                )

        # Check for PID-based kills against the server process family
        # Scan ALL PIDs that appear near kill commands
        pid_matches = re.finditer(r'''["\']kill["\'][\s\S]{0,50}?["\'](\d+)["\']''', code, re.IGNORECASE)
        from api.state import check_protected_pid
        for m in pid_matches:
            try:
                target_pid = int(m.group(1))
                if target_pid > 0 and check_protected_pid(target_pid):
                    return (
                        f"⛔ 该 Python 代码被阻止执行：PID {target_pid} "
                        f"是 Open-AGC 服务进程或其父进程，终止它会导致服务崩溃。\n\n"
                        f"被阻止的代码被截断显示：{code[:300]}\n"
                    )
            except (ImportError, ValueError, Exception):
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
            temp.write(code)
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
            )

            try:
                stdout, stderr = proc.communicate(timeout=60)
                output = ""
                if stdout:
                    output += f"STDOUT:\n{stdout}\n"
                if stderr:
                    output += f"STDERR:\n{stderr}\n"
                output += f"Exit Code: {proc.returncode}"

                # Detect background service launches
                import re as _re_ps
                if _re_ps.search(r'\b(Popen|run\b.*start|subprocess\b.*start)', code):
                    output += ("\n[SERVER_PROCESS] Python代码启动了后台进程，"
                               "进程可能仍在运行。如需等待可调用 pause_and_wait。")
                return output

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
                output = (proc.stdout.read() or "") if proc.stdout else ""
                output += "\n\nError: Python execution timed out after 60 seconds. "
                output += "后台进程（如 ffmpeg/Popen）的资源句柄阻止了脚本退出，已强制终止。"
                output += "\n如需启动长期运行的进程，请使用 execute_shell + detach=True，"
                output += "或在 Python 中将 stdout/stderr 重定向到 subprocess.DEVNULL。"
                return output

        except Exception as e:
            return f"Error executing python code: {str(e)}"
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
