# -*- coding: utf-8 -*-
"""终态收割与发现进程外来树排除的回归测试。

生产实证 #584：任务死于 LLM_ERROR 后 detach 的 powershell/cmd 无人收割；
sandbox 在仓库内时 VS Code 打开子目录会让 Code.exe/java.exe 被误列进
「发现的进程」并给出终止按钮（点到就杀了用户的编辑器）。
"""
import pytest

import api.task_core as tc
from api.routes import routes_tasks as rt


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import api.db as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "test.db"))
    db_mod.init_db()
    return db_mod


def _insert(db_mod):
    conn = db_mod.db_connect()
    cur = conn.execute(
        "INSERT INTO tasks (title, user_query, status, task_type, session_id) "
        "VALUES ('t', 'q', 'running', 'oneshot', 1)")
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def test_llm_error_failure_reaps_processes(db, monkeypatch):
    calls = []
    monkeypatch.setattr(tc, "kill_tracked_background_process",
                        lambda tid, notify=True: calls.append(tid) or [1234])
    tid = _insert(db)
    assert tc.handle_task_completion(tid, "[LLM_ERROR] 连不上模型服务", [], session_id=1) == 'failed'
    assert calls == [tid]


def test_completed_does_not_reap(db, monkeypatch):
    calls = []
    monkeypatch.setattr(tc, "kill_tracked_background_process",
                        lambda tid, notify=True: calls.append(tid) or [])
    tid = _insert(db)
    assert tc.handle_task_completion(tid, "任务完成，已保存到 outputs/x.md。", [],
                                     session_id=1) == 'completed'
    assert calls == []


def test_user_interrupt_reaps(db, monkeypatch):
    calls = []
    monkeypatch.setattr(tc, "kill_tracked_background_process",
                        lambda tid, notify=True: calls.append(tid) or [1234])
    tid = _insert(db)
    assert tc.handle_task_completion(tid, "Task interrupted by user.", [],
                                     session_id=1) == 'interrupted_user'
    assert calls == [tid]


# ── 发现进程的外来树排除 ──

class _FakeProc:
    def __init__(self, pid, name, cwd, parent):
        self.pid = pid
        self._name = name
        self._cwd = cwd
        self._parent = parent

    def name(self):
        return self._name

    def cwd(self):
        return self._cwd

    def cmdline(self):
        return [self._name]

    def parent(self):
        return self._parent

    def create_time(self):
        return 1000.0


_SANDBOX = r"D:\GitHub\open-agc\workspace"


def test_foreign_tree_excluded(monkeypatch):
    """VS Code 窗口开在 sandbox 子目录：其子进程（java 语言服务）不得误列。"""
    code_main = _FakeProc(1, "Code.exe", r"C:\Program Files\VSCode", None)
    code_win = _FakeProc(2, "Code.exe", _SANDBOX + r"\zxsai-server", code_main)
    java = _FakeProc(3, "java.exe", _SANDBOX + r"\zxsai-server", code_win)
    assert not rt._has_foreign_alive_ancestor(code_win, _SANDBOX) is False  # 外来
    assert rt._has_foreign_alive_ancestor(java, _SANDBOX)                      # 外来
    assert rt._has_foreign_alive_ancestor(code_win, _SANDBOX)                  # 外来


def test_orphan_and_own_tree_included(monkeypatch):
    """父进程已死（孤儿）或父链通到本服务 → 是 agent 遗留，应列出。"""
    import api.state as st
    monkeypatch.setattr(st, "_server_pid", 999, raising=False)
    orphan = _FakeProc(10, "powershell.exe", _SANDBOX, None)
    assert not rt._has_foreign_alive_ancestor(orphan, _SANDBOX)
    server_child = _FakeProc(999, "python.exe", r"D:\GitHub\open-agc", None)
    agent_cmd = _FakeProc(11, "cmd.exe", _SANDBOX, server_child)
    assert not rt._has_foreign_alive_ancestor(agent_cmd, _SANDBOX)
