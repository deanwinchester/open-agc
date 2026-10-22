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


class TestOutputsDiff:
    """execute_python/execute_shell 产物捕获：执行前后 outputs/ 目录 diff。"""

    def test_new_and_modified_files_captured(self, tmp_path):
        from agent.agent import _snapshot_outputs, _diff_outputs
        outputs = tmp_path / "outputs"
        (outputs / "proj").mkdir(parents=True)
        old = outputs / "proj" / "old.txt"
        old.write_text("v1", encoding="utf-8")
        before = _snapshot_outputs(str(outputs))

        # 新增二进制产物（docx 等只能由 execute_python 生成）
        (outputs / "proj" / "报告.docx").write_bytes(b"\x00\x01")
        # 修改既有文件（大小变化 → mtime/size 元组变化）
        old.write_text("v1-v1", encoding="utf-8")
        # tmp/ 中间产物不记
        (outputs / "proj" / "tmp").mkdir()
        (outputs / "proj" / "tmp" / "scratch.txt").write_text("x", encoding="utf-8")

        out = _diff_outputs(before, str(outputs))
        paths = {f["path"] for f in out}
        assert "outputs/proj/报告.docx" in paths
        assert "outputs/proj/old.txt" in paths
        assert not any("/tmp/" in p for p in paths)

    def test_no_change_empty_diff(self, tmp_path):
        from agent.agent import _snapshot_outputs, _diff_outputs
        outputs = tmp_path / "outputs"
        outputs.mkdir()
        (outputs / "a.txt").write_text("a", encoding="utf-8")
        before = _snapshot_outputs(str(outputs))
        assert _diff_outputs(before, str(outputs)) == []

    def test_missing_outputs_dir(self, tmp_path):
        from agent.agent import _snapshot_outputs, _diff_outputs
        missing = str(tmp_path / "outputs")
        before = _snapshot_outputs(missing)
        assert before == {}
        assert _diff_outputs(before, missing) == []


class TestSubAgentGeneratedFiles:
    """子代理 tool_done 事件携带 generated_files（与主 agent 同格式）。"""

    def test_write_file_tool_done_carries_generated_files(self):
        import json as _json
        from types import SimpleNamespace
        from agent.sub_agent import SubAgent

        def _tc(name, args, call_id):
            fn = SimpleNamespace(
                name=name, arguments=_json.dumps(args, ensure_ascii=False))
            return SimpleNamespace(id=call_id, type="function", function=fn)

        def _resp(text=None, tool_calls=None):
            msg = SimpleNamespace(
                role="assistant", content=text or "", tool_calls=tool_calls)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=msg)], usage=None), "mock"

        class _LLM:
            def __init__(self, script):
                self.script = list(script)

            def chat(self, messages=None, tools=None, interrupt_check=None):
                text, tcs = self.script.pop(0)
                return _resp(text, tcs)

        class _WriteTool:
            name = "write_file"
            description = "fake"

            def get_openai_schema(self):
                return {"type": "function", "function": {
                    "name": "write_file", "description": "fake",
                    "parameters": {"type": "object", "properties": {}}}}

            def execute(self, **kwargs):
                return "OK"

        events = []
        llm = _LLM([
            (None, [_tc("write_file", {"path": "outputs/报告.md"}, "c1")]),
            ("完成", None),
        ])
        sub = SubAgent(task="写文件", tools=["write_file"],
                       parent_tools={"write_file": _WriteTool()},
                       max_iterations=5, llm_client=llm,
                       progress_callback=events.append)
        result = sub.run()
        assert result["success"] is True
        done = [e for e in events if e.get("event") == "tool_done"
                and e.get("tool") == "write_file"]
        assert len(done) == 1
        assert _json.loads(done[0]["generated_files"]) == \
            [{"path": "outputs/报告.md"}]

    def test_non_write_tool_has_no_generated_files(self):
        import json as _json
        from types import SimpleNamespace
        from agent.sub_agent import SubAgent

        def _tc(name, args, call_id):
            fn = SimpleNamespace(
                name=name, arguments=_json.dumps(args, ensure_ascii=False))
            return SimpleNamespace(id=call_id, type="function", function=fn)

        def _resp(text=None, tool_calls=None):
            msg = SimpleNamespace(
                role="assistant", content=text or "", tool_calls=tool_calls)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=msg)], usage=None), "mock"

        class _LLM:
            def __init__(self, script):
                self.script = list(script)

            def chat(self, messages=None, tools=None, interrupt_check=None):
                text, tcs = self.script.pop(0)
                return _resp(text, tcs)

        class _ReadTool:
            name = "read_file"
            description = "fake"

            def get_openai_schema(self):
                return {"type": "function", "function": {
                    "name": "read_file", "description": "fake",
                    "parameters": {"type": "object", "properties": {}}}}

            def execute(self, **kwargs):
                return "OK"

        events = []
        llm = _LLM([
            (None, [_tc("read_file", {"path": "outputs/a.md"}, "c1")]),
            ("完成", None),
        ])
        sub = SubAgent(task="读文件", tools=["read_file"],
                       parent_tools={"read_file": _ReadTool()},
                       max_iterations=5, llm_client=llm,
                       progress_callback=events.append)
        result = sub.run()
        assert result["success"] is True
        done = [e for e in events if e.get("event") == "tool_done"]
        assert len(done) == 1
        assert "generated_files" not in done[0]
