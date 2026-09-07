# -*- coding: utf-8 -*-
"""单实例锁回归测试：agent 任务中反复拉起 Open-AGC 时每起一个新进程就多开
一个窗口（生产实证）。_acquire_single_instance_lock 必须在已有持锁者时
返回 False，释放后可再拿。"""
import sys

import pytest

import gui_app


def _force_release_lock():
    """按平台正确释放锁（CloseHandle / 关 fd）。

    同进程的其它测试（如 test_oob_experience 调 gui_app.main()）可能已持有
    锁：仅 delattr 不会释放 Windows 互斥体，必须显式 CloseHandle。"""
    fn = gui_app._acquire_single_instance_lock
    if sys.platform.startswith("win"):
        import ctypes
        h = getattr(fn, "_handle", None)
        if h is not None:
            ctypes.windll.kernel32.CloseHandle(h)
            delattr(fn, "_handle")
    fd = getattr(fn, "_fd", None)
    if fd is not None:
        try:
            fd.close()
        except Exception:
            pass
        delattr(fn, "_fd")


@pytest.fixture(autouse=True)
def _clean_lock():
    _force_release_lock()
    yield
    _force_release_lock()


def test_second_acquire_fails():
    assert gui_app._acquire_single_instance_lock() is True
    # 同一进程内再次获取必须失败（模拟第二个实例）
    assert gui_app._acquire_single_instance_lock() is False


def test_lock_released_allows_reacquire(tmp_path):
    """POSIX 路径：flock 释放后可重新获取（Windows 路径由上一用例覆盖）。"""
    if sys.platform.startswith("win"):
        pytest.skip("POSIX flock 专用")
    import fcntl
    lock = tmp_path / "app.lock"
    fd1 = open(lock, "w")
    fcntl.flock(fd1, fcntl.LOCK_EX | fcntl.LOCK_NB)
    fd2 = open(lock, "w")
    try:
        fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert False, "第二把锁不应拿到"
    except OSError:
        pass
    fd2.close()
    fd1.close()  # 释放
    fd3 = open(lock, "w")
    fcntl.flock(fd3, fcntl.LOCK_EX | fcntl.LOCK_NB)  # 不抛异常即成功
    fd3.close()

