# -*- coding: utf-8 -*-
"""后台任务唤醒回归：backgrounded 任务的死进程条目被 /api/processes 的
惰性回收先清掉，BgMonitor 失去「进程结束→恢复」信号，任务永远唤不醒
（生产实证：任务 #350 进程已结束、无 wake_at、resume_count=0 空等）。
修复：两个读取路径的 reap 调用都排除 backgrounded 任务。"""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class TestReapExcludesBackgrounded:
    def test_list_payload_excludes_backgrounded(self):
        src = open(os.path.join(PROJECT_ROOT, "api", "routes",
                                "routes_tasks.py"), encoding="utf-8").read()
        assert "_backgrounded_task_ids" in src
        assert src.count("exclude_task_ids=_backgrounded_task_ids()") >= 2, \
            "list_processes 与 get_task_process 两处 reap 都必须排除 backgrounded 任务"

    def test_reap_respects_exclusion(self, tmp_path, monkeypatch):
        """死 pid 条目在排除名单中时不被回收。"""
        import tools.shell as sh
        monkeypatch.setattr(sh, "_BG_STORE_PATH",
                            str(tmp_path / "bg.json"))
        with sh._background_process_lock:
            sh._background_process_info.clear()
            sh._background_process_info["350"] = {
                "99999999": {"pid": 99999999, "command": "x",
                             "started_at": 0, "alive": True}}
        reaped = sh.reap_dead_background_processes(exclude_task_ids={"350"})
        assert reaped == []
        assert "350" in sh.get_background_processes()
        # 不排除时确实会被回收（对照）
        reaped2 = sh.reap_dead_background_processes()
        assert len(reaped2) == 1
        with sh._background_process_lock:
            sh._background_process_info.clear()
