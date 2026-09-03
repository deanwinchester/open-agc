"""后台进程追踪覆盖不全 修复回归测试。

根因与修复对应关系：
- A. 注册表一任务一槽位互相覆盖 → {task_id: {pid: info}} 多进程结构，
  register/get/cleanup/kill_for_task 访问器；BgMonitor 按 pid 逐个判活，
  全死才恢复；冻结解除追踪改为移入 orphan 池（detached 标记，不丢弃）。
- B. 纯内存注册表重启失忆 → 注册/注销写-through 到
  data/background_processes.json；restore_background_processes 启动复活
  （pid 存活 + create_time 与 started_at 误差 < 60s 防 pid 复用误判）。
- C. execute_python 不接入追踪 → 脚本退出/超时后枚举递归子孙中存活者，
  逐个登记（有 task_id 走主表，否则 orphan 池带 session_id）。
- D. 进程页无 OS 兜底 → GET /api/processes 增加 discovered 分区（cwd/
  cmdline 命中 sandbox 的未追踪进程）；POST /api/processes/{pid}/kill
  服务端重校验 sandbox 归属后才 kill_tree。
"""
import asyncio
import json
import os
import sqlite3
import sys
import threading
import types
from datetime import datetime, timedelta, timezone

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ---------- 公共 fixture ----------

@pytest.fixture(autouse=True)
def _isolated_bg_store(tmp_path, monkeypatch):
    """注册表写-through 持久化重定向到临时目录，不碰真实 data/。"""
    import tools.shell as sh
    monkeypatch.setattr(sh, "_BG_STORE_PATH", str(tmp_path / "background_processes.json"))


@pytest.fixture(autouse=True)
def _clean_registries():
    """每个测试前后清空真实注册表/orphan 池，避免跨测试污染。"""
    import tools.shell as sh
    with sh._background_process_lock:
        sh._background_process_info.clear()
    with sh._orphan_process_lock:
        sh._orphan_process_info.clear()
    yield
    with sh._background_process_lock:
        sh._background_process_info.clear()
    with sh._orphan_process_lock:
        sh._orphan_process_info.clear()


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """把 api.db.DB_PATH 指到临时库；routes_tasks 持有 DB_PATH 值引用，同步指过去。"""
    import api.db as db_mod
    db_file = str(tmp_path / "test.db")
    monkeypatch.setattr(db_mod, "DB_PATH", db_file)
    db_mod.init_db()
    import api.routes.routes_tasks as rt
    monkeypatch.setattr(rt, "DB_PATH", db_file)
    return db_mod


def _insert_task(db_mod, status="backgrounded", session_id=1):
    conn = db_mod.db_connect()
    cur = conn.execute(
        "INSERT INTO tasks (title, user_query, status, task_type, session_id) "
        "VALUES (?, ?, ?, 'oneshot', ?)",
        ("测试任务", "原始查询", status, session_id))
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def _task_row(db_mod, tid):
    conn = db_mod.db_connect()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status, resume_count, interruption_reason, wake_at FROM tasks WHERE id=?",
        (tid,)).fetchone()
    conn.close()
    return dict(row) if row else None


class _FakeProc:
    """psutil.Process 替身：只实现测试用到的方法。"""

    def __init__(self, pid, ppid=0, name="proc", cmdline=None, cwd=None,
                 create_time=1000.0, status="running"):
        self.pid = pid
        self._ppid = ppid
        self._name = name
        self._cmdline = cmdline if cmdline is not None else [name]
        self._cwd = cwd
        self._create_time = create_time
        self._status = status

    def ppid(self):
        return self._ppid

    def name(self):
        return self._name

    def cmdline(self):
        return self._cmdline

    def cwd(self):
        if self._cwd is None:
            import psutil
            raise psutil.AccessDenied(pid=self.pid)
        return self._cwd

    def create_time(self):
        return self._create_time

    def status(self):
        return self._status


def _install_monitor_harness(monkeypatch, bg, run_rounds=8):
    """monitor_loop 跑真线程、恢复 worker 走假线程；sleep 改为计数闸门。
    （与 tests/test_s7t4_patrol_governance.py 的 harness 同款）。"""
    spawned = []
    rounds_done = threading.Event()
    release_loop = threading.Event()
    rounds = {"n": 0}

    def _thread_factory(target=None, args=(), daemon=None, **kw):
        if getattr(target, "__name__", "") == "monitor_loop":
            return threading.Thread(target=target, daemon=True)
        spawned.append({"target": target, "args": args})
        return types.SimpleNamespace(start=lambda: None)

    monkeypatch.setattr(bg, "threading", types.SimpleNamespace(Thread=_thread_factory))

    def _fake_sleep(seconds):
        rounds["n"] += 1
        if rounds["n"] >= run_rounds:
            rounds_done.set()
            release_loop.wait(timeout=30)
            threading.Event().wait()  # 泊住（daemon，随进程退出）

    monkeypatch.setattr(bg._time, "sleep", _fake_sleep)
    return spawned, rounds_done, release_loop


def _seed_info(pid, **over):
    info = {"pid": pid, "output_file": "", "command": f"cmd {pid}",
            "started_at": 1000.0, "timeout": 0, "alive": True}
    info.update(over)
    return info


# ---------- A. 一任务多进程注册表 ----------

class TestMultiProcessRegistry:
    def test_register_two_processes_same_task_no_overwrite(self):
        """同一任务登记第二个进程不再顶掉第一个（28 次重试 28 个进程全可见）。"""
        import tools.shell as sh
        sh.register_background_process(7, _seed_info(900001))
        sh.register_background_process(7, _seed_info(900002))
        procs = sh.get_background_processes_for_task(7)
        assert set(procs.keys()) == {"900001", "900002"}
        assert procs["900001"]["command"] == "cmd 900001"
        assert procs["900002"]["command"] == "cmd 900002"

    def test_cleanup_single_pid_keeps_others(self):
        import tools.shell as sh
        sh.register_background_process(7, _seed_info(900001))
        sh.register_background_process(7, _seed_info(900002))
        sh.cleanup_background_process("7", 900001)
        assert set(sh.get_background_processes_for_task(7).keys()) == {"900002"}

    def test_cleanup_task_removes_whole_group(self):
        import tools.shell as sh
        sh.register_background_process(7, _seed_info(900001))
        sh.register_background_process(7, _seed_info(900002))
        sh.cleanup_background_process("7")
        assert sh.get_background_processes_for_task(7) == {}
        assert "7" not in sh.get_background_processes()

    def test_register_without_pid_or_task_is_noop(self):
        import tools.shell as sh
        sh.register_background_process(0, _seed_info(900001))
        sh.register_background_process(7, {"command": "no pid"})
        assert sh.get_background_processes() == {}


class TestKillGroup:
    def test_kill_for_task_kills_all_pids_then_pops(self, monkeypatch):
        """杀整组：两个 pid 都被 kill_tree，返回完整 pid 列表，整组清出。"""
        import tools.shell as sh
        sh.register_background_process(7, _seed_info(900001))
        sh.register_background_process(7, _seed_info(900002))
        killed = []
        monkeypatch.setattr(sh, "kill_tree", lambda pid: killed.append(pid))
        pids = sh.kill_background_process_for_task(7)
        assert sorted(pids) == [900001, 900002]
        assert sorted(killed) == [900001, 900002]
        assert sh.get_background_processes_for_task(7) == {}

    def test_kill_failure_on_one_pid_still_kills_rest_then_raises(self, monkeypatch):
        """单个 pid 杀失败不波及其他 pid；整组仍清表；首个异常清表后抛出。"""
        import tools.shell as sh
        sh.register_background_process(7, _seed_info(900001))
        sh.register_background_process(7, _seed_info(900002))

        def _boom_on_first(pid):
            if pid == 900001:
                raise RuntimeError("taskkill missing")
            killed.append(pid)

        killed = []
        monkeypatch.setattr(sh, "kill_tree", _boom_on_first)
        with pytest.raises(RuntimeError):
            sh.kill_background_process_for_task(7)
        assert killed == [900002]
        assert sh.get_background_processes_for_task(7) == {}


class TestOrphanAdopt:
    def test_adopt_multiple_orphans_keeps_all(self):
        """多个 orphan 认领到同一任务：各按 pid 入槽，不再互相覆盖。"""
        import tools.shell as sh
        import time as _t
        sh.register_orphan_process(_seed_info(900001, started_at=_t.time()), session_id=3)
        sh.register_orphan_process(_seed_info(900002, started_at=_t.time()), session_id=3)
        adopted = sh.adopt_orphan_processes(7, session_id=3)
        assert adopted == 2
        assert set(sh.get_background_processes_for_task(7).keys()) == {"900001", "900002"}
        assert sh.get_orphan_processes() == {}

    def test_detached_orphan_not_adopted(self):
        """detached 条目是 BgMonitor 主动脱离监控的，不被重新认领回任务。"""
        import tools.shell as sh
        import time as _t
        oid = sh.register_orphan_process(
            _seed_info(900001, started_at=_t.time(), detached=True), session_id=3)
        assert sh.adopt_orphan_processes(7, session_id=3) == 0
        assert oid in sh.get_orphan_processes()
        assert sh.get_background_processes_for_task(7) == {}


class TestDetach:
    def test_detach_moves_entry_to_orphan_pool(self):
        """冻结解除追踪 → 移入 orphan 池（detached 标记 + task_id），主表清空。"""
        import tools.shell as sh
        sh.register_background_process(7, _seed_info(900001, output_file="x.log"))
        oid = sh.detach_background_process("7", 900001)
        assert oid is not None
        assert sh.get_background_processes_for_task(7) == {}
        orphans = sh.get_orphan_processes()
        assert oid in orphans
        oinfo = orphans[oid]
        assert oinfo["detached"] is True
        assert oinfo["task_id"] == "7"
        assert oinfo["output_file"] == "x.log"

    def test_detach_missing_entry_returns_none(self):
        import tools.shell as sh
        assert sh.detach_background_process("7", 999999) is None


# ---------- B. 持久化 + 重启复活 ----------

def _read_store():
    import tools.shell as sh
    with open(sh._background_store_path(), "r", encoding="utf-8") as f:
        return json.load(f)


def _write_store(data):
    import tools.shell as sh
    with open(sh._background_store_path(), "w", encoding="utf-8") as f:
        json.dump(data, f)


class TestPersistence:
    def test_register_and_cleanup_write_through(self):
        """注册/注销即时写盘，文件内容与内存结构一致（嵌套 {task: {pid: info}}）。"""
        import tools.shell as sh
        sh.register_background_process(7, _seed_info(900001))
        sh.register_background_process(7, _seed_info(900002))
        data = _read_store()
        assert set(data.keys()) == {"7"}
        assert set(data["7"].keys()) == {"900001", "900002"}
        assert data["7"]["900001"]["command"] == "cmd 900001"
        sh.cleanup_background_process("7", 900001)
        assert set(_read_store()["7"].keys()) == {"900002"}
        sh.cleanup_background_process("7")
        assert _read_store() == {}

    def test_restore_revives_alive_pid_with_matching_create_time(self, monkeypatch):
        """pid 存活 + create_time 与 started_at 误差 < 60s → 复活登记。"""
        import tools.shell as sh
        import psutil
        _write_store({"7": {"900001": _seed_info(900001, started_at=1000.0)}})
        monkeypatch.setattr(sh, "pid_alive", lambda pid: True)
        monkeypatch.setattr(psutil, "Process",
                            lambda pid: _FakeProc(pid, create_time=1005.0))
        assert sh.restore_background_processes() == 1
        assert set(sh.get_background_processes_for_task(7).keys()) == {"900001"}
        # 回写后文件只剩存活条目
        assert set(_read_store()["7"].keys()) == {"900001"}

    def test_restore_drops_dead_pid(self, monkeypatch):
        import tools.shell as sh
        import psutil
        _write_store({"7": {"900001": _seed_info(900001, started_at=1000.0)}})
        monkeypatch.setattr(sh, "pid_alive", lambda pid: False)
        monkeypatch.setattr(psutil, "Process",
                            lambda pid: _FakeProc(pid, create_time=1000.0))
        assert sh.restore_background_processes() == 0
        assert sh.get_background_processes() == {}
        assert _read_store() == {}

    def test_restore_drops_reused_pid(self, monkeypatch):
        """pid 复用防护：pid 活着但 create_time 与记录差 > 60s → 是别的进程，剔除。"""
        import tools.shell as sh
        import psutil
        _write_store({"7": {"900001": _seed_info(900001, started_at=1000.0)}})
        monkeypatch.setattr(sh, "pid_alive", lambda pid: True)
        monkeypatch.setattr(psutil, "Process",
                            lambda pid: _FakeProc(pid, create_time=1000.0 + 3600))
        assert sh.restore_background_processes() == 0
        assert sh.get_background_processes() == {}
        assert _read_store() == {}

    def test_restore_upgrades_legacy_flat_format(self, monkeypatch):
        """旧版扁平格式 {task_id: info} 文件也能复活并升级为嵌套结构。"""
        import tools.shell as sh
        import psutil
        _write_store({"7": _seed_info(900001, started_at=1000.0)})
        monkeypatch.setattr(sh, "pid_alive", lambda pid: True)
        monkeypatch.setattr(psutil, "Process",
                            lambda pid: _FakeProc(pid, create_time=1000.0))
        assert sh.restore_background_processes() == 1
        assert set(sh.get_background_processes_for_task(7).keys()) == {"900001"}

    def test_restore_missing_or_bad_file_is_noop(self):
        import tools.shell as sh
        assert sh.restore_background_processes() == 0


class TestReconcileSkipsLiveTracked:
    def test_reconcile_leaves_task_with_live_process_to_bgmonitor(
            self, tmp_db, monkeypatch):
        """重启 reconcile：任务有存活的已登记进程 → 不置 interrupted，不起恢复。"""
        import api.background as bg
        import tools.shell as sh
        tid = _insert_task(tmp_db, status="backgrounded")
        sh.register_background_process(tid, _seed_info(900001))
        monkeypatch.setattr("tools.shell.pid_alive", lambda pid: True)
        # reconcile 判活走 core.process.pid_alive_as(pid, started_at)（校验
        # create_time）——局部 import 自 core.process，打源头
        monkeypatch.setattr("core.process.pid_alive_as",
                            lambda pid, started_at=None: True)
        spawned = []
        monkeypatch.setattr(bg, "threading", types.SimpleNamespace(
            Thread=lambda **kw: spawned.append(kw) or types.SimpleNamespace(start=lambda: None)))
        bg.reconcile_backgrounded_after_restart()
        row = _task_row(tmp_db, tid)
        assert row["status"] == "backgrounded"   # 留给 BgMonitor 接管
        assert spawned == []


# ---------- A. BgMonitor 按 pid 逐个判活 ----------

class TestBgMonitorMultiProcess:
    def test_partial_alive_no_resume_dead_entry_cleaned(self, tmp_db, monkeypatch):
        """一活一死：不恢复；死 pid 条目清掉，活条目继续监控。"""
        import api.background as bg
        import tools.shell as sh
        tid = _insert_task(tmp_db, status="backgrounded")
        sh.register_background_process(tid, _seed_info(900001))
        sh.register_background_process(tid, _seed_info(900002))
        monkeypatch.setattr(bg, "pid_alive_as",
                            lambda pid, started_at=None: pid == 900001)
        spawned, rounds_done, release_loop = _install_monitor_harness(
            monkeypatch, bg, run_rounds=3)
        bg.start_background_monitor()
        try:
            assert rounds_done.wait(timeout=20), "monitor loop did not run 3 rounds"
        finally:
            release_loop.set()
        assert spawned == []                                   # 部分存活 → 不恢复
        assert set(sh.get_background_processes_for_task(tid).keys()) == {"900001"}
        assert _task_row(tmp_db, tid)["status"] == "backgrounded"

    def test_all_dead_triggers_resume(self, tmp_db, monkeypatch):
        """全部 pid 死亡：才触发恢复（CAS 认领 → running，rc+1），整组清表。"""
        import api.background as bg
        import tools.shell as sh
        tid = _insert_task(tmp_db, status="backgrounded")
        sh.register_background_process(tid, _seed_info(900001, started_at=1.0))
        sh.register_background_process(tid, _seed_info(900002, started_at=2.0))
        monkeypatch.setattr(bg, "pid_alive", lambda pid: False)
        monkeypatch.setattr(bg, "_broadcast_task_history", lambda *a, **k: None)
        spawned, rounds_done, release_loop = _install_monitor_harness(
            monkeypatch, bg, run_rounds=3)
        bg.start_background_monitor()
        try:
            assert rounds_done.wait(timeout=20), "monitor loop did not run 3 rounds"
        finally:
            release_loop.set()
        assert len(spawned) == 1
        assert spawned[0]["target"] is bg._run_background_task
        assert sh.get_background_processes_for_task(tid) == {}
        row = _task_row(tmp_db, tid)
        assert row["status"] == "running"
        assert row["resume_count"] == 1

    def test_frozen_pid_detached_to_real_orphan_pool(self, tmp_db, monkeypatch, tmp_path):
        """输出冻结满阈值：真实 detach——主表清空、orphan 池出现 detached 条目
        （进程不丢弃、可见可杀），写兜底 wake_at，如实通知。"""
        import api.background as bg
        import tools.shell as sh
        from api.task_core import save_task_context, get_task_context
        monkeypatch.setattr(bg, "_STALL_FREEZE_ROUNDS", 3)
        tid = _insert_task(tmp_db, status="backgrounded")
        save_task_context(tid, [{"role": "user", "content": "原始任务"}])
        out_file = tmp_path / "out.log"
        out_file.write_text("partial", encoding="utf-8")
        import time as _t
        sh.register_background_process(tid, _seed_info(
            900001, output_file=str(out_file), started_at=_t.time()))
        monkeypatch.setattr(bg, "pid_alive_as", lambda pid, started_at=None: True)
        spawned, rounds_done, release_loop = _install_monitor_harness(
            monkeypatch, bg, run_rounds=8)
        bg.start_background_monitor()
        try:
            assert rounds_done.wait(timeout=20), "monitor loop did not run 8 rounds"
        finally:
            release_loop.set()
        assert sh.get_background_processes_for_task(tid) == {}
        orphans = sh.get_orphan_processes()
        assert len(orphans) == 1
        oinfo = list(orphans.values())[0]
        assert oinfo["detached"] is True
        assert oinfo["pid"] == 900001
        assert oinfo["task_id"] == str(tid)
        assert spawned == []                                   # 进程活着 → 不恢复
        assert out_file.exists()                               # 不删输出文件
        row = _task_row(tmp_db, tid)
        assert row["status"] == "backgrounded"
        assert row["wake_at"] is not None
        last = get_task_context(tid)[-1]["content"]
        assert "仍在运行" in last and "无输出" in last
        assert "脱离监控" in last


# ---------- C. execute_python 子孙进程登记 ----------

class TestPythonReplTracking:
    def test_descendants_registered_under_task(self, monkeypatch):
        """递归子孙（子+孙）全部登记到主表；无关进程不登记；zombie 跳过。"""
        import psutil
        import tools.shell as sh
        from tools.python_repl import _register_surviving_descendants
        tree = [
            _FakeProc(100, 1, "script"),                 # 脚本自身
            _FakeProc(101, 100, "child"),                # 子
            _FakeProc(102, 101, "grandchild"),           # 孙
            _FakeProc(103, 101, "zombie", status="zombie"),  # zombie 不登记
            _FakeProc(200, 1, "other"),                  # 无关
        ]
        monkeypatch.setattr(psutil, "process_iter", lambda *a, **k: iter(tree))
        n = _register_surviving_descendants(100, task_id=77, session_id=5)
        assert n == 2
        procs = sh.get_background_processes_for_task(77)
        assert set(procs.keys()) == {"101", "102"}
        assert procs["101"]["source"] == "execute_python"
        assert "child" in procs["101"]["command"]

    def test_descendants_orphan_when_no_task_id(self, monkeypatch):
        """无 task_id：走 orphan 池并带 session_id（等迟到绑定）。"""
        import psutil
        import tools.shell as sh
        from tools.python_repl import _register_surviving_descendants
        tree = [_FakeProc(300, 1, "script"), _FakeProc(301, 300, "child")]
        monkeypatch.setattr(psutil, "process_iter", lambda *a, **k: iter(tree))
        n = _register_surviving_descendants(300, task_id=0, session_id=9)
        assert n == 1
        orphans = sh.get_orphan_processes()
        assert len(orphans) == 1
        oinfo = list(orphans.values())[0]
        assert oinfo["pid"] == 301
        assert oinfo["session_id"] == 9

    def test_execute_reports_registered_survivors(self, monkeypatch):
        """正常退出路径：输出补一句登记提示。"""
        import tools.python_repl as pr
        monkeypatch.setattr(pr, "_register_surviving_descendants", lambda *a, **k: 2)
        out = pr.PythonREPLTool().execute(code="print('hello')")
        assert "Exit Code: 0" in out
        assert "已登记 2 个存活子进程" in out
        assert "进程管理" in out

    def test_timeout_path_registers_survivors(self, monkeypatch):
        """超时 taskkill 后仍枚举存活子孙并登记，输出如实提示。"""
        import subprocess as _sp
        import tools.python_repl as pr

        class _FakePopen:
            def __init__(self, *a, **k):
                self.pid = 424242
                self.stdout = None
                self.returncode = None

            def communicate(self, timeout=None):
                raise _sp.TimeoutExpired(cmd="python", timeout=timeout)

            def kill(self):
                pass

            def wait(self, timeout=None):
                return None

        monkeypatch.setattr(pr.subprocess, "Popen", _FakePopen)
        monkeypatch.setattr(pr, "_register_surviving_descendants", lambda *a, **k: 1)
        out = pr.PythonREPLTool().execute(code="import time; time.sleep(999)")
        assert "timed out after 60 seconds" in out
        assert "已登记 1 个存活子进程" in out


# ---------- D. discovered 扫描 + 按 pid kill 端点 ----------

class TestDiscoveredScan:
    def test_discover_filters_and_excludes(self, monkeypatch, tmp_path):
        """cwd/cmdline 命中 sandbox 才收；已追踪 pid、受保护 pid、无关进程都排除。"""
        import psutil
        import api.state as state
        from api.routes import routes_tasks as rt
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        monkeypatch.setattr(rt, "load_config",
                            lambda: {"sandbox_mode": True, "sandbox_dir": str(sandbox)})
        procs = [
            _FakeProc(501, 0, "hit_cwd", cwd=str(sandbox / "sub")),
            _FakeProc(502, 0, "hit_cmd", cwd=str(tmp_path),
                      cmdline=["python", str(sandbox / "job.py")]),
            _FakeProc(503, 0, "miss", cwd=str(tmp_path), cmdline=["python", "x.py"]),
            _FakeProc(504, 0, "tracked", cwd=str(sandbox)),      # 已在追踪表
            _FakeProc(505, 0, "protected", cwd=str(sandbox)),    # 服务自身/祖先
        ]
        monkeypatch.setattr(psutil, "process_iter", lambda *a, **k: iter(procs))
        # 新机制（35s 卡死修复）：保护集合= _server_pid + 其 parents，每次扫描算一次
        monkeypatch.setattr(state, "_server_pid", 505)
        found = rt._discover_sandbox_processes({504})
        pids = [p["pid"] for p in found]
        assert 501 in pids and 502 in pids
        assert 503 not in pids    # 与 sandbox 无关
        assert 504 not in pids    # 已追踪
        assert 505 not in pids    # 受保护
        row = next(p for p in found if p["pid"] == 501)
        assert set(row.keys()) >= {"pid", "name", "cmdline", "create_time", "uptime"}

    def test_discover_empty_when_sandbox_disabled(self, monkeypatch):
        from api.routes import routes_tasks as rt
        monkeypatch.setattr(rt, "load_config", lambda: {"sandbox_mode": False})
        assert rt._discover_sandbox_processes(set()) == []

    def test_list_processes_flattens_and_adds_discovered(self, monkeypatch):
        """/api/processes：主表按 {task:pid} 展平每进程一行，附 discovered 分区。
        （用真实存活 pid——死 pid 会被惰性回收，见 TestZombieReaping）"""
        import psutil
        import tools.shell as sh
        from api.routes import routes_tasks as rt
        live1, live2 = os.getpid(), os.getppid()
        # pid_alive_as 校验 create_time：真实存活 pid 需带真实 started_at，
        # 否则被判作 pid 复用而回收
        sh.register_background_process(
            55, _seed_info(live1, started_at=psutil.Process(live1).create_time()))
        sh.register_background_process(
            55, _seed_info(live2, started_at=psutil.Process(live2).create_time()))
        oid = sh.register_orphan_process(
            _seed_info(live1, started_at=psutil.Process(live1).create_time()),
            session_id=2)
        monkeypatch.setattr(rt, "_discover_sandbox_processes",
                            lambda exclude, **kw: [
                                {"pid": 987699, "name": "wild", "cmdline": "x",
                                 "create_time": 1.0, "uptime": 5.0}])
        resp = asyncio.run(rt.list_processes())
        procs = resp["processes"]
        assert set(procs.keys()) == {f"55:{live1}", f"55:{live2}", oid}
        row = procs[f"55:{live1}"]
        assert row["task_id"] == "55" and row["pid"] == live1
        assert row["alive"] is True and "uptime" in row
        assert resp["discovered"] == [
            {"pid": 987699, "name": "wild", "cmdline": "x",
             "create_time": 1.0, "uptime": 5.0}]


class TestKillWildEndpoint:
    def _patch_sandbox(self, monkeypatch, tmp_path):
        import api.state as state
        from api.routes import routes_tasks as rt
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        monkeypatch.setattr(rt, "load_config",
                            lambda: {"sandbox_mode": True, "sandbox_dir": str(sandbox)})
        monkeypatch.setattr(state, "check_protected_pid", lambda pid: False)
        return rt, sandbox

    def test_kill_untracked_sandbox_pid_succeeds(self, monkeypatch, tmp_path):
        """不在注册表的 discovered 野生进程：sandbox 命中 → kill_tree 终止。"""
        import psutil
        import core.process as cp
        rt, sandbox = self._patch_sandbox(monkeypatch, tmp_path)
        monkeypatch.setattr(psutil, "Process",
                            lambda pid: _FakeProc(pid, 0, "wild", cwd=str(sandbox)))
        killed = []
        monkeypatch.setattr(cp, "kill_tree", lambda pid: killed.append(pid))
        resp = asyncio.run(rt.kill_wild_process(601))
        assert resp == {"status": "success", "killed_pid": 601}
        assert killed == [601]

    def test_rejects_non_sandbox_pid(self, monkeypatch, tmp_path):
        """cwd/cmdline 与 sandbox 无关的进程：403 拒绝（防端点被杀任意进程）。"""
        import psutil
        import core.process as cp
        rt, sandbox = self._patch_sandbox(monkeypatch, tmp_path)
        monkeypatch.setattr(psutil, "Process",
                            lambda pid: _FakeProc(pid, 0, "sys", cwd=str(tmp_path),
                                                  cmdline=["svchost"]))
        killed = []
        monkeypatch.setattr(cp, "kill_tree", lambda pid: killed.append(pid))
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            asyncio.run(rt.kill_wild_process(603))
        assert ei.value.status_code == 403
        assert killed == []

    def test_rejects_protected_pid(self, monkeypatch, tmp_path):
        """Open-AGC 服务自身/祖先：403，根本不做 sandbox 匹配。"""
        import api.state as state
        from api.routes import routes_tasks as rt
        monkeypatch.setattr(rt, "load_config",
                            lambda: {"sandbox_mode": True, "sandbox_dir": str(tmp_path)})
        monkeypatch.setattr(state, "check_protected_pid", lambda pid: True)
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            asyncio.run(rt.kill_wild_process(604))
        assert ei.value.status_code == 403

    def test_rejects_nonexistent_pid(self, monkeypatch, tmp_path):
        import psutil
        rt, sandbox = self._patch_sandbox(monkeypatch, tmp_path)

        def _gone(pid):
            raise psutil.NoSuchProcess(pid)

        monkeypatch.setattr(psutil, "Process", _gone)
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            asyncio.run(rt.kill_wild_process(605))
        assert ei.value.status_code == 404

    def test_rejects_when_sandbox_disabled(self, monkeypatch, tmp_path):
        """未追踪 pid + sandbox 关闭：无法校验归属 → 403。"""
        import api.state as state
        from api.routes import routes_tasks as rt
        monkeypatch.setattr(rt, "load_config", lambda: {"sandbox_mode": False})
        monkeypatch.setattr(state, "check_protected_pid", lambda pid: False)
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            asyncio.run(rt.kill_wild_process(606))
        assert ei.value.status_code == 403


# ---------- 评审 I1：按 pid kill 端点的任务状态同步 ----------

class TestKillWildTaskSync:
    def test_kill_last_tracked_pid_flips_task(self, tmp_db, monkeypatch, tmp_path):
        """I1：被杀的是任务最后一个进程 → 复用 /api/tasks/{id}/kill 语义：
        守卫 UPDATE 置 interrupted/user、resume_count 复位、置 agent 中断
        标志、注入输出通知——backgrounded 任务不再卡到 6h 超时。"""
        import api.state as state
        import core.process as cp
        import tools.shell as sh
        from api.routes import routes_tasks as rt
        from api.task_core import save_task_context, get_task_context

        tid = _insert_task(tmp_db, status="backgrounded")
        save_task_context(tid, [{"role": "user", "content": "原始查询"}])
        out_file = tmp_path / "o.log"
        out_file.write_text("tail output\n", encoding="utf-8")
        sh.register_background_process(tid, _seed_info(987654, output_file=str(out_file)))
        monkeypatch.setattr(state, "check_protected_pid", lambda pid: False)
        killed = []
        monkeypatch.setattr(cp, "kill_tree", lambda pid: killed.append(pid))
        fake_bg_agent = types.SimpleNamespace(is_interrupted=False)
        state._background_agents[tid] = fake_bg_agent
        try:
            resp = asyncio.run(rt.kill_wild_process(987654))
            assert resp["status"] == "success"
            assert resp["killed_pid"] == 987654
            assert resp["task_id"] == str(tid)
            assert resp["task_interrupted"] is True
            assert killed == [987654]
            st = _task_row(tmp_db, tid)
            assert st["status"] == "interrupted"
            assert st["interruption_reason"] == "user"
            assert st["resume_count"] == 0
            assert fake_bg_agent.is_interrupted is True
            assert fake_bg_agent._completed_by_user is True
            ctx = get_task_context(tid)
            assert any("Process killed by user" in m.get("content", "") for m in ctx)
        finally:
            state._background_agents.pop(tid, None)

    def test_kill_non_last_pid_leaves_task_untouched(self, tmp_db, monkeypatch):
        """I1：任务还有其他存活 pid → 只清该 pid 条目，任务状态不动。"""
        import api.state as state
        import core.process as cp
        import tools.shell as sh
        from api.routes import routes_tasks as rt

        tid = _insert_task(tmp_db, status="backgrounded")
        sh.register_background_process(tid, _seed_info(987654))
        sh.register_background_process(tid, _seed_info(os.getpid()))  # 存活
        monkeypatch.setattr(state, "check_protected_pid", lambda pid: False)
        monkeypatch.setattr(cp, "kill_tree", lambda pid: None)
        resp = asyncio.run(rt.kill_wild_process(987654))
        assert resp["status"] == "success"
        assert resp["task_id"] == str(tid)
        assert resp["task_interrupted"] is False
        assert resp["remaining_pids"] == [os.getpid()]
        assert _task_row(tmp_db, tid)["status"] == "backgrounded"
        assert set(sh.get_background_processes_for_task(tid).keys()) == {str(os.getpid())}


# ---------- 评审 I2：已追踪 pid 免 sandbox 校验 ----------

class TestKillWildSandboxBypass:
    def test_tracked_pid_bypasses_sandbox_check(self, tmp_db, monkeypatch):
        """I2：主表 pid 免 sandbox 校验——sandbox_mode 关闭也能杀（否则进程页
        终止按钮全部 403）。杀的是最后一个进程 → 任务同步翻转。"""
        import api.state as state
        import core.process as cp
        import tools.shell as sh
        from api.routes import routes_tasks as rt

        monkeypatch.setattr(rt, "load_config", lambda: {"sandbox_mode": False})
        monkeypatch.setattr(state, "check_protected_pid", lambda pid: False)
        killed = []
        monkeypatch.setattr(cp, "kill_tree", lambda pid: killed.append(pid))
        tid = _insert_task(tmp_db, status="running")
        sh.register_background_process(tid, _seed_info(987654))
        resp = asyncio.run(rt.kill_wild_process(987654))
        assert resp["status"] == "success"
        assert killed == [987654]
        assert resp["task_interrupted"] is True
        assert _task_row(tmp_db, tid)["status"] == "interrupted"

    def test_orphan_pid_bypasses_sandbox_check(self, monkeypatch):
        """I2：orphan 池 pid 同样免 sandbox 校验；orphan 无任务，不做状态同步。"""
        import api.state as state
        import core.process as cp
        import tools.shell as sh
        from api.routes import routes_tasks as rt

        monkeypatch.setattr(rt, "load_config", lambda: {"sandbox_mode": False})
        monkeypatch.setattr(state, "check_protected_pid", lambda pid: False)
        killed = []
        monkeypatch.setattr(cp, "kill_tree", lambda pid: killed.append(pid))
        sh.register_orphan_process(_seed_info(987655), session_id=1)
        resp = asyncio.run(rt.kill_wild_process(987655))
        assert resp == {"status": "success", "killed_pid": 987655}
        assert killed == [987655]
        assert sh.get_orphan_processes() == {}


# ---------- 评审 I3：sandbox 路径边界匹配 ----------

class TestSandboxPathBoundary:
    def test_sibling_directory_not_matched(self, tmp_path):
        """workspace2 这类兄弟目录不误命中（旧实现是无边界的子串包含）。"""
        from api.routes import routes_tasks as rt
        sandbox = tmp_path / "workspace"
        sandbox.mkdir()
        sibling = tmp_path / "workspace2"
        sibling.mkdir()
        assert not rt._pid_matches_sandbox(
            _FakeProc(900, 0, "p", cwd=str(sibling)), str(sandbox))
        assert not rt._pid_matches_sandbox(
            _FakeProc(901, 0, "p", cwd=str(tmp_path),
                      cmdline=["python", str(sibling / "job.py")]), str(sandbox))

    def test_exact_and_descendant_matched(self, tmp_path):
        """相等或子孙路径（cwd / cmdline 路径 token）命中。"""
        from api.routes import routes_tasks as rt
        sandbox = tmp_path / "workspace"
        (sandbox / "sub").mkdir(parents=True)
        assert rt._pid_matches_sandbox(
            _FakeProc(902, 0, "p", cwd=str(sandbox)), str(sandbox))
        assert rt._pid_matches_sandbox(
            _FakeProc(903, 0, "p", cwd=str(sandbox / "sub")), str(sandbox))
        assert rt._pid_matches_sandbox(
            _FakeProc(904, 0, "p", cwd=str(tmp_path),
                      cmdline=["python", str(sandbox / "sub" / "job.py")]), str(sandbox))
        # --dir=<path> 形式的 token 也按路径边界判定
        assert rt._pid_matches_sandbox(
            _FakeProc(905, 0, "p", cwd=str(tmp_path),
                      cmdline=["tool", f"--dir={sandbox / 'sub'}"]), str(sandbox))

    def test_non_path_cmdline_tokens_ignored(self, tmp_path):
        """不含路径分隔符的普通参数（如裸词 workspace）不当路径解析。"""
        from api.routes import routes_tasks as rt
        sandbox = tmp_path / "workspace"
        sandbox.mkdir()
        assert not rt._pid_matches_sandbox(
            _FakeProc(906, 0, "p", cwd=str(tmp_path),
                      cmdline=["python", "-m", "workspace"]), str(sandbox))


# ---------- 评审 I4：kill 并发窗口 + 迟到登记兜底 ----------

class TestKillConcurrencyWindow:
    def test_concurrent_registration_in_kill_window_also_killed(self, monkeypatch):
        """I4：杀进程窗口内新登记的 pid 不被连带删除而不杀——下一轮抓到照杀。"""
        import tools.shell as sh
        sh.register_background_process(7, _seed_info(900001))
        killed = []

        def _kill_then_register(pid):
            killed.append(pid)
            if pid == 900001:
                # 模拟并发窗口：杀旧 pid 时新进程登记进同一任务
                sh.register_background_process(7, _seed_info(900002))

        monkeypatch.setattr(sh, "kill_tree", _kill_then_register)
        pids = sh.kill_background_process_for_task(7)
        assert sorted(pids) == [900001, 900002]
        assert sorted(killed) == [900001, 900002]
        assert sh.get_background_processes_for_task(7) == {}

    def test_late_registration_after_kill_reaped(self, monkeypatch):
        """I4b：清理后迟到的登记（典型：execute_python 60s 超时退出时又登记回
        已中断任务）由僵尸回收兜底——死 pid 条目被清出。"""
        import tools.shell as sh
        sh.register_background_process(7, _seed_info(900001))
        monkeypatch.setattr(sh, "kill_tree", lambda pid: None)
        sh.kill_background_process_for_task(7)
        # 迟到登记：pid 已死
        sh.register_background_process(7, _seed_info(987654))
        reaped = sh.reap_dead_background_processes()
        assert [e["pid"] for e in reaped] == [987654]
        assert sh.get_background_processes_for_task(7) == {}


# ---------- 僵尸进程条目：惰性回收 ----------

class TestZombieReaping:
    def test_get_task_process_real_alive_and_reaps_dead(self, tmp_db, tmp_path):
        """detail 端点：每个 pid 计算真实 alive（不再硬编码 True）；死 pid
        条目回收后以 alive=false/reaped=true 标志返回一次，输出文件还在
        → 保留路径；再读不再出现。"""
        import psutil
        import tools.shell as sh
        from api.routes import routes_tasks as rt
        tid = _insert_task(tmp_db, status="running")
        out_file = tmp_path / "x.log"
        out_file.write_text("log", encoding="utf-8")
        sh.register_background_process(tid, _seed_info(987654, output_file=str(out_file)))
        sh.register_background_process(
            tid, _seed_info(os.getpid(),
                            started_at=psutil.Process(os.getpid()).create_time()))
        resp = asyncio.run(rt.get_task_process(tid))
        rows = resp["processes"]
        live = [r for r in rows if r.get("alive")]
        dead = [r for r in rows if r.get("reaped")]
        assert len(live) == 1 and live[0]["pid"] == os.getpid()
        assert len(dead) == 1 and dead[0]["pid"] == 987654
        assert dead[0]["alive"] is False and dead[0]["reaped"] is True
        assert dead[0]["output_file"] == str(out_file)   # 文件还在→保留路径
        assert resp["process"]["alive"] is True          # 兼容字段指向存活行
        # 表已回收；再读不再出现死条目
        resp2 = asyncio.run(rt.get_task_process(tid))
        assert [r["pid"] for r in resp2["processes"]] == [os.getpid()]
        assert set(sh.get_background_processes_for_task(tid).keys()) == {str(os.getpid())}

    def test_reaped_row_marks_deleted_output_file(self, tmp_db):
        """输出文件已删 → output_file 置空 + output_file_deleted 标记。"""
        import tools.shell as sh
        from api.routes import routes_tasks as rt
        tid = _insert_task(tmp_db, status="running")
        sh.register_background_process(
            tid, _seed_info(987654, output_file="/nonexistent/dir/gone.log"))
        resp = asyncio.run(rt.get_task_process(tid))
        dead = [r for r in resp["processes"] if r.get("reaped")]
        assert len(dead) == 1
        assert dead[0]["output_file"] == ""
        assert dead[0]["output_file_deleted"] is True

    def test_list_processes_reaps_dead_once_with_flag(self, monkeypatch):
        """list 端点：死条目首次读取带 reaped 标志返回，之后不再出现。"""
        import psutil
        import tools.shell as sh
        from api.routes import routes_tasks as rt
        sh.register_background_process(
            55, _seed_info(os.getpid(),
                           started_at=psutil.Process(os.getpid()).create_time()))
        sh.register_background_process(55, _seed_info(987654))
        monkeypatch.setattr(rt, "_discover_sandbox_processes", lambda *a, **k: [])
        resp = asyncio.run(rt.list_processes())
        procs = resp["processes"]
        assert procs[f"55:{os.getpid()}"]["alive"] is True
        zombie = procs.get("55:987654")
        assert zombie is not None
        assert zombie["alive"] is False and zombie["reaped"] is True
        resp2 = asyncio.run(rt.list_processes())
        assert "55:987654" not in resp2["processes"]
        assert set(sh.get_background_processes_for_task(55).keys()) == {str(os.getpid())}

    def test_bgmonitor_reaps_zombie_entries_of_running_task(self, tmp_db, monkeypatch):
        """BgMonitor 每轮回收：running（非 backgrounded）任务名下的死 pid
        条目也被清——此前是监控盲区（僵尸条目永远显示"运行中"）。"""
        import api.background as bg
        import tools.shell as sh
        tid = _insert_task(tmp_db, status="running")
        sh.register_background_process(tid, _seed_info(987654))
        monkeypatch.setattr(bg, "pid_alive", lambda pid: False)
        spawned, rounds_done, release_loop = _install_monitor_harness(
            monkeypatch, bg, run_rounds=3)
        bg.start_background_monitor()
        try:
            assert rounds_done.wait(timeout=20), "monitor loop did not run 3 rounds"
        finally:
            release_loop.set()
        assert sh.get_background_processes_for_task(tid) == {}
        assert spawned == []          # 不触发任何恢复（任务本来 running）
        assert _task_row(tmp_db, tid)["status"] == "running"



# ---------- /api/processes 性能回归（全站卡死 35s 根因） ----------

class TestDiscoverPerformance:
    """discovered 扫描曾逐进程调 check_protected_pid（每个重建
    psutil.Process(server).parents()，~90ms × 394 进程 = 35s 同步阻塞
    事件循环）。修复：保护集合每次扫描算一次 + 结果 10s TTL 缓存 +
    端点整体移执行器线程。"""

    def _fake_procs(self, n):
        procs = []
        for i in range(n):
            p = types.SimpleNamespace(pid=10000 + i)
            p.cwd = lambda: "C:\\other"
            p.cmdline = lambda: ["python", "x.py"]
            p.name = lambda: "python.exe"
            p.create_time = lambda: 1700000000.0
            procs.append(p)
        return procs

    def test_protected_set_computed_once_per_scan(self, monkeypatch, tmp_path):
        """N 个进程扫描时 psutil.Process 实例化次数与 N 无关（≤2）。"""
        import api.routes.routes_tasks as rt
        import api.state as state
        monkeypatch.setattr(state, "_server_pid", 999999)  # 不存在也无妨
        calls = {"n": 0}

        class _FakeProc:
            def __init__(self, pid):
                calls["n"] += 1
                self.pid = pid

            def parents(self):
                return []

        import psutil as real_psutil
        monkeypatch.setattr(real_psutil, "Process", _FakeProc)
        monkeypatch.setattr(real_psutil, "process_iter",
                            lambda attrs: iter(self._fake_procs(400)))
        monkeypatch.setattr(rt, "_sandbox_dir_from_config", lambda: str(tmp_path))
        rt._discover_sandbox_processes(set())
        assert calls["n"] <= 2, f"psutil.Process 被实例化 {calls['n']} 次（每进程一次即回归）"

    def test_discover_cached_ttl_and_filter(self, monkeypatch, tmp_path):
        """TTL 内复用扫描结果；tracked pid 在请求时过滤。"""
        import api.routes.routes_tasks as rt
        scans = {"n": 0}
        fake_items = [{"pid": 111, "name": "a", "cmdline": "", "create_time": 1, "uptime": 0},
                      {"pid": 222, "name": "b", "cmdline": "", "create_time": 2, "uptime": 0}]

        def _fake_scan(exclude):
            scans["n"] += 1
            return list(fake_items)

        monkeypatch.setattr(rt, "_discover_sandbox_processes", _fake_scan)
        monkeypatch.setattr(rt, "_sandbox_dir_from_config", lambda: str(tmp_path))
        with rt._discovered_lock:
            rt._discovered_cache.update({"ts": 0.0, "sandbox": None, "items": []})
        first = rt._discover_cached(set())
        second = rt._discover_cached({111})
        assert scans["n"] == 1, "TTL 内不应重复全盘扫描"
        assert len(first) == 2
        assert [p["pid"] for p in second] == [222], "tracked pid 应在请求时被过滤"

    def test_list_processes_offloaded_to_executor(self, monkeypatch):
        """端点不在事件循环内做同步工作：经 run_in_executor 调度。"""
        import api.routes.routes_tasks as rt
        used = {"executor": False}
        loop = asyncio.new_event_loop()
        real_run = loop.run_in_executor

        def spy(executor, func, *args):
            used["executor"] = True
            return real_run(executor, func, *args)

        monkeypatch.setattr(loop, "run_in_executor", spy)
        try:
            result = loop.run_until_complete(rt.list_processes())
        finally:
            loop.close()
        assert used["executor"], "list_processes 未移出事件循环"
        assert "processes" in result and "discovered" in result
