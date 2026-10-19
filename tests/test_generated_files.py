# -*- coding: utf-8 -*-
"""产物记录机制测试：写文件工具 → generated_files 落库 → 任务产物端点。

背景：generated_files 列此前无任何写入点（机制建了没接线）；chips 曾靠
从回复文本正则提取路径（远端路径误附着）。现在 chips 只走记录机制。
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class TestGeneratedFilesForTool:
    def test_write_file_records_path(self):
        from agent.agent import _generated_files_for_tool
        out = _generated_files_for_tool("write_file", {"path": "outputs/报告.md"})
        assert out == [{"path": "outputs/报告.md"}]

    def test_tmp_path_excluded(self):
        from agent.agent import _generated_files_for_tool
        assert _generated_files_for_tool("write_file", {"path": "tmp/一次性.py"}) == []
        assert _generated_files_for_tool("edit_file", {"path": "workspace/tmp/x.py"}) == []

    def test_apply_patch_multiple(self):
        from agent.agent import _generated_files_for_tool
        patches = [{"path": "outputs/a.md", "edits": [{"old_string": "a", "new_string": "b"}]},
                   {"path": "tmp/b.py", "edits": [{"old_string": "a", "new_string": "b"}]},
                   {"path": "outputs/c.md", "edits": [{"old_string": "a", "new_string": "b"}]}]
        out = _generated_files_for_tool("apply_patch", {"patches": patches})
        assert out == [{"path": "outputs/a.md"}, {"path": "outputs/c.md"}]

    def test_non_write_tools_empty(self):
        from agent.agent import _generated_files_for_tool
        assert _generated_files_for_tool("execute_shell", {"command": "ls"}) == []
        assert _generated_files_for_tool("read_file", {"path": "outputs/a.md"}) == []


class TestGeneratedFilesEndpoint:
    def test_merge_and_serve(self, tmp_path, monkeypatch):
        import api.db as db_mod
        import api.routes.routes_tasks as rt
        db_file = tmp_path / "t.db"
        monkeypatch.setattr(db_mod, "DB_PATH", str(db_file))
        monkeypatch.setattr(rt, "DB_PATH", str(db_file))
        db_mod.init_db()

        from api.task_core import create_task, add_task_step, merge_generated_files
        f1 = tmp_path / "report.md"
        f1.write_text("hello", encoding="utf-8")
        f_missing = tmp_path / "missing.md"  # 不存在的文件：不附着

        tid = create_task("测试任务", "query", session_id=1)
        add_task_step(tid, 1, "write_file", tool_label="写文件")
        merge_generated_files(
            tid,
            json.dumps([{"path": str(f1)}, {"path": str(f_missing)}, {"path": str(f1)}]),
            step_number=1,
        )

        # 幂等：重复合并不产生重复行
        merge_generated_files(tid, json.dumps([{"path": str(f1)}]), step_number=1)

        app = FastAPI()
        app.include_router(rt.router)
        r = TestClient(app).get(f"/api/tasks/{tid}/generated_files")
        assert r.status_code == 200
        files = r.json()["files"]
        assert len(files) == 1
        assert files[0]["path"].endswith("report.md")
        assert files[0]["size"] == 5

    def test_empty_task(self, tmp_path, monkeypatch):
        import api.db as db_mod
        import api.routes.routes_tasks as rt
        db_file = tmp_path / "t.db"
        monkeypatch.setattr(db_mod, "DB_PATH", str(db_file))
        monkeypatch.setattr(rt, "DB_PATH", str(db_file))
        db_mod.init_db()
        from api.task_core import create_task
        tid = create_task("空任务", "query", session_id=1)
        app = FastAPI()
        app.include_router(rt.router)
        r = TestClient(app).get(f"/api/tasks/{tid}/generated_files")
        assert r.status_code == 200
        assert r.json()["files"] == []
