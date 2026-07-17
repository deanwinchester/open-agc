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
