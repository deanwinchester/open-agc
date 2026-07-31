# -*- coding: utf-8 -*-
"""交付物登记制（语义命名 + 共享删除策略）测试。

覆盖：
- 建表/索引随 init_db 自动创建；启动回填（outputs/task_*/、检查点 files_dir、
  task_steps.generated_files 里 outputs/ 目录）幂等
- 登记写入点：handle_task_completion 完成钩子登记 outputs/ 目录并幂等关联
  多任务；created_by_task 只首次写入
- GET /api/tasks/{id}/artifacts：dirs 带 shared_with/missing，未登记来源
  （outputs/task_<id>/、检查点 files_dir）按旧逻辑兜底并入 files
- DELETE /api/tasks/{id}：artifact_dirs 清单按勾选删除（共享目录被显式勾选
  也照删）、非法 JSON 400、../ 与绝对路径逃逸逐项拒绝；delete_artifacts=true
  旧语义只删独占目录、共享跳过并列 skipped_shared；任务删除后关联清理、
  deliverables 行保留作历史
- 沙箱 entries：登记目录从登记表取语义名与多任务关联；未登记老目录维持
  task_<id>→deliverable / 其他→dir 兼容
- agent 系统提示「沙箱分区」语义命名约定存在（源码断言）

风格同 tests/test_sandbox_janitor.py：tmp_path 假 sandbox + tmp DB，
asyncio.run 直调端点函数，不碰真实 workspace/ 与 data/。
"""
import asyncio
import json
import os
import sqlite3
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import api.db as db_mod  # noqa: E402
import api.task_core as tc  # noqa: E402
import api.deliverables_registry as dr  # noqa: E402
import api.routes.routes_sandbox as rs  # noqa: E402
import api.routes.routes_tasks as rt  # noqa: E402
from fastapi import HTTPException  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """tmp DB + 假 sandbox：registry/sandbox/task_core/tasks 路由的 DB 与
    sandbox_dir 口径全部指向临时目录；摘掉 goals 联动写与目标完成度 LLM 判定。"""
    sb = tmp_path / "workspace"
    sb.mkdir()
    db_file = str(tmp_path / "test.db")
    monkeypatch.setattr(db_mod, "DB_PATH", db_file)
    db_mod.init_db()
    monkeypatch.setattr(rt, "DB_PATH", db_file)
    cfg = lambda: {"sandbox_dir": str(sb)}  # noqa: E731
    monkeypatch.setattr(dr, "load_config", cfg)
    monkeypatch.setattr(rs, "load_config", cfg)
    monkeypatch.setattr(tc, "load_config", cfg)
    monkeypatch.setattr("tools.task_plan.update_goals", lambda fn: None)
    monkeypatch.setattr(tc, "_check_goal_completeness", lambda task_id: 0)
    with rs._stats_lock:
        rs._reset_stats_locked()
        rs._stats_active = 0
        rs._stats_root = None
    yield sb
    with rs._stats_lock:
        rs._reset_stats_locked()
        rs._stats_active = 0
        rs._stats_root = None


def _insert_task(title="t", status="completed"):
    conn = sqlite3.connect(db_mod.DB_PATH)
    cur = conn.execute(
        "INSERT INTO tasks (title, user_query, status) VALUES (?, 'q', ?)",
        (title, status))
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def _insert_step(task_id, generated_files):
    conn = sqlite3.connect(db_mod.DB_PATH)
    conn.execute(
        "INSERT INTO task_steps (task_id, step_number, tool_name, generated_files) "
        "VALUES (?, 1, 'write_file', ?)",
        (task_id, json.dumps(generated_files, ensure_ascii=False)))
    conn.commit()
    conn.close()


def _write_checkpoint(sb, task_id, data):
    ckpt_dir = sb / ".checkpoints"
    ckpt_dir.mkdir(exist_ok=True)
    (ckpt_dir / f"task_{task_id}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _db_rows(sql, params=()):
    conn = sqlite3.connect(db_mod.DB_PATH)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


# ---------- 建表与索引 ----------

class TestSchema:
    def test_tables_and_indexes_created(self, env):
        names = {r[0] for r in _db_rows(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
        assert "deliverables" in names
        assert "task_deliverables" in names
        assert "idx_task_deliverables_task" in names
        assert "idx_task_deliverables_deliverable" in names
        cols = {r[1] for r in _db_rows("PRAGMA table_info(deliverables)")}
        assert {"id", "dir_path", "name", "created_by_task",
                "created_at", "updated_at"} <= cols


# ---------- 登记写入 ----------

class TestRegister:
    def test_register_idempotent_and_created_by_first(self, env):
        did1 = dr.register_deliverable("outputs/唐嫣照片", task_id=1)
        did2 = dr.register_deliverable("outputs/唐嫣照片", task_id=2)
        assert did1 == did2  # 同目录一行
        rows = _db_rows("SELECT name, created_by_task FROM deliverables")
        assert len(rows) == 1
        assert rows[0][0] == "唐嫣照片"      # 语义名取自目录名
        assert rows[0][1] == 1             # created_by_task 只首次写入
        links = _db_rows("SELECT task_id FROM task_deliverables ORDER BY task_id")
        assert [r[0] for r in links] == [1, 2]  # 多任务关联（多对多）
        # 关联幂等：重复登记不产生重复关联行
        dr.register_deliverable("outputs/唐嫣照片", task_id=2)
        links = _db_rows("SELECT COUNT(*) FROM task_deliverables")
        assert links[0][0] == 2

    def test_updated_at_refreshed_on_touch_only(self, env):
        dr.register_deliverable("outputs/报告", task_id=1, touch=False)
        ts1 = _db_rows("SELECT updated_at FROM deliverables")[0][0]
        dr.register_deliverable("outputs/报告", task_id=1, touch=False)
        ts2 = _db_rows("SELECT updated_at FROM deliverables")[0][0]
        assert ts1 == ts2  # 回填路径不刷新 updated_at

    def test_completion_hook_registers_outputs_dirs(self, env):
        """完成钩子：检查点 files_dir 与 generated_files 里 outputs/ 目录登记
        并关联本任务；outputs 之外的 generated_files 不登记。"""
        tid = _insert_task()
        d = env / "outputs" / "唐嫣照片"
        d.mkdir(parents=True)
        (d / "x.jpg").write_text("x", encoding="utf-8")
        _write_checkpoint(env, tid, {"files_dir": "outputs/唐嫣照片"})
        _insert_step(tid, [
            {"path": str(d / "x.jpg"), "type": "output"},
            {"path": str(env / "tmp" / "scratch.py"), "type": "temp"},
        ])
        result = tc.handle_task_completion(tid, "完成了唐嫣照片整理", [])
        assert result == "completed"
        dirs = dr.get_task_dirs(tid)
        assert [x["dir_path"] for x in dirs] == ["outputs/唐嫣照片"]
        assert dirs[0]["task_ids"] == [tid]
        assert _db_rows("SELECT COUNT(*) FROM deliverables")[0][0] == 1  # 两来源去重

    def test_register_conflict_path_keeps_association(self, env):
        """I4：撞 UNIQUE（行已被并发方先建）→ INSERT OR IGNORE 后回读 id 续走
        关联，不整体返回 None 丢关联。"""
        conn = sqlite3.connect(db_mod.DB_PATH)
        cur = conn.execute(
            "INSERT INTO deliverables (dir_path, name) VALUES ('outputs/x', 'x')")
        pre_id = cur.lastrowid
        conn.commit()
        conn.close()
        did = dr.register_deliverable("outputs/x", task_id=3)
        assert did == pre_id
        assert _db_rows("SELECT task_id FROM task_deliverables") == [(3,)]
        assert _db_rows("SELECT COUNT(*) FROM deliverables")[0][0] == 1

    def test_concurrent_register_keeps_links(self, env):
        """I4：多线程并发首登同一目录——单行、全部关联都在（busy_timeout 内
        SQLite 串行化写入，输家 INSERT OR IGNORE 后回读同一 id）。"""
        import threading
        tids = list(range(100, 108))
        threads = [threading.Thread(
            target=dr.register_deliverable,
            args=("outputs/并发目录", t)) for t in tids]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert _db_rows("SELECT COUNT(*) FROM deliverables")[0][0] == 1
        got = {r[0] for r in _db_rows("SELECT task_id FROM task_deliverables")}
        assert got == set(tids)

    def test_outputs_dir_of_rejects_dotdot(self, env):
        """Minor：../ 逃逸段显式拒绝（旧 lstrip("./") 会把 ../outputs/x 误吞成
        沙箱内 outputs/x 登记）。"""
        root = str(env)
        assert dr.outputs_dir_of("../outputs/evil", root) is None
        assert dr.outputs_dir_of("outputs/../evil", root) is None
        assert dr.outputs_dir_of("./outputs/ok/f.txt", root) == "outputs/ok"

    def test_dir_path_normcase_unified(self, env):
        """Minor：dir_path 统一 canon_dir_path 口径（POSIX 分隔符 + 平台大小写
        折叠——os.path.normcase 在 Windows 会把 / 翻成 \\，不能单独用），
        登记/查询同口径命中。"""
        dr.register_deliverable("outputs/MixedCase", task_id=1)
        stored = _db_rows("SELECT dir_path FROM deliverables")[0][0]
        assert stored == dr.canon_dir_path("outputs/MixedCase")
        assert "/" in stored
        m = dr.get_dirs_map(["outputs/MixedCase"])
        assert dr.canon_dir_path("outputs/MixedCase") in m


# ---------- 启动回填 ----------

class TestBackfill:
    def test_backfill_sources_and_idempotent(self, env):
        t3, t7, t9 = _insert_task(), _insert_task(), _insert_task()
        (env / "outputs" / f"task_{t3}").mkdir(parents=True)
        (env / "outputs" / "task_999").mkdir(parents=True)  # 死任务残留目录
        (env / "exports").mkdir()
        _write_checkpoint(env, t7, {"files_dir": "exports"})
        _insert_step(t9, [{"path": str(env / "outputs" / "报告" / "a.md"),
                           "type": "output"}])

        dr.backfill_deliverables(root=str(env))
        rows = _db_rows(
            "SELECT d.dir_path, td.task_id FROM deliverables d "
            "JOIN task_deliverables td ON td.deliverable_id=d.id ORDER BY d.id")
        mapping = {}
        for dir_path, tid in rows:
            mapping.setdefault(dir_path, []).append(tid)
        assert mapping == {f"outputs/task_{t3}": [t3], "exports": [t7],
                           "outputs/报告": [t9]}
        # I3：死任务（999 无 tasks 行）的残留目录不登记

        # 幂等：再跑一次行数不变
        dr.backfill_deliverables(root=str(env))
        assert _db_rows("SELECT COUNT(*) FROM deliverables")[0][0] == 3
        assert _db_rows("SELECT COUNT(*) FROM task_deliverables")[0][0] == 3

    def test_backfill_dedups_generated_files(self, env):
        """Minor：同任务多步骤指向同一 outputs 目录，回填去重后只登记一次。"""
        tid = _insert_task()
        f = str(env / "outputs" / "报告" / "a.md")
        _insert_step(tid, [{"path": f, "type": "output"}])
        _insert_step(tid, [{"path": f, "type": "output"}])
        dr.backfill_deliverables(root=str(env))
        assert _db_rows("SELECT COUNT(*) FROM deliverables")[0][0] == 1
        assert _db_rows("SELECT COUNT(*) FROM task_deliverables")[0][0] == 1


# ---------- GET /api/tasks/{id}/artifacts ----------

class TestArtifactsEndpoint:
    def test_dirs_shared_with_and_missing(self, env):
        t1, t2 = _insert_task(), _insert_task()
        shared = env / "outputs" / "共享目录"
        shared.mkdir(parents=True)
        (shared / "s.txt").write_text("x", encoding="utf-8")
        dr.register_deliverable("outputs/共享目录", task_id=t1)
        dr.register_deliverable("outputs/共享目录", task_id=t2)
        dr.register_deliverable("outputs/已删目录", task_id=t1)  # 盘上不存在

        resp = asyncio.run(rs.get_task_artifacts(t1))
        by_dir = {d["dir"]: d for d in resp["dirs"]}
        assert by_dir["outputs/共享目录"]["shared_with"] == [t2]
        assert by_dir["outputs/共享目录"]["missing"] is False
        assert [f["name"] for f in by_dir["outputs/共享目录"]["files"]] == ["s.txt"]
        assert by_dir["outputs/已删目录"]["missing"] is True
        assert by_dir["outputs/已删目录"]["files"] == []
        # 合并 files 旧契约保留
        assert [f["name"] for f in resp["files"]] == ["s.txt"]
        assert resp["files"][0]["source"] == "outputs"

    def test_unregistered_sources_fallback(self, env):
        """未登记的 outputs/task_<id>/ 与检查点 files_dir 仍按旧逻辑并入 files，
        dirs 为空（删除确认框退化为旧 delete_artifacts 勾选）。"""
        tid = 5
        (env / "outputs" / f"task_{tid}").mkdir(parents=True)
        (env / "outputs" / f"task_{tid}" / "report.md").write_bytes(b"22")
        resp = asyncio.run(rs.get_task_artifacts(tid))
        assert resp["dirs"] == []
        assert [f["name"] for f in resp["files"]] == ["report.md"]

    def test_shared_with_only_living_tasks(self, env):
        """I3：shared_with 只列存活任务——已删任务的残留关联不构成共享，
        UI 不会显示"还被任务 #999（已删）使用"。"""
        t1 = _insert_task()
        (env / "outputs" / "目录").mkdir(parents=True)
        dr.register_deliverable("outputs/目录", task_id=t1)
        dr.register_deliverable("outputs/目录", task_id=999)  # 死任务残留关联
        resp = asyncio.run(rs.get_task_artifacts(t1))
        assert resp["dirs"][0]["shared_with"] == []


# ---------- DELETE /api/tasks/{id} ----------

class TestDeleteTask:
    def test_artifact_dirs_selected_only(self, env):
        """按清单删除：只有勾选的目录被删；未勾选的共享目录保留。"""
        t1, t2 = _insert_task(), _insert_task()
        own = env / "outputs" / "独占目录"
        own.mkdir(parents=True)
        (own / "a.txt").write_text("x", encoding="utf-8")
        shared = env / "outputs" / "共享目录"
        shared.mkdir(parents=True)
        dr.register_deliverable("outputs/独占目录", task_id=t1)
        dr.register_deliverable("outputs/共享目录", task_id=t1)
        dr.register_deliverable("outputs/共享目录", task_id=t2)

        resp = asyncio.run(rt.delete_task(
            t1, artifact_dirs=json.dumps(["outputs/独占目录"])))
        assert resp["artifacts_removed"] == ["outputs/独占目录"]
        assert resp["artifacts_errors"] == []
        assert resp["skipped_shared"] == []
        assert not own.exists() and shared.exists()
        assert dr.get_task_dirs(t1) == []           # 关联已清
        assert dr.get_task_dirs(t2) != []           # 其他任务关联不受影响

    def test_artifact_dirs_shared_explicitly_selected(self, env):
        """共享目录被用户显式勾选 → 照删（之后对其他任务标 missing）。"""
        t1, t2 = _insert_task(), _insert_task()
        shared = env / "outputs" / "共享目录"
        shared.mkdir(parents=True)
        dr.register_deliverable("outputs/共享目录", task_id=t1)
        dr.register_deliverable("outputs/共享目录", task_id=t2)

        resp = asyncio.run(rt.delete_task(
            t1, artifact_dirs=json.dumps(["outputs/共享目录"])))
        assert resp["artifacts_removed"] == ["outputs/共享目录"]
        assert not shared.exists()
        # 登记保留作历史：t2 查询时该目录 missing
        resp2 = asyncio.run(rs.get_task_artifacts(t2))
        assert resp2["dirs"][0]["missing"] is True

    def test_artifact_dirs_invalid_json_400(self, env):
        tid = _insert_task()
        with pytest.raises(HTTPException) as ei:
            asyncio.run(rt.delete_task(tid, artifact_dirs="{not json"))
        assert ei.value.status_code == 400

    def test_artifact_dirs_escape_rejected_400(self, env, tmp_path):
        """I1/I2：../ 与绝对路径逃逸——整体 400（在任何删除动作之前），
        任务行与沙箱外目标都完好。"""
        tid = _insert_task()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("x", encoding="utf-8")
        for bad in ("../outside", str(outside)):
            with pytest.raises(HTTPException) as ei:
                asyncio.run(rt.delete_task(tid, artifact_dirs=json.dumps([bad])))
            assert ei.value.status_code == 400
        assert (outside / "keep.txt").exists()
        assert _db_rows("SELECT COUNT(*) FROM tasks WHERE id=?", (tid,))[0][0] == 1

    def test_artifact_dirs_not_in_task_set_400(self, env):
        """I1：清单项必须属于本任务交付物目录集合——未登记目录与任意嵌套路径
        （.checkpoints/task_5.json，绕开"仅顶层"限制的典型）整体 400。"""
        tid = _insert_task()
        (env / "outputs" / "别的目录").mkdir(parents=True)
        _write_checkpoint(env, 5, {"files_dir": "exports"})  # 别的任务的检查点
        for bad in ("outputs/别的目录", ".checkpoints/task_5.json"):
            with pytest.raises(HTTPException) as ei:
                asyncio.run(rt.delete_task(tid, artifact_dirs=json.dumps([bad])))
            assert ei.value.status_code == 400
        assert _db_rows("SELECT COUNT(*) FROM tasks WHERE id=?", (tid,))[0][0] == 1
        assert (env / "outputs" / "别的目录").exists()
        assert (env / ".checkpoints" / "task_5.json").exists()

    def test_artifact_dirs_drive_relative_400(self, env):
        """I2：Windows 驱动器相对路径 C:foo 能穿过 os.path.isabs（ntpath 实测），
        join 后逃逸 root——显式拒绝（同类输入校验全走 splitdrive 判据）。"""
        tid = _insert_task()
        with pytest.raises(HTTPException) as ei:
            asyncio.run(rt.delete_task(tid, artifact_dirs=json.dumps(["C:foo"])))
        assert ei.value.status_code == 400
        assert _db_rows("SELECT COUNT(*) FROM tasks WHERE id=?", (tid,))[0][0] == 1

    def test_legacy_fallback_shared_task_dir_not_deleted(self, env):
        """C1：outputs/task_<id>/ 同时是兜底来源与登记共享目录——legacy 删除时
        只进 skipped_shared，绝不进删除清单（不被兜底分支连坐删掉）。"""
        t1, t2 = _insert_task(), _insert_task()
        d = env / "outputs" / f"task_{t1}"
        d.mkdir(parents=True)
        (d / "f.txt").write_text("x", encoding="utf-8")
        dr.register_deliverable(f"outputs/task_{t1}", task_id=t1)
        dr.register_deliverable(f"outputs/task_{t1}", task_id=t2)

        resp = asyncio.run(rt.delete_task(t1, delete_artifacts=True))
        assert resp["artifacts_removed"] == []
        assert resp["skipped_shared"] == [
            {"dir": f"outputs/task_{t1}", "shared_with": [t2]}]
        assert d.exists() and (d / "f.txt").exists()

    def test_legacy_fallback_files_dir_shared_not_deleted(self, env):
        """C1：检查点 files_dir 兜底来源被登记为其他任务在用（未关联本任务）
        ——兜底分支同样过共享判定，不删。"""
        t1, t2 = _insert_task(), _insert_task()
        exports = env / "exports"
        exports.mkdir()
        (exports / "a.csv").write_text("x", encoding="utf-8")
        _write_checkpoint(env, t1, {"files_dir": "exports"})
        dr.register_deliverable("exports", task_id=t2)  # 只关联 t2

        resp = asyncio.run(rt.delete_task(t1, delete_artifacts=True))
        assert resp["artifacts_removed"] == []
        assert resp["skipped_shared"] == [{"dir": "exports", "shared_with": [t2]}]
        assert exports.exists() and (exports / "a.csv").exists()

    def test_delete_removes_checkpoint_file(self, env):
        """I3：删除任务连带删除 .checkpoints/task_<id>.json（残留检查点会在
        任务 id 复用/恢复路径被误读）。"""
        tid = _insert_task()
        _write_checkpoint(env, tid, {"files_dir": "outputs/x", "done": 3})
        ck = env / ".checkpoints" / f"task_{tid}.json"
        assert ck.exists()
        resp = asyncio.run(rt.delete_task(tid))
        assert resp["status"] == "success"
        assert not ck.exists()

    def test_legacy_delete_artifacts_skips_shared(self, env):
        """delete_artifacts=true 旧语义：独占目录删除，共享目录跳过并列
        skipped_shared（带 shared_with 明细）。"""
        t1, t2 = _insert_task(), _insert_task()
        own = env / "outputs" / "独占目录"
        own.mkdir(parents=True)
        shared = env / "outputs" / "共享目录"
        shared.mkdir(parents=True)
        dr.register_deliverable("outputs/独占目录", task_id=t1)
        dr.register_deliverable("outputs/共享目录", task_id=t1)
        dr.register_deliverable("outputs/共享目录", task_id=t2)

        resp = asyncio.run(rt.delete_task(t1, delete_artifacts=True))
        assert resp["artifacts_removed"] == ["outputs/独占目录"]
        assert resp["skipped_shared"] == [
            {"dir": "outputs/共享目录", "shared_with": [t2]}]
        assert not own.exists() and shared.exists()

    def test_delete_unlinks_task_but_keeps_history(self, env):
        """不删目录的纯删除：关联清理，deliverables 行保留（目录不 missing）。"""
        tid = _insert_task()
        (env / "outputs" / "唐嫣照片").mkdir(parents=True)
        dr.register_deliverable("outputs/唐嫣照片", task_id=tid)
        resp = asyncio.run(rt.delete_task(tid))
        assert resp["artifacts_deleted"] is False
        assert dr.get_task_dirs(tid) == []
        assert _db_rows("SELECT COUNT(*) FROM deliverables")[0][0] == 1
        assert (env / "outputs" / "唐嫣照片").exists()


# ---------- 沙箱 entries：登记表取名与任务关联 ----------

class TestSandboxEntries:
    def test_registered_semantic_dir_multi_tasks(self, env):
        t1, t2 = _insert_task(), _insert_task()
        (env / "outputs" / "唐嫣照片").mkdir(parents=True)
        dr.register_deliverable("outputs/唐嫣照片", task_id=t2)
        dr.register_deliverable("outputs/唐嫣照片", task_id=t1)
        entries = asyncio.run(rs.list_sandbox_entries())["entries"]
        row = [e for e in entries if e["path"] == "outputs/唐嫣照片"][0]
        assert row["type"] == "deliverable"
        assert row["name"] == "唐嫣照片"        # 语义名以登记表为准
        assert row["task_ids"] == sorted([t1, t2])  # 多任务关联都列出（存活任务）
        assert row["task_id"] == min(t1, t2)    # 兼容单任务字段取首个

    def test_unregistered_dirs_compat(self, env):
        """登记表没有的老目录维持现状：task_<id>→deliverable（task_id 取名称
        解析），其他→dir。"""
        (env / "outputs" / "task_9").mkdir(parents=True)
        (env / "outputs" / "report_dir").mkdir(parents=True)
        entries = asyncio.run(rs.list_sandbox_entries())["entries"]
        by_path = {e["path"]: e for e in entries}
        assert by_path["outputs/task_9"]["type"] == "deliverable"
        assert by_path["outputs/task_9"]["task_id"] == 9
        assert by_path["outputs/task_9"]["task_ids"] == [9]
        assert by_path["outputs/report_dir"]["type"] == "dir"
        assert "task_id" not in by_path["outputs/report_dir"]


# ---------- 系统提示：语义命名约定 ----------

class TestPrompt:
    def test_semantic_naming_convention_present(self):
        src = open(os.path.join(PROJECT_ROOT, "agent", "agent.py"),
                   encoding="utf-8").read()
        assert "outputs/<主题语义名>/" in src      # 语义命名
        assert "ls outputs/" in src               # 动笔前先查看已有目录
        assert "报告-2" in src                     # 撞名数字后缀示例
        assert "outputs/task_<当前任务ID>/" in src  # task_<id> 兜底保留



class TestDeliverableRootGuard:
    """生产实证回归：agent 把检查点 files_dir 写成沙箱根，回填登记出
    dir_path='.' 脏行——登记/查询层必须在源头挡住（执行层 forbidden_reals
    是最后防线）。"""

    def test_is_valid_deliverable_rel(self):
        from api.deliverables_registry import is_valid_deliverable_rel as v
        assert not v(".") and not v("") and not v("outputs")
        assert not v("projects") and not v(".checkpoints")
        assert not v("projects/sub") and not v("tmp/x")      # 分区目录树下不算交付物
        assert not v("../outputs/x") and not v("outputs/../x")
        assert v("outputs/唐嫣照片") and v("outputs/report-2/sub")
        assert v("cbsign_all") and v("exports")              # 检查点历史形态保留

    def test_register_rejects_root(self, env):
        assert dr.register_deliverable(".", task_id=1) is None
        assert dr.register_deliverable("outputs", task_id=1) is None
        assert dr.register_deliverable("projects", task_id=1) is None
        assert dr.register_deliverable("outputs/ok", task_id=1) is not None

    def test_completion_hook_skips_root_files_dir(self, env):
        """files_dir=沙箱根 的检查点不产生任何登记（task_321 实证场景）。"""
        (env / ".checkpoints").mkdir(parents=True)
        (env / ".checkpoints" / "task_9.json").write_text(
            json.dumps({"files_dir": str(env)}), encoding="utf-8")
        registered = dr.register_task_deliverables(9, root=str(env))
        assert registered == [], "files_dir=沙箱根不应登记任何交付物"

    def test_get_task_dirs_filters_dirty_rows(self, env):
        """历史脏行（dir_path='.'）即使已在表里也不展示不流转。"""
        tid = _insert_task()
        conn = sqlite3.connect(db_mod.DB_PATH)
        conn.execute("INSERT INTO deliverables (dir_path, name) VALUES ('.', '.')")
        conn.execute("INSERT INTO deliverables (dir_path, name) "
                     "VALUES ('outputs/a', 'a')")
        conn.execute("INSERT INTO task_deliverables (task_id, deliverable_id) "
                     "SELECT ?, id FROM deliverables", (tid,))
        conn.commit()
        conn.close()
        dirs = dr.get_task_dirs(tid)
        assert [d["dir_path"] for d in dirs] == ["outputs/a"]
