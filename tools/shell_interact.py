"""Tool for sending input to interactive shell processes (shell_send)."""
import os
import time
from typing import Any, Dict, Optional

from tools.base import BaseTool


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class ShellSendTool(BaseTool):
    name: str = "shell_send"
    description: str = (
        "向正在交互模式运行的进程发送输入并读取输出。\n"
        "用于与 python、mysql、llama-cli 等交互式命令行程序进行对话。\n\n"
        "用法：\n"
        "1. 先执行 execute_shell 启动交互式命令\n"
        "2. 收到 [Interactive] PID xxx 后，用此工具发送输入\n"
        "3. 用 exit/quit 退出后，进程自动结束\n\n"
        "注意：如果进程已结束或 PID 无效，工具会返回错误信息。"
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
                            "description": "交互进程的 PID（从 execute_shell 返回的 [Interactive] 消息中获取）",
                        },
                        "input": {
                            "type": "string",
                            "description": "要发送给进程的输入文本（会自动追加换行）",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "等待输出的超时秒数（默认 30，最长 120）",
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

        # Read new output from the process's output file
        info = None
        for tid, p in get_background_processes().items():
            if p.get("pid") == pid:
                info = p; break
        out_file = info.get("output_file", "") if info else ""

        if out_file and os.path.exists(out_file):
            # Wait briefly for output to appear
            time.sleep(0.5)
            try:
                with open(out_file, "r", encoding="utf-8", errors="replace") as f:
                    return f"[shell_send] 进程输出:\n{f.read()[-3000:]}"
            except Exception:
                pass

        return f"[shell_send] 输入已发送（PID {pid}）。"
