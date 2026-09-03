# -*- coding: utf-8 -*-
"""pid_alive_as 与删除即终止 / orphan 收割宽限的回归测试。

- pid_alive_as：pid 复用防护——pid 存在但 create_time 与登记值不符判死；
- delete_task：删除运行中任务必须执行完整终止序列（置中断 + kill 后台
  进程 + tombstone），此后 handle_task_completion/claim_task_for_resume
  拒绝再碰该任务；
- reap_dead_background_processes：orphan 死条目保留 10 分钟宽限，
  等 BgMonitor adopt 观察死亡来唤醒任务（此前即清 → 永不唤醒）。
"""
import os
import time

import pytest

from core.process import pid_alive, pid_alive_as


class TestPidAliveAs:
    def test_current_process_alive_with_correct_start(self):
        import psutil
        pid = os.getpid()
        started = psutil.Process(pid).create_time()
        assert pid_alive_as(pid, started) is True

    def test_current_process_dead_with_stale_start(self):
        """同一 pid 但登记时间对不上（模拟 pid 复用）→ 判死。"""
        pid = os.getpid()
        assert pid_alive_as(pid, time.time() - 10 ** 6) is False

    def test_dead_pid_always_dead(self):
        # 找一个不存在的 pid
        pid = 2 ** 22
        while pid_alive(pid):
            pid += 1
        assert pid_alive_as(pid, time.time()) is False

    def test_no_started_at_degrades_to_pid_alive(self):
        assert pid_alive_as(os.getpid(), None) is True


class TestDeleteTermination:
    @pytest.fixture(autouse=True)
    def _clean(self):
        from api import state
        state._deleted_task_ids.clear()
        yield
        state._deleted_task_ids.clear()

    def test_tombstone_marks_and_blocks_completion(self):
        from api.state import mark_task_deleted, is_task_deleted
        from api.task_core import handle_task_completion, claim_task_for_resume

        mark_task_deleted(424242)
        assert is_task_deleted(424242)
        assert not is_task_deleted(1)
        # 完成路径不再写库
        assert handle_task_completion(424242, "anything", []) == 'deleted'
        # 恢复认领被拒绝
        assert claim_task_for_resume(424242, ('backgrounded',)) is False

    def test_tombstone_tolerates_bad_ids(self):
        from api.state import mark_task_deleted, is_task_deleted
        mark_task_deleted(None)
        mark_task_deleted("abc")
        assert not is_task_deleted(None)
        assert not is_task_deleted("abc")


class TestOrphanReapGrace:
    @pytest.fixture(autouse=True)
    def _clean_orphans(self):
        from tools import shell
        with shell._orphan_process_lock:
            shell._orphan_process_info.clear()
        yield
        with shell._orphan_process_lock:
            shell._orphan_process_info.clear()

    def test_recent_dead_orphan_survives_reap(self):
        """近期登记的 orphan 死条目不被收割（留给 adopt 观察死亡）。"""
        from tools import shell
        shell._orphan_process_info["o1"] = {
            "pid": 2 ** 22,  # 不存在 → 死
            "output_file": "",
            "command": "x",
            "started_at": time.time(),  # 刚登记
        }
        reaped = shell.reap_dead_background_processes()
        assert all(e.get("orphan_id") != "o1" for e in reaped)
        assert "o1" in shell._orphan_process_info

    def test_old_dead_orphan_reaped(self):
        """超过宽限期的 orphan 死条目正常收割。"""
        from tools import shell
        shell._orphan_process_info["o2"] = {
            "pid": 2 ** 22,
            "output_file": "",
            "command": "x",
            "started_at": time.time() - 3600,  # 1 小时前
        }
        reaped = shell.reap_dead_background_processes()
        assert any(e.get("orphan_id") == "o2" for e in reaped)
        assert "o2" not in shell._orphan_process_info
