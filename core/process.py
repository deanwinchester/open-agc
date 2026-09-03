"""Cross-platform process helpers.

`os.kill(pid, 0)` is a POSIX idiom for liveness checks, but on Windows CPython
implements `os.kill` for any non-CTRL signal via TerminateProcess — i.e. the
"liveness check" actually kills the target process. Use `pid_alive()` instead.

Similarly, killing a whole process tree must not rely on POSIX process groups
(`os.killpg` without `start_new_session` would SIGKILL the server itself).
"""
import subprocess
import sys

import psutil


def pid_alive(pid: int) -> bool:
    """Return True if a process with `pid` exists and is not a zombie."""
    if not pid or pid <= 0:
        return False
    try:
        if not psutil.pid_exists(pid):
            return False
        proc = psutil.Process(pid)
        return proc.status() != psutil.STATUS_ZOMBIE
    except (psutil.Error, OSError):
        return False


def pid_alive_as(pid: int, started_at: float = None, tolerance: float = 120.0) -> bool:
    """判活并校验进程身份：pid 存在且其创建时间与登记的 started_at 一致。

    Windows 上 pid 复用很常见——原进程退出后 pid 被无关进程占用，裸
    `pid_alive` 会恒判活，导致后台进程条目永不收割、等待中的任务永不
    唤醒。started_at 为 None 时退化为普通 pid_alive。
    """
    if not pid_alive(pid):
        return False
    if not started_at:
        return True
    try:
        create_time = psutil.Process(pid).create_time()
    except (psutil.Error, OSError):
        return False
    return abs(create_time - started_at) <= tolerance


def kill_tree(pid: int) -> None:
    """Terminate the process and all of its children (best effort)."""
    if not pid or pid <= 0:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=10)
        except Exception:
            pass
        return
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.kill()
            except psutil.Error:
                pass
        try:
            parent.kill()
        except psutil.Error:
            pass
    except psutil.Error:
        pass
