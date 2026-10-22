# -*- coding: utf-8 -*-
"""大任务检查点恢复机制测试。

覆盖：
- read_task_checkpoint：文件存在返回 dict；不存在返回 None；JSON 损坏/
  非 JSON 对象不抛异常返回 None
- format_checkpoint_notice：有效检查点时注入文本包含 done/total/last_cursor
  与断点续跑指引；无检查点/损坏时返回空串
- get_task_context 恢复注入（task_core 行为级）：构造检查点文件后，恢复快照
  上下文末尾追加检查点提示；快照里历次恢复留下的旧提示被去重（至多一条、
  永远最新读盘结果）；无检查点时上下文原样返回
"""
import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import api.task_core as tc  # noqa: E402

CKPT = {
    "task": "从 MongoDB 导出 180 万条数据",
    "total": 1800000,
    "done": 450000,
    "last_cursor": "507f1f77bcf86cd799439011",
    "phase": "exporting",
    "files_dir": "workspace/mongo_export",
    "updated_at": "2026-07-28T02:00:00",
}


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """把 api.db.DB_PATH 指到临时库。"""
    import api.db as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "test.db"))
    db_mod.init_db()
    return db_mod


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """把 api.task_core.load_config 指到临时 sandbox_dir（隔离真实 workspace）。"""
    sb = tmp_path / "workspace"
    (sb / ".checkpoints").mkdir(parents=True)
    monkeypatch.setattr(tc, "load_config", lambda: {"sandbox_dir": str(sb)})
    return sb


def _write_checkpoint(sandbox, task_id, data):
    path = sandbox / ".checkpoints" / f"task_{task_id}.json"
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _insert_task(db_mod, snapshot=None):
    conn = db_mod.db_connect()
    cur = conn.execute(
        "INSERT INTO tasks (title, user_query, status, session_id) "
        "VALUES (?, ?, 'interrupted', 1)",
        ("导出数据", "从 MongoDB 导出 180 万条数据"))
    tid = cur.lastrowid
    if snapshot is not None:
        conn.execute("UPDATE tasks SET context_snapshot=? WHERE id=?",
                     (json.dumps(snapshot, ensure_ascii=False), tid))
    conn.commit()
    conn.close()
    return tid


# ---------- read_task_checkpoint ----------

class TestReadTaskCheckpoint:
    def test_missing_file_returns_none(self, sandbox):
        assert tc.read_task_checkpoint(999) is None

    def test_valid_checkpoint_returns_dict(self, sandbox):
        _write_checkpoint(sandbox, 7, CKPT)
        data = tc.read_task_checkpoint(7)
        assert data["done"] == 450000
        assert data["total"] == 1800000
        assert data["last_cursor"] == "507f1f77bcf86cd799439011"

    def test_corrupt_json_returns_none_no_raise(self, sandbox):
        _write_checkpoint(sandbox, 7, "{not valid json")
        assert tc.read_task_checkpoint(7) is None

    def test_non_dict_json_returns_none(self, sandbox):
        _write_checkpoint(sandbox, 7, [1, 2, 3])
        assert tc.read_task_checkpoint(7) is None


# ---------- format_checkpoint_notice ----------

class TestFormatCheckpointNotice:
    def test_notice_contains_progress_and_cursor(self, sandbox):
        _write_checkpoint(sandbox, 7, CKPT)
        text = tc.format_checkpoint_notice(7)
        assert text.startswith(tc._CHECKPOINT_NOTICE_PREFIX)
        assert "450000" in text                      # done
        assert "1800000" in text                     # total
        assert "507f1f77bcf86cd799439011" in text    # last_cursor
        assert "last_cursor" in text
        assert "严禁" in text and "从头重跑" in text  # 断点续跑指引

    def test_missing_checkpoint_returns_empty(self, sandbox):
        assert tc.format_checkpoint_notice(999) == ""

    def test_corrupt_checkpoint_returns_empty_no_raise(self, sandbox):
        _write_checkpoint(sandbox, 7, "{{{")
        assert tc.format_checkpoint_notice(7) == ""


# ---------- get_task_context 恢复注入（task_core 行为级）----------

class TestGetTaskContextInjection:
    def _snapshot(self):
        return [
            {"role": "user", "content": "从 MongoDB 导出 180 万条数据"},
            {"role": "assistant", "content": "好的，开始分批导出。"},
        ]

    def test_resume_context_appends_checkpoint_notice(self, tmp_db, sandbox):
        tid = _insert_task(tmp_db, snapshot=self._snapshot())
        _write_checkpoint(sandbox, tid, CKPT)
        ctx = tc.get_task_context(tid)
        assert len(ctx) == 3
        last = ctx[-1]
        assert last["role"] == "user"
        assert last["content"].startswith(tc._CHECKPOINT_NOTICE_PREFIX)
        assert "507f1f77bcf86cd799439011" in last["content"]
        assert "450000" in last["content"] and "1800000" in last["content"]

    def test_no_checkpoint_leaves_context_unchanged(self, tmp_db, sandbox):
        tid = _insert_task(tmp_db, snapshot=self._snapshot())
        ctx = tc.get_task_context(tid)
        assert ctx == self._snapshot()

    def test_stale_notice_deduplicated(self, tmp_db, sandbox):
        """快照里历次恢复留下的旧检查点提示被剔除，只保留最新读盘结果。"""
        old_notice = (tc._CHECKPOINT_NOTICE_PREFIX +
                      "：旧提示，done=100/total=1800000，last_cursor=OLD_CURSOR")
        snap = self._snapshot() + [{"role": "user", "content": old_notice}]
        tid = _insert_task(tmp_db, snapshot=snap)
        _write_checkpoint(sandbox, tid, CKPT)
        ctx = tc.get_task_context(tid)
        notices = [m for m in ctx
                   if m.get("role") == "user"
                   and isinstance(m.get("content"), str)
                   and m["content"].startswith(tc._CHECKPOINT_NOTICE_PREFIX)]
        assert len(notices) == 1
        assert "OLD_CURSOR" not in notices[0]["content"]
        assert "507f1f77bcf86cd799439011" in notices[0]["content"]

    def test_reconstruction_path_also_injects(self, tmp_db, sandbox):
        """无快照时走 task_steps 重建路径，检查点提示同样追加在末尾。"""
        tid = _insert_task(tmp_db)  # 无 context_snapshot
        _write_checkpoint(sandbox, tid, CKPT)
        ctx = tc.get_task_context(tid)
        assert ctx[-1]["role"] == "user"
        assert "507f1f77bcf86cd799439011" in ctx[-1]["content"]

    def test_missing_checkpoint_file_no_raise(self, tmp_db, sandbox):
        """任务无任何检查点文件时注入静默跳过，不影响原上下文。"""
        tid = _insert_task(tmp_db, snapshot=self._snapshot())
        ctx = tc.get_task_context(tid)
        assert all(not m.get("content", "").startswith(tc._CHECKPOINT_NOTICE_PREFIX)
                   if isinstance(m.get("content"), str) else True
                   for m in ctx)


# ---------- 毒化 tool_calls 清洗（生产实证 #652 恢复死循环）----------

# 200 字符硬切从 \uXXXX 转义中间断开的残缺 arguments（生产实证 #652 的
# 分身步骤 args_preview 形态）：json.loads 必抛 Invalid \uXXXX escape
_TRUNCATED_ARGS = '{"path": "outputs/报告.md", "content": "标题\\u4e2'


class TestSanitizeToolCallArgs:
    def test_fallback_reconstruction_replaces_truncated_args(self, tmp_db, sandbox):
        """兜底重建遇截断 args_preview → arguments="{}" 且 tool 消息带说明。"""
        tid = _insert_task(tmp_db)  # 无 context_snapshot → 走 task_steps 重建
        tc.add_task_step(tid, 1, "write_file",
                         args_preview=_TRUNCATED_ARGS,
                         result_preview="OK", tool_call_id="c1")
        ctx = tc.get_task_context(tid)
        asst = next(m for m in ctx if m.get("role") == "assistant")
        args = asst["tool_calls"][0]["function"]["arguments"]
        assert json.loads(args) == {}
        tool_msg = next(m for m in ctx if m.get("role") == "tool")
        assert tool_msg["content"].startswith(tc._TOOL_ARGS_TRUNCATED_NOTE)

    def test_poisoned_snapshot_sanitized_on_load(self, tmp_db, sandbox):
        """含非法 arguments 的存量毒化快照经 get_task_context 加载即清洗。"""
        bad = [
            {"role": "user", "content": "写文档"},
            {"role": "assistant", "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "write_file",
                             "arguments": _TRUNCATED_ARGS}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "write_file",
             "content": "已写入"},
        ]
        tid = _insert_task(tmp_db, snapshot=bad)
        ctx = tc.get_task_context(tid)
        args = ctx[1]["tool_calls"][0]["function"]["arguments"]
        assert json.loads(args) == {}
        assert ctx[2]["content"].startswith(tc._TOOL_ARGS_TRUNCATED_NOTE)
        # 说明幂等：再次清洗不重复叠加
        again = tc._sanitize_tool_call_args(ctx)
        assert again[2]["content"].count(tc._TOOL_ARGS_TRUNCATED_NOTE) == 1

    def test_valid_arguments_untouched(self, tmp_db, sandbox):
        """合法 arguments 原样保留，tool 消息不加说明。"""
        good = [
            {"role": "user", "content": "写文档"},
            {"role": "assistant", "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "write_file",
                             "arguments": '{"path": "a.md", "content": "x"}'}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "write_file",
             "content": "已写入"},
        ]
        tid = _insert_task(tmp_db, snapshot=good)
        ctx = tc.get_task_context(tid)
        assert ctx[1]["tool_calls"][0]["function"]["arguments"] == \
            '{"path": "a.md", "content": "x"}'
        assert not ctx[2]["content"].startswith(tc._TOOL_ARGS_TRUNCATED_NOTE)


# ---------- 分身步骤补记 full_args（tool_start 事件携带 tool_args）----------

class TestSubAgentFullArgs:
    """分身 tool_start 事件携带完整 tool_args（ws/background 落 full_args
    的数据源）——替身模式参照 test_generated_files.py。"""

    def test_tool_start_event_carries_full_args(self):
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

        # 参数超过 200 字符：args_preview 装不下，full_args 必须走 tool_args
        long_args = {"path": "outputs/数据.csv", "mode": "长" * 300}
        events = []
        llm = _LLM([
            (None, [_tc("read_file", long_args, "c1")]),
            ("完成", None),
        ])
        sub = SubAgent(task="读文件", tools=["read_file"],
                       parent_tools={"read_file": _ReadTool()},
                       max_iterations=5, llm_client=llm,
                       progress_callback=events.append)
        result = sub.run()
        assert result["success"] is True
        starts = [e for e in events if e.get("event") == "tool_start"]
        assert len(starts) == 1
        assert _json.loads(starts[0]["tool_args"]) == long_args
        assert len(starts[0]["args_preview"]) <= 200
