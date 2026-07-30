# -*- coding: utf-8 -*-
"""沙箱治理（二期）测试：Janitor TTL/硬水位/清单、保留标记（pins）、
任务删除联动交付物（delete_artifacts）、清理记录端点（janitor_log）、
entries 水位（watermark）与 tmp 展开 pinned 标志；评审修复轮（I1 junction
剪枝真实直测 / I2 统一统计引擎 / I3 files_dir 联动与明细 / Minor 1/3/4/5）。

- janitor 线程逻辑直调 run_janitor_once 测（不真等 interval）；
- 水位总量复用一期条目统计缓存聚合（二期 I2），测试直接播种 rs._stats_cache；
- 链接场景：真实 junction（mklink /J，评审实测本机可建）直测统计剪枝与
  janitor 删除 happy path；逃逸场景沿用一期 _mock_realpath 伪装兜底；
- janitor 数据文件（pins/清单日志）由 tests/conftest.py 全局重定向到临时目录，
  不落真实 data/。
"""
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
import types

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import api.task_core as tc  # noqa: E402
import api.routes.routes_sandbox as rs  # noqa: E402
import core.sandbox_janitor as sj  # noqa: E402
from fastapi import HTTPException  # noqa: E402

_GB = 1024 ** 3


def _cfg(**over):
    cfg = {"enabled": True, "tmp_ttl_days": 7, "interval_hours": 1.0,
           "soft_gb": 20, "hard_gb": 50}
    cfg.update(over)
    return cfg


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """假 sandbox + janitor 配置口径指向临时目录；重置一期统计缓存（水位总量
    由同一份缓存聚合，二期 I2——重置它即重置水位）。"""
    sb = tmp_path / "workspace"
    sb.mkdir()
    monkeypatch.setattr(rs, "load_config", lambda: {"sandbox_dir": str(sb)})
    monkeypatch.setattr(tc, "load_config", lambda: {"sandbox_dir": str(sb)})
    monkeypatch.setattr(sj, "load_config", lambda: {"sandbox_dir": str(sb)})
    with rs._stats_lock:
        rs._reset_stats_locked()
        rs._stats_active = 0
        rs._stats_root = None
    yield sb
    with rs._stats_lock:
        rs._reset_stats_locked()
        rs._stats_active = 0
        rs._stats_root = None


def _seed_stats(rel_path, size, partial=False):
    """播种一期条目统计缓存（真实遍历结果直接注入，跳过异步等待）。"""
    with rs._stats_lock:
        rs._stats_cache[rel_path] = {
            "size": size, "file_count": 1, "partial": partial, "ts": time.time(),
        }


def _seed_total(root, size):
    """播种总大小水位（二期 I2 聚合口径：各顶层条目统计之和——本文件测试
    沙箱仅 tmp 一个顶层目录，播种 tmp 条目缓存即得总大小）。"""
    _seed_stats("tmp", size)


def _age(path, days):
    """把条目自身 mtime 拨到 N 天前（目录须在建完内容后调用——建子项会刷新 mtime）。"""
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def _read_log():
    path = sj._log_path()
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _mock_realpath(monkeypatch, mapping):
    """把 mapping 里路径的 realpath 伪装成指定目标，模拟 junction/symlink 逃逸。"""
    real = os.path.realpath
    norm_map = {
        os.path.normcase(os.path.abspath(k)): os.path.normcase(os.path.abspath(v))
        for k, v in mapping.items()
    }

    def fake(p):
        return norm_map.get(os.path.normcase(os.path.abspath(str(p))), real(p))

    monkeypatch.setattr(os.path, "realpath", fake)
    return fake


# ---------- Janitor：TTL 判定 ----------

class TestJanitorTtl:
    def test_old_entries_deleted_fresh_kept(self, sandbox):
        tmp = sandbox / "tmp"
        tmp.mkdir()
        old_file = tmp / "old.txt"
        old_file.write_text("x", encoding="utf-8")
        fresh_file = tmp / "fresh.txt"
        fresh_file.write_text("x", encoding="utf-8")
        stale_dir = tmp / "stale_dir"
        stale_dir.mkdir()
        (stale_dir / "inner.txt").write_text("x", encoding="utf-8")
        _age(old_file, 10)
        _age(stale_dir, 10)   # 目录取自身 mtime（建完内容后再拨）
        _seed_total(sandbox, 1)   # 有缓存且远低于水位 → TTL 模式

        summary = sj.run_janitor_once(str(sandbox), _cfg())
        assert summary["round"] == "ok" and summary["mode"] == "ttl"
        assert summary["deleted"] == 2
        assert not old_file.exists() and not stale_dir.exists()
        assert fresh_file.exists()
        assert tmp.exists()   # tmp 目录本身保留

    def test_ttl_boundary_fresh_not_deleted(self, sandbox):
        tmp = sandbox / "tmp"
        tmp.mkdir()
        borderline = tmp / "borderline.txt"
        borderline.write_text("x", encoding="utf-8")
        _age(borderline, 6)   # < tmp_ttl_days=7 → 保留
        _seed_total(sandbox, 1)
        summary = sj.run_janitor_once(str(sandbox), _cfg())
        assert summary["deleted"] == 0
        assert borderline.exists()

    def test_tmp_missing_silent_skip(self, sandbox):
        _seed_total(sandbox, 1)
        summary = sj.run_janitor_once(str(sandbox), _cfg())
        assert summary["round"] == "skipped_tmp_missing"
        assert not os.path.isfile(sj._log_path())   # 静默：不写清单


# ---------- Janitor：清单写入 ----------

class TestJanitorManifest:
    def test_manifest_written_fresh_not_logged(self, sandbox):
        tmp = sandbox / "tmp"
        tmp.mkdir()
        old = tmp / "old.txt"
        old.write_text("x", encoding="utf-8")
        _age(old, 10)
        (tmp / "fresh.txt").write_text("x", encoding="utf-8")
        _seed_total(sandbox, 1)

        sj.run_janitor_once(str(sandbox), _cfg())
        recs = _read_log()
        assert len(recs) == 1   # 未到期条目不记录
        rec = recs[0]
        assert rec["entry"] == "old.txt" and rec["type"] == "file"
        assert rec["reason"] == "ttl" and rec["result"] == "deleted"
        assert rec["ts"] and rec["size"] is None   # 缓存无 → None

    def test_manifest_appends_across_runs(self, sandbox):
        tmp = sandbox / "tmp"
        tmp.mkdir()
        for i in range(2):
            f = tmp / f"old{i}.txt"
            f.write_text("x", encoding="utf-8")
            _age(f, 10)
            # 删除后总大小缓存会失效（下轮无缓存将跳过并重新遍历）——每轮重新播种
            _seed_total(sandbox, 1)
            sj.run_janitor_once(str(sandbox), _cfg())
        assert len(_read_log()) == 2

    def test_manifest_size_from_stats_cache(self, sandbox):
        tmp = sandbox / "tmp"
        tmp.mkdir()
        d = tmp / "stale"
        d.mkdir()
        (d / "a.bin").write_bytes(b"12345")
        _age(d, 10)
        _seed_stats("tmp/stale", 5)
        _seed_total(sandbox, 1)
        sj.run_janitor_once(str(sandbox), _cfg())
        rec = _read_log()[0]
        assert rec["size"] == 5 and rec["type"] == "dir"
        assert "size_partial" not in rec   # 精确统计不带 partial 标志

    def test_manifest_marks_partial_size(self, sandbox):
        """评审 Minor 1：size 来自 partial 统计时清单带 size_partial=True，
        读者不会把低估值当精确值。"""
        tmp = sandbox / "tmp"
        tmp.mkdir()
        d = tmp / "stale"
        d.mkdir()
        (d / "a.bin").write_bytes(b"12345")
        _age(d, 10)
        _seed_stats("tmp/stale", 3, partial=True)
        _seed_total(sandbox, 1)
        sj.run_janitor_once(str(sandbox), _cfg())
        rec = _read_log()[0]
        assert rec["size"] == 3 and rec["size_partial"] is True


# ---------- Janitor：保留标记（pinned）跳过 ----------

class TestJanitorPinned:
    def test_pinned_old_entry_skipped_and_logged(self, sandbox):
        tmp = sandbox / "tmp"
        tmp.mkdir()
        keep = tmp / "keepme"
        keep.write_text("x", encoding="utf-8")
        _age(keep, 30)
        old = tmp / "old.txt"
        old.write_text("x", encoding="utf-8")
        _age(old, 10)
        sj.set_pinned("keepme", True)
        _seed_total(sandbox, 1)

        summary = sj.run_janitor_once(str(sandbox), _cfg())
        assert summary["deleted"] == 1 and summary["skipped_pinned"] == 1
        assert keep.exists() and not old.exists()
        pin_rec = [r for r in _read_log() if r["entry"] == "keepme"][0]
        assert pin_rec["result"] == "skipped_pinned" and pin_rec["reason"] == "ttl"


# ---------- Janitor：链接条目 ----------

class TestJanitorLink:
    def test_link_entry_not_followed(self, sandbox, tmp_path, monkeypatch):
        """tmp 内混入链接条目（realpath 伪装指向沙箱外）：只按链接处理（mock 下
        rmdir 失败计入 skipped_links），绝不跟随进入目标递归删除。"""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "important.txt").write_text("x", encoding="utf-8")
        tmp = sandbox / "tmp"
        tmp.mkdir()
        evil = tmp / "evil"
        evil.mkdir()
        (evil / "payload.txt").write_text("x", encoding="utf-8")
        normal = tmp / "old.txt"
        normal.write_text("x", encoding="utf-8")
        _age(evil, 10)
        _age(normal, 10)
        _mock_realpath(monkeypatch, {str(evil): str(outside)})
        _seed_total(sandbox, 1)

        summary = sj.run_janitor_once(str(sandbox), _cfg())
        assert summary["deleted"] == 1          # normal.txt
        assert summary["skipped_links"] == 1    # evil 按链接处理：mock 下无法 rmdir → 跳过
        assert evil.exists() and (evil / "payload.txt").exists()
        assert (outside / "important.txt").exists()
        link_rec = [r for r in _read_log() if r["entry"] == "evil"][0]
        assert link_rec["result"] == "skipped_link" and link_rec["type"] == "link"

    def test_tmp_itself_link_round_rejected(self, sandbox, tmp_path, monkeypatch):
        """tmp 本身被替换成指向沙箱外的链接：整轮拒绝（同一期 C1 判据）。"""
        outside = tmp_path / "outside"
        outside.mkdir()
        tmp = sandbox / "tmp"
        tmp.mkdir()
        keep = tmp / "keep.txt"
        keep.write_text("x", encoding="utf-8")
        _age(keep, 30)
        _mock_realpath(monkeypatch, {str(tmp): str(outside)})
        _seed_total(sandbox, 1)

        summary = sj.run_janitor_once(str(sandbox), _cfg())
        assert summary["round"] == "skipped_tmp_link" and summary["deleted"] == 0
        assert keep.exists()


# ---------- Janitor：磁盘硬水位 ----------

class TestHardWatermark:
    def test_hard_watermark_clears_regardless_of_ttl(self, sandbox):
        tmp = sandbox / "tmp"
        tmp.mkdir()
        fresh = tmp / "fresh.txt"   # 未到期，TTL 模式会保留；硬水位无视 TTL
        fresh.write_text("x", encoding="utf-8")
        keep = tmp / "keep.txt"
        keep.write_text("x", encoding="utf-8")
        sj.set_pinned("keep.txt", True)
        _seed_total(sandbox, 60 * _GB)   # > hard_gb=50

        summary = sj.run_janitor_once(str(sandbox), _cfg())
        assert summary["mode"] == "hard_watermark"
        assert summary["deleted"] == 1 and summary["skipped_pinned"] == 1
        assert not fresh.exists() and keep.exists() and tmp.exists()
        recs = _read_log()
        assert len(recs) == 2
        assert all(r["reason"] == "hard_watermark" for r in recs)

    def test_no_total_cache_round_skipped(self, sandbox):
        """总大小聚合未就绪（一个目录都没缓存过）：本轮跳过（什么都不删），
        未就绪目录已排入一期线程池暖缓存。"""
        tmp = sandbox / "tmp"
        tmp.mkdir()
        old = tmp / "old.txt"
        old.write_text("x", encoding="utf-8")
        _age(old, 30)
        # fixture 已重置统计缓存 → 聚合必然未就绪

        summary = sj.run_janitor_once(str(sandbox), _cfg())
        assert summary["round"] == "skipped_no_total" and summary["deleted"] == 0
        assert old.exists()

    def test_under_hard_falls_back_to_ttl(self, sandbox):
        tmp = sandbox / "tmp"
        tmp.mkdir()
        fresh = tmp / "fresh.txt"
        fresh.write_text("x", encoding="utf-8")
        _seed_total(sandbox, 1)   # 远低于 soft/hard → TTL 模式

        summary = sj.run_janitor_once(str(sandbox), _cfg())
        assert summary["mode"] == "ttl" and summary["deleted"] == 0
        assert fresh.exists()


# ---------- pins 持久化与死钉清理 ----------

class TestPinsStore:
    def test_pin_persistence_roundtrip(self, sandbox):
        sj.set_pinned("a.txt", True)
        sj.set_pinned("b_dir", True)
        assert sj.load_pins() == {"a.txt", "b_dir"}
        with open(sj._pins_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        assert sorted(data["pinned"]) == ["a.txt", "b_dir"]
        sj.set_pinned("a.txt", False)
        assert sj.load_pins() == {"b_dir"}

    def test_prune_dead_pins(self, sandbox):
        tmp = sandbox / "tmp"
        tmp.mkdir()
        (tmp / "alive.txt").write_text("x", encoding="utf-8")
        sj.set_pinned("alive.txt", True)
        sj.set_pinned("gone.txt", True)
        alive = sj.prune_dead_pins(str(tmp))
        assert alive == {"alive.txt"} == sj.load_pins()


# ---------- POST /api/sandbox/pin ----------

class TestPinEndpoint:
    def test_pin_unpin_via_api_and_entries_flag(self, sandbox):
        tmp = sandbox / "tmp"
        tmp.mkdir()
        (tmp / "a.txt").write_text("x", encoding="utf-8")

        resp = asyncio.run(rs.pin_sandbox_entry(
            rs.SandboxPinRequest(path="tmp/a.txt", pinned=True)))
        assert resp["status"] == "success" and resp["pinned"] is True
        assert resp["path"] == "tmp/a.txt"

        entries = asyncio.run(rs.list_sandbox_entries())["entries"]
        child = [e for e in entries if e["path"] == "tmp/a.txt"][0]
        assert child["pinned"] is True
        # tmp 行自身仍在，且不携带 pinned 标志
        tmp_row = [e for e in entries if e["path"] == "tmp"][0]
        assert "pinned" not in tmp_row

        resp = asyncio.run(rs.pin_sandbox_entry(
            rs.SandboxPinRequest(path="tmp/a.txt", pinned=False)))
        assert resp["pinned"] is False
        entries = asyncio.run(rs.list_sandbox_entries())["entries"]
        child = [e for e in entries if e["path"] == "tmp/a.txt"][0]
        assert child["pinned"] is False

    @pytest.mark.parametrize("bad", ["foo", "projects/x", "tmp", "tmp/a/b", "tmp/.."])
    def test_pin_rejects_non_tmp_top_level(self, sandbox, bad):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(rs.pin_sandbox_entry(rs.SandboxPinRequest(path=bad, pinned=True)))
        assert ei.value.status_code == 400

    def test_pin_rejects_absolute_path(self, sandbox):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(rs.pin_sandbox_entry(
                rs.SandboxPinRequest(path=str(sandbox / "tmp" / "x"), pinned=True)))
        assert ei.value.status_code == 400

    def test_pin_missing_entry_404(self, sandbox):
        (sandbox / "tmp").mkdir()
        with pytest.raises(HTTPException) as ei:
            asyncio.run(rs.pin_sandbox_entry(
                rs.SandboxPinRequest(path="tmp/nope", pinned=True)))
        assert ei.value.status_code == 404


# ---------- entries：水位（watermark） ----------

class TestWatermark:
    def test_entries_watermark_levels(self, sandbox):
        (sandbox / "tmp").mkdir()
        _seed_total(sandbox, 30 * _GB)   # ∈ [soft 20, hard 50)
        wm = asyncio.run(rs.list_sandbox_entries())["watermark"]
        assert wm["level"] == "soft" and wm["total_size"] == 30 * _GB
        assert wm["soft_bytes"] == 20 * _GB and wm["hard_bytes"] == 50 * _GB

        _seed_total(sandbox, 60 * _GB)
        wm = asyncio.run(rs.list_sandbox_entries())["watermark"]
        assert wm["level"] == "hard"

        _seed_total(sandbox, 1)
        wm = asyncio.run(rs.list_sandbox_entries())["watermark"]
        assert wm["level"] == "ok"

    def test_entries_watermark_pending_when_no_cache(self, sandbox, monkeypatch):
        """总大小尚未统计完：total_size=None、level=ok（前端不告警，轮询等待）。
        fake 线程下统计 worker 不真正跑，聚合未就绪是确定性的。"""
        (sandbox / "big").mkdir()
        spawned = []

        class _FakeThread:
            def __init__(self, target=None, args=(), daemon=None, **kw):
                spawned.append({"target": target, "args": args})

            def start(self):
                pass

        monkeypatch.setattr(rs, "threading", types.SimpleNamespace(Thread=_FakeThread))
        wm = asyncio.run(rs.list_sandbox_entries())["watermark"]
        assert wm["level"] == "ok" and wm["total_size"] is None
        # 二期 I2：不会为水位单独调度全树遍历——排队的只有各顶层目录自己的统计
        for th in spawned:
            assert th["args"][1] != str(sandbox)


# ---------- GET /api/sandbox/janitor_log ----------

class TestJanitorLogEndpoint:
    def test_tail_and_limit_newest_first(self, sandbox):
        recs = [sj.make_record(f"e{i}", "file", None, "ttl", "deleted") for i in range(5)]
        sj.append_manifest(recs)
        resp = asyncio.run(rs.get_janitor_log(limit=3))
        assert [r["entry"] for r in resp["records"]] == ["e4", "e3", "e2"]
        resp = asyncio.run(rs.get_janitor_log(limit=50))
        assert len(resp["records"]) == 5

    def test_missing_log_returns_empty(self, sandbox):
        resp = asyncio.run(rs.get_janitor_log(limit=50))
        assert resp["records"] == []


# ---------- clean_tmp：手动清理写清单 ----------

class TestCleanTmpManifest:
    def test_manual_clean_writes_manifest_and_prunes_pins(self, sandbox):
        tmp = sandbox / "tmp"
        tmp.mkdir()
        (tmp / "a.py").write_text("x", encoding="utf-8")
        (tmp / "b.py").write_text("x", encoding="utf-8")
        sj.set_pinned("a.py", True)

        resp = asyncio.run(rs.clean_tmp())
        assert resp["removed"] == 2   # 手动清空是显式动作，不豁免 pinned
        recs = _read_log()
        assert len(recs) == 2
        assert all(r["reason"] == "manual" and r["result"] == "deleted" for r in recs)
        assert sj.load_pins() == set()   # 死钉已清


# ---------- DELETE /api/tasks/{id}?delete_artifacts=true ----------

class TestDeleteArtifacts:
    @pytest.fixture()
    def task_env(self, sandbox, tmp_path, monkeypatch):
        """tmp DB + 摘掉 goals 联动写（delete_task 会 load-modify-save 真实
        data/goals.json，测试中必须拦截）。"""
        import api.db as db_mod
        db_file = str(tmp_path / "test.db")
        monkeypatch.setattr(db_mod, "DB_PATH", db_file)
        db_mod.init_db()
        import api.routes.routes_tasks as rt
        monkeypatch.setattr(rt, "DB_PATH", db_file)
        monkeypatch.setattr("tools.task_plan.update_goals", lambda fn: None)
        return rt

    def _insert_task(self, rt):
        conn = sqlite3.connect(rt.DB_PATH)
        cur = conn.execute(
            "INSERT INTO tasks (title, user_query, status) VALUES ('t','q','completed')")
        tid = cur.lastrowid
        conn.commit()
        conn.close()
        return tid

    def test_delete_artifacts_true_removes_outputs_dir(self, sandbox, task_env):
        tid = self._insert_task(task_env)
        d = sandbox / "outputs" / f"task_{tid}"
        d.mkdir(parents=True)
        (d / "report.md").write_text("x", encoding="utf-8")

        resp = asyncio.run(task_env.delete_task(tid, delete_artifacts=True))
        assert resp["status"] == "success"
        assert resp["artifacts_deleted"] is True
        assert not d.exists()

    def test_default_keeps_outputs_dir(self, sandbox, task_env):
        tid = self._insert_task(task_env)
        d = sandbox / "outputs" / f"task_{tid}"
        d.mkdir(parents=True)

        resp = asyncio.run(task_env.delete_task(tid))
        assert resp["artifacts_deleted"] is False
        assert d.exists()

    def test_delete_artifacts_missing_dir_no_error(self, sandbox, task_env):
        tid = self._insert_task(task_env)
        resp = asyncio.run(task_env.delete_task(tid, delete_artifacts=True))
        assert resp["artifacts_deleted"] is False

    def test_delete_artifacts_link_not_followed(self, sandbox, task_env,
                                                tmp_path, monkeypatch):
        """outputs/task_<id> 为逃逸链接（realpath 伪装指向沙箱外）：只按链接
        处理（mock 下 rmdir 非空目录失败 → 未删），绝不跟随删除目标内容。"""
        tid = self._insert_task(task_env)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("x", encoding="utf-8")
        d = sandbox / "outputs" / f"task_{tid}"
        d.mkdir(parents=True)
        (d / "inner.txt").write_text("x", encoding="utf-8")
        _mock_realpath(monkeypatch, {str(d): str(outside)})

        resp = asyncio.run(task_env.delete_task(tid, delete_artifacts=True))
        assert resp["artifacts_deleted"] is False
        assert len(resp["artifacts_errors"]) == 1   # 失败如实进明细（评审 I3）
        assert (outside / "keep.txt").exists()
        assert (d / "inner.txt").exists()

    # ---------- 评审 I3：files_dir 来源联动 ----------

    def _write_checkpoint(self, sb, task_id, data):
        ckpt_dir = sb / ".checkpoints"
        ckpt_dir.mkdir(exist_ok=True)
        (ckpt_dir / f"task_{task_id}.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_delete_artifacts_covers_checkpoint_files_dir(self, sandbox, task_env):
        """勾选框依据是两来源合并列表，删除须同范围（评审 I3）：检查点
        files_dir 与 outputs/task_<id> 都删，明细如实列出。"""
        tid = self._insert_task(task_env)
        exports = sandbox / "exports"
        exports.mkdir()
        (exports / "a.csv").write_text("x", encoding="utf-8")
        out = sandbox / "outputs" / f"task_{tid}"
        out.mkdir(parents=True)
        (out / "report.md").write_text("x", encoding="utf-8")
        self._write_checkpoint(sandbox, tid, {"files_dir": "exports"})

        resp = asyncio.run(task_env.delete_task(tid, delete_artifacts=True))
        assert resp["artifacts_deleted"] is True
        assert sorted(resp["artifacts_removed"]) == ["exports", f"outputs/task_{tid}"]
        assert resp["artifacts_errors"] == []
        assert not exports.exists() and not out.exists()

    def test_delete_artifacts_files_dir_only(self, sandbox, task_env):
        """交付物仅在 files_dir（无 outputs 目录）时勾选不再是空操作。"""
        tid = self._insert_task(task_env)
        exports = sandbox / "exports"
        exports.mkdir()
        (exports / "a.csv").write_text("x", encoding="utf-8")
        self._write_checkpoint(sandbox, tid, {"files_dir": "exports"})

        resp = asyncio.run(task_env.delete_task(tid, delete_artifacts=True))
        assert resp["artifacts_removed"] == ["exports"]
        assert resp["artifacts_errors"] == []
        assert not exports.exists()

    def test_delete_artifacts_same_dir_deduplicated(self, sandbox, task_env):
        """files_dir 与 outputs/task_<id> 同目录 → 只删一次（与 artifacts
        端点去重口径一致）。"""
        tid = self._insert_task(task_env)
        out = sandbox / "outputs" / f"task_{tid}"
        out.mkdir(parents=True)
        (out / "f.txt").write_text("x", encoding="utf-8")
        self._write_checkpoint(sandbox, tid, {"files_dir": f"outputs/task_{tid}"})

        resp = asyncio.run(task_env.delete_task(tid, delete_artifacts=True))
        assert resp["artifacts_removed"] == [f"outputs/task_{tid}"]
        assert resp["artifacts_errors"] == []
        assert not out.exists()

    def test_delete_artifacts_files_dir_escape_ignored(self, sandbox, task_env,
                                                       tmp_path):
        """files_dir 越出沙箱 → 该来源跳过（与 artifacts 端点同一 realpath
        口径），不算错误、不报已删。"""
        tid = self._insert_task(task_env)
        outside = tmp_path / "secret"
        outside.mkdir()
        (outside / "s.txt").write_text("x", encoding="utf-8")
        self._write_checkpoint(sandbox, tid, {"files_dir": str(outside)})

        resp = asyncio.run(task_env.delete_task(tid, delete_artifacts=True))
        assert resp["artifacts_deleted"] is False
        assert resp["artifacts_removed"] == [] and resp["artifacts_errors"] == []
        assert (outside / "s.txt").exists()

    def test_delete_artifacts_partition_dir_refused(self, sandbox, task_env):
        """files_dir 指向分区目录（outputs 整区）→ 拒绝删除并记入 errors，
        分区内容完好（同一期 _FORBIDDEN_NAMES 判据）。"""
        tid = self._insert_task(task_env)
        (sandbox / "outputs" / "task_other").mkdir(parents=True)
        (sandbox / "outputs" / "task_other" / "x.txt").write_text("x", encoding="utf-8")
        self._write_checkpoint(sandbox, tid, {"files_dir": "outputs"})

        resp = asyncio.run(task_env.delete_task(tid, delete_artifacts=True))
        assert resp["artifacts_removed"] == []
        assert len(resp["artifacts_errors"]) == 1
        assert resp["artifacts_errors"][0]["path"] == "outputs"
        assert (sandbox / "outputs" / "task_other" / "x.txt").exists()

    def test_delete_artifacts_failure_reported(self, sandbox, task_env, monkeypatch):
        """删除失败如实进 artifacts_errors（评审 I3：不再只 print 吞掉）。"""
        tid = self._insert_task(task_env)
        out = sandbox / "outputs" / f"task_{tid}"
        out.mkdir(parents=True)
        (out / "report.md").write_text("x", encoding="utf-8")

        def _boom(p, *a, **kw):
            raise OSError("disk busy")

        monkeypatch.setattr(task_env.shutil, "rmtree", _boom)
        resp = asyncio.run(task_env.delete_task(tid, delete_artifacts=True))
        assert resp["artifacts_deleted"] is False
        assert resp["artifacts_removed"] == []
        assert len(resp["artifacts_errors"]) == 1
        assert "disk busy" in resp["artifacts_errors"][0]["error"]
        assert out.exists()


# ---------- 评审 I1：统计遍历 junction 剪枝（真实 junction 直测）----------

def _make_junction(link, target):
    """创建 Windows 真实 junction（mklink /J 无需管理员——评审本机实测可建）。"""
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       capture_output=True, text=True)
    return r.returncode == 0


class TestJunctionPruning:
    def test_stats_worker_prunes_escaping_junction(self, sandbox, tmp_path):
        """I1（真实 junction）：os.walk 会跟随 junction 越出遍历根——剪枝后
        外部内容不计入 size/file_count，剪除数写入缓存 pruned_links。"""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "big.bin").write_bytes(b"x" * 100)
        big = sandbox / "big"
        big.mkdir()
        (big / "own.txt").write_bytes(b"12345")
        if not _make_junction(big / "esc", outside):
            pytest.skip("本机无法创建 junction")
        assert (big / "esc").is_dir()   # junction 跟随可见（确认建的确实是目录链接）

        with rs._stats_lock:
            gen = rs._stats_generation
            rs._stats_active = 1   # 直接调 worker：补齐调度侧计数，结束时归零
        rs._stats_worker("big", str(big), gen)

        with rs._stats_lock:
            entry = rs._stats_cache.get("big")
        assert entry is not None
        assert entry["size"] == 5 and entry["file_count"] == 1   # 外部 big.bin 未计入
        assert entry["pruned_links"] == 1
        assert (outside / "big.bin").exists()

    def test_stats_worker_counts_normal_subdir(self, sandbox):
        """对照：普通子目录内容正常计入（剪枝不误伤）。"""
        big = sandbox / "big"
        (big / "sub").mkdir(parents=True)
        (big / "sub" / "a.txt").write_bytes(b"12")
        with rs._stats_lock:
            gen = rs._stats_generation
            rs._stats_active = 1
        rs._stats_worker("big", str(big), gen)
        with rs._stats_lock:
            entry = rs._stats_cache.get("big")
        assert entry["size"] == 2 and entry["file_count"] == 1
        assert entry["pruned_links"] == 0

    def test_janitor_deletes_real_junction_link_only(self, sandbox, tmp_path):
        """真实 junction 的 janitor 删除 happy path（评审 Minor 8）：realpath
        判据识别为链接，os.rmdir 只删链接本体，目标内容完好。"""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("x", encoding="utf-8")
        tmp = sandbox / "tmp"
        tmp.mkdir()
        link = tmp / "esc"
        if not _make_junction(link, outside):
            pytest.skip("本机无法创建 junction")
        _seed_total(sandbox, 60 * _GB)   # 硬水位模式无视 TTL（junction 自身 mtime 新）

        summary = sj.run_janitor_once(str(sandbox), _cfg())
        assert summary["mode"] == "hard_watermark"
        assert summary["deleted"] == 1 and summary["skipped_links"] == 0
        assert not link.exists()                 # 链接本体已删
        assert (outside / "keep.txt").exists()   # 目标内容完好
        rec = _read_log()[0]
        assert rec["type"] == "link" and rec["result"] == "deleted"


# ---------- 评审 I2：统一统计引擎 ----------

class TestUnifiedStatsEngine:
    def test_watermark_aggregates_entry_stats_no_whole_tree_walk(self, sandbox,
                                                                 monkeypatch):
        """I2：watermark 总量 = 各条目统计之和（tmp 行 + 普通目录 + outputs/task_*
        + 顶层散文件），不触发独立全树遍历（os.walk 不会被以沙箱根调用）。"""
        (sandbox / "tmp").mkdir()
        (sandbox / "proj").mkdir()
        (sandbox / "outputs" / "task_3").mkdir(parents=True)
        (sandbox / "file.txt").write_bytes(b"12345")
        _seed_stats("tmp", 100)
        _seed_stats("proj", 200)
        _seed_stats("outputs/task_3", 50)
        walk_calls = []
        real_walk = os.walk

        def _spy_walk(p, *a, **kw):
            walk_calls.append(os.path.normcase(os.path.abspath(str(p))))
            return real_walk(p, *a, **kw)

        monkeypatch.setattr(os, "walk", _spy_walk)
        resp = asyncio.run(rs.list_sandbox_entries())
        wm = resp["watermark"]
        # 聚合 = 100(tmp) + 200(proj) + 50(task_3) + 5(file.txt)；全部就绪 → 非 partial
        assert wm["total_size"] == 355 and wm["partial"] is False
        assert resp["total_size"] == 355   # 顶层 total_size 与水位同口径
        assert os.path.normcase(os.path.abspath(str(sandbox))) not in walk_calls

    def test_watermark_partial_when_entry_pending(self, sandbox, monkeypatch):
        """I2：部分条目未就绪 → 总量为已就绪下界且 partial=True。"""
        (sandbox / "a").mkdir()
        (sandbox / "b").mkdir()
        _seed_stats("a", 100)
        spawned = []

        class _FakeThread:
            def __init__(self, target=None, args=(), daemon=None, **kw):
                spawned.append({"target": target, "args": args})

            def start(self):
                pass

        monkeypatch.setattr(rs, "threading", types.SimpleNamespace(Thread=_FakeThread))
        wm = asyncio.run(rs.list_sandbox_entries())["watermark"]
        assert wm["total_size"] == 100 and wm["partial"] is True
        assert wm["level"] == "ok"

    def test_janitor_has_no_independent_total_walker(self):
        """I2：janitor 模块不再保留独立全树遍历（水位统一走一期统计引擎）。"""
        assert not hasattr(sj, "_total_worker")
        assert not hasattr(sj, "get_total_size_async")
        assert not hasattr(sj, "peek_total_size")

    def test_aggregate_total_size_helper(self, sandbox):
        """janitor 侧聚合入口：顶层目录缓存 + 散文件求和；outputs 展开口径与
        entries 一致；未就绪目录 schedule=True 时排入一期线程池。"""
        (sandbox / "tmp").mkdir()
        (sandbox / "outputs" / "task_7").mkdir(parents=True)
        (sandbox / "note.txt").write_bytes(b"x" * 12)
        _seed_stats("tmp", 10)
        _seed_stats("outputs/task_7", 20)
        agg = sj._aggregate_total_size(str(sandbox), schedule=False)
        assert agg == {"size": 42, "partial": False}


# ---------- 评审 Minor 5：tmp_ttl_days 负值钳制 ----------

class TestTtlClamp:
    def test_negative_ttl_clamped_to_zero(self, sandbox, monkeypatch):
        monkeypatch.setattr(
            sj, "load_config",
            lambda: {"sandbox_janitor": {"tmp_ttl_days": -5}})
        assert sj.load_janitor_config()["tmp_ttl_days"] == 0.0

    def test_zero_ttl_deletes_existing_entries(self, sandbox):
        """ttl=0（含负值钳制到 0 的语义）：任何已存在的条目都「已过期」。"""
        tmp = sandbox / "tmp"
        tmp.mkdir()
        f = tmp / "old.txt"
        f.write_text("x", encoding="utf-8")
        _age(f, 0.001)   # 86 秒前：避免与 now 同计时器刻度导致的边界抖动
        _seed_total(sandbox, 1)
        summary = sj.run_janitor_once(str(sandbox), _cfg(tmp_ttl_days=0))
        assert summary["deleted"] == 1 and not f.exists()


# ---------- 评审 Minor 3：pin 端点拒绝链接条目 ----------

class TestPinLinkRejected:
    def test_pin_escaping_link_rejected_403(self, sandbox, tmp_path, monkeypatch):
        """链接条目（realpath 判据）不允许 pin——与 entries 跳过逃逸链接的
        口径一致。"""
        outside = tmp_path / "outside"
        outside.mkdir()
        tmp = sandbox / "tmp"
        tmp.mkdir()
        link = tmp / "esc"
        link.mkdir()
        _mock_realpath(monkeypatch, {str(link): str(outside)})
        with pytest.raises(HTTPException) as ei:
            asyncio.run(rs.pin_sandbox_entry(
                rs.SandboxPinRequest(path="tmp/esc", pinned=True)))
        assert ei.value.status_code == 403
        assert sj.load_pins() == set()


# ---------- 评审 Minor 4：清单日志轮转 ----------

class TestLogRotation:
    def test_rotation_keeps_tail(self, sandbox, monkeypatch):
        """超阈值截断保留尾部：最新记录仍在、最旧被截掉、每行仍是完整 JSON。"""
        monkeypatch.setattr(sj, "_LOG_MAX_BYTES", 2048)
        monkeypatch.setattr(sj, "_LOG_KEEP_BYTES", 512)
        for i in range(30):
            sj.append_manifest([sj.make_record(f"old{i}", "file", None, "ttl", "deleted")])
        path = sj._log_path()
        assert os.path.getsize(path) <= 2048 + 512   # 轮转后文件收敛
        recs = sj.read_manifest_tail(100)
        entries = [r["entry"] for r in recs]
        assert "old29" in entries and "old0" not in entries
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    json.loads(line)   # 行边界对齐，无半行 JSON


# ---------- 评审 I3：前端删除结果提示（源码级回归）----------

class TestFrontendHonesty:
    def test_delete_result_messages_use_detail_fields(self):
        """前端删除结果提示读取 artifacts_removed/artifacts_errors 明细并配
        三态文案（成功 N 项/失败 N 项/无交付物），不再只弹通用成功文案——
        源码级回归（同 test_interrupt_restart_sync 的 ws 源码检查风格）。"""
        for rel in ("vue-app/src/views/TasksView.vue",
                    "vue-app/src/views/TaskDetailView.vue"):
            src = open(os.path.join(PROJECT_ROOT, *rel.split("/")),
                       encoding="utf-8").read()
            assert "artifacts_removed" in src and "artifacts_errors" in src
            assert "deleteArtifactsDeleted" in src and "deleteArtifactsFailed" in src
        zh = open(os.path.join(PROJECT_ROOT, "vue-app/src/i18n/zh.js"),
                  encoding="utf-8").read()
        for key in ("deleteArtifactsDeleted", "deleteArtifactsFailed",
                    "deleteArtifactsNone"):
            assert key in zh



# ── 设置页持久化（sandbox_janitor 节）──

import api.routes.routes_settings as rset  # noqa: E402


class TestJanitorSettings:
    """设置页可配 janitor（用户反馈：阈值/TTL/开关应进设置页，不只 config.json）。"""

    def test_sanitize_whitelist_and_clamp(self):
        out = rset._sanitize_janitor_section({
            "enabled": 0, "tmp_ttl_days": -3, "interval_hours": 0,
            "soft_gb": "15", "hard_gb": 99.5, "bogus_key": 1})
        assert out["enabled"] is False
        assert out["tmp_ttl_days"] == 0          # 负值钳制
        assert out["interval_hours"] == 0.01     # 下限钳制
        assert out["soft_gb"] == 15              # 数字字符串可转
        assert out["hard_gb"] == 99.5
        assert "bogus_key" not in out            # 白名单

    def test_sanitize_bad_value_400(self):
        with pytest.raises(HTTPException) as ei:
            rset._sanitize_janitor_section({"tmp_ttl_days": "abc"})
        assert ei.value.status_code == 400

    def test_get_settings_exposes_janitor(self, monkeypatch):
        monkeypatch.setattr(sj, "load_config", lambda: {
            "sandbox_janitor": {"tmp_ttl_days": 3, "hard_gb": 88}})
        data = asyncio.run(rset.get_settings())
        j = data["sandbox_janitor"]
        assert j["tmp_ttl_days"] == 3
        assert j["hard_gb"] == 88
        assert j["enabled"] is True              # 缺省合并
        assert j["soft_gb"] == 20

    def test_post_settings_persists_janitor(self, monkeypatch, tmp_path):
        """POST 增量保存：sandbox_janitor 合并写入 config.json，其他节不受影响。"""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps(
            {"sandbox_janitor": {"tmp_ttl_days": 5}, "default_model": "m"}),
            encoding="utf-8")
        import api.config as acfg
        monkeypatch.setattr(acfg, "CONFIG_PATH", str(cfg_file))
        monkeypatch.setattr(rset, "load_config", acfg.load_config)
        # .env 等数据文件重定向到临时目录，不碰真实 data/
        monkeypatch.setattr(rset, "get_data_path",
                            lambda name: str(tmp_path / name))
        upd = rset.ConfigUpdate(sandbox_janitor={"hard_gb": 66, "unknown": 1})
        asyncio.run(rset.update_settings(upd))
        saved = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert saved["sandbox_janitor"]["hard_gb"] == 66
        assert saved["sandbox_janitor"]["tmp_ttl_days"] == 5   # 合并不覆盖
        assert "unknown" not in saved["sandbox_janitor"]
        assert saved["default_model"] == "m"
