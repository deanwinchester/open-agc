"""Tool for sending input to interactive shell processes (shell_send)."""
import os
import time
from typing import Any, Dict, Optional

from tools.base import BaseTool

# Track per-PID read positions so we don't re-read old output
_interact_positions: Dict[int, int] = {}

def _is_pid_alive(pid: int) -> bool:
    # os.kill(pid, 0) would TERMINATE the process on Windows — use psutil instead.
    from core.process import pid_alive
    return pid_alive(pid)


class ShellSendTool(BaseTool):
    name: str = "shell_send"
    description: str = (
        "向交互中的进程发输入并读新输出。"
        "execute_shell 返回 [Interactive] PID 后用它续聊（python、mysql 等）。"
    )

    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pid": {
                            "type": "integer",
                            "description": "交互进程 PID（来自 [Interactive] 消息）。",
                        },
                        "input": {
                            "type": "string",
                            "description": "输入文本（自动追加换行）。",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "等待输出秒数（默认 30，最长 120）。",
                        },
                    },
                    "required": ["pid", "input"],
                },
            },
        }

    def execute(self, pid: int, input: str, timeout: int = 30, **kwargs) -> str:
        if not pid:
            return "[shell_send] 需要 pid 参数。"
        timeout = max(5, min(timeout, 120))

        # ══ Security: only allow PIDs registered via [Interactive] ══
        from tools.shell import _interactive_procs, _interactive_procs_lock, get_background_processes
        stdin_pipe = None
        with _interactive_procs_lock:
            stdin_pipe = _interactive_procs.get(pid)

        if stdin_pipe is None:
            return f"[shell_send] PID {pid} 不是有效的交互进程。"

        # Verify process is alive
        if not _is_pid_alive(pid):
            with _interactive_procs_lock:
                _interactive_procs.pop(pid, None)
            return f"[shell_send] PID {pid} 已结束。"

        if stdin_pipe.closed:
            return f"[shell_send] PID {pid} 的 stdin 管道已关闭，进程可能已退出。"

        # Write input to the process stdin via the pipe
        try:
            stdin_pipe.write((input + "\n").encode("utf-8"))
            stdin_pipe.flush()
        except Exception as e:
            with _interactive_procs_lock:
                _interactive_procs.pop(pid, None)
            return f"[shell_send] 写入失败: {e}"

        # Read new output since last read position
        out_file = ""
        for tid, p in get_background_processes().items():
            if p.get("pid") == pid:
                out_file = p.get("output_file", "")
                break

        if out_file and os.path.exists(out_file):
            last_pos = _interact_positions.get(pid, 0)
            # Poll for new output (up to timeout seconds)
            deadline = time.time() + timeout
            new_output = ""
            while time.time() < deadline:
                try:
                    cur_size = os.path.getsize(out_file)
                    if cur_size > last_pos:
                        with open(out_file, "rb") as f:
                            f.seek(last_pos)
                            raw = f.read(cur_size - last_pos)
                        _interact_positions[pid] = cur_size
                        try:
                            new_output = raw.decode("utf-8")
                        except UnicodeDecodeError:
                            new_output = raw.decode("utf-8", errors="replace")
                        break
                except OSError:
                    break
                if not _is_pid_alive(pid):
                    break
                time.sleep(0.2)

            if new_output:
                return f"[shell_send] 进程输出:\n{new_output}"
            return f"[shell_send] 输入已发送（PID {pid}），暂未收到新输出。"

        return f"[shell_send] 输入已发送（PID {pid}）。注意：由于进程输出未重定向到文件，" \
               f"请用 execute_shell 重新运行命令查看结果。"
