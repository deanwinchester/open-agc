# -*- coding: utf-8 -*-
"""单实例锁回归测试：agent 任务中反复拉起 Open-AGC 时每起一个新进程就多开
一个窗口（生产实证）。_acquire_single_instance_lock 必须在已有持锁者时
返回 False，释放后可再拿。"""
import importlib

import gui_app


def test_second_acquire_fails():
    # 模块级句柄可能已被本进程其他测试持有——先清掉保证可重复
    for attr in ("_handle", "_fd"):
        if hasattr(gui_app._acquire_single_instance_lock, attr):
            obj = getattr(gui_app._acquire_single_instance_lock, attr)
            try:
                if attr == "_fd":
                    obj.close()
            except Exception:
                pass
            delattr(gui_app._acquire_single_instance_lock, attr)

    assert gui_app._acquire_single_instance_lock() is True
    # 同一进程内再次获取必须失败（模拟第二个实例）
    assert gui_app._acquire_single_instance_lock() is False


def test_lock_released_allows_reacquire(tmp_path, monkeypatch):
    """POSIX 路径：flock 释放后可重新获取（Windows 路径由上一用例覆盖）。"""
    import sys
    if sys.platform.startswith("win"):
        import pytest
        pytest.skip("POSIX flock 专用")
    import fcntl, os
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
