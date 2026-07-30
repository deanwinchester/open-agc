# -*- coding: utf-8 -*-
"""沙箱治理（一期）API 测试。

覆盖：
- GET /api/sandbox/entries 分类规则（project/deliverable/installer/temp/dir/file）
  与 size/file_count 异步统计缓存（首返 null → 后台遍历写缓存 → 再返命中）
- POST /api/sandbox/delete：正常删除；绝对路径/../ 逃逸拒绝；.checkpoints 与
  分区目录（projects/outputs/tmp/downloads）拒绝；非顶层路径拒绝；不存在 404
- POST /api/sandbox/move：projects/tmp 正常归类；重名 409；非法 dest 400；
  分区目录 403；目标分区为链接 403
- POST /api/sandbox/clean_tmp：清空但保留目录；幂等；tmp 不存在时返回 0；
  tmp 本身为链接 403；条目级链接只删链接不跟随
- GET /api/tasks/{id}/artifacts：合并检查点 files_dir 与 outputs/task_<id>/；
  同一目录去重；files_dir 越出沙箱被忽略；outputs 链接来源跳过；两者皆无返回空
- 评审修复轮回归：遍历预算按条目间隔检查（I4）；统计线程池并发上限与队列
  去重（I5）；invalidate 代际丢弃在途 worker 写回（I6）；sandbox root 变更
  缓存失效（Minor #7）；entries/交付物展开跳过逃逸链接（I1/Minor #2）

链接（junction/symlink）场景用 monkeypatch 伪装 os.path.realpath（本机 Windows
无 symlink 权限，mock 出真断言而非全靠 skip）。
全部用 tmp_path 构造假 sandbox（monkeypatch sandbox_dir 口径），不碰真实 workspace/。
"""
import asyncio
import itertools
import json
import os
import sys
import time
import types

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import api.task_core as tc  # noqa: E402
import api.routes.routes_sandbox as rs  # noqa: E402
from fastapi import HTTPException  # noqa: E402


def _reset_stats_state():
    """清空统计线程池全部状态并递增失效代际（在途旧代 worker 写回被丢弃）。"""
    with rs._stats_lock:
        rs._reset_stats_locked()
        rs._stats_active = 0
        rs._stats_root = None


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """把 routes_sandbox 与 task_core 的 sandbox_dir 口径都指到临时沙箱，
    并重置统计线程池状态（模块级缓存按 rel_path 作 key，跨用例必须隔离）。"""
    sb = tmp_path / "workspace"
    sb.mkdir()
    monkeypatch.setattr(rs, "load_config", lambda: {"sandbox_dir": str(sb)})
    monkeypatch.setattr(tc, "load_config", lambda: {"sandbox_dir": str(sb)})
    _reset_stats_state()
    yield sb
    _reset_stats_state()


def _entries(sb):
    return asyncio.run(rs.list_sandbox_entries())["entries"]


def _entry_by_name(entries, name):
    for e in entries:
        if e["name"] == name:
            return e
    return None


def _wait_stats(rel_path, timeout=10.0):
    """等待后台统计线程写入缓存（目录小，正常毫秒级完成）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with rs._stats_lock:
            entry = rs._stats_cache.get(rel_path)
        if entry is not None:
            return entry
        time.sleep(0.05)
    return None


def _mock_realpath(monkeypatch, mapping):
    """把 mapping 里路径的 realpath 伪装成指定目标，模拟 junction/symlink 逃逸。
    key/value 均为字符串路径；未命中路径走真实 realpath。"""
    real = os.path.realpath
    norm_map = {
        os.path.normcase(os.path.abspath(k)): os.path.normcase(os.path.abspath(v))
        for k, v in mapping.items()
    }

    def fake(p):
        return norm_map.get(os.path.normcase(os.path.abspath(str(p))), real(p))

    monkeypatch.setattr(os.path, "realpath", fake)
    return fake


# ---------- entries 分类规则 ----------

class TestEntriesClassification:
    def test_classify_all_types(self, sandbox):
        (sandbox / "myrepo" / ".git").mkdir(parents=True)
        (sandbox / "tmp").mkdir()
        (sandbox / "plain_dir").mkdir()
        (sandbox / "outputs" / "task_12").mkdir(parents=True)
        (sandbox / "installer.exe").write_bytes(b"x")
        (sandbox / "note.txt").write_text("hi", encoding="utf-8")

        entries = _entries(sandbox)
        by_name = {e["name"]: e for e in entries}

        assert by_name["myrepo"]["type"] == "project"
        assert by_name["tmp"]["type"] == "temp"
        assert by_name["plain_dir"]["type"] == "dir"
        assert by_name["installer.exe"]["type"] == "installer"
        assert by_name["note.txt"]["type"] == "file"
        # outputs/ 展开一层：task_12 作为 deliverable 条目，带 task_id
        d = by_name["task_12"]
        assert d["type"] == "deliverable"
        assert d["task_id"] == 12
        assert d["path"] == "outputs/task_12"
        assert d["is_dir"] is True
        # outputs 目录本身不单独出现
        assert "outputs" not in by_name

    def test_installer_extensions(self, sandbox):
        for fn in ("a.msi", "b.7z", "c.zip", "d.dmg", "e.pkg"):
            (sandbox / fn).write_bytes(b"x")
        entries = _entries(sandbox)
        for fn in ("a.msi", "b.7z", "c.zip", "d.dmg", "e.pkg"):
            assert _entry_by_name(entries, fn)["type"] == "installer"

    def test_file_size_sync_dir_size_async(self, sandbox):
        (sandbox / "big").mkdir()
        (sandbox / "big" / "a.bin").write_bytes(b"12345")
        (sandbox / "small.txt").write_bytes(b"abc")

        entries = _entries(sandbox)
        # 散文件同步返回大小；目录首访返回 null（后台统计中）
        assert _entry_by_name(entries, "small.txt")["size"] == 3
        d = _entry_by_name(entries, "big")
        assert d["size"] is None and d["file_count"] is None

    def test_async_stats_cache_write_and_hit(self, sandbox):
        (sandbox / "big").mkdir()
        (sandbox / "big" / "a.bin").write_bytes(b"12345")
        (sandbox / "big" / "sub").mkdir()
        (sandbox / "sub2").mkdir()

        # 第一次：触发后台统计，size 为 null
        first = _entry_by_name(_entries(sandbox), "big")
        assert first["size"] is None

        # 后台线程写缓存
        cached = _wait_stats("big")
        assert cached is not None
        assert cached["size"] == 5
        assert cached["file_count"] == 1
        assert cached["partial"] is False

        # 第二次：命中缓存直接返回
        second = _entry_by_name(_entries(sandbox), "big")
        assert second["size"] == 5
        assert second["file_count"] == 1
        # sibling 目录在首次列表时同批触发了后台统计
        cached2 = _wait_stats("sub2")
        assert cached2 is not None
        assert cached2["size"] == 0 and cached2["file_count"] == 0


# ---------- delete ----------

class TestDelete:
    def test_delete_dir_and_file(self, sandbox):
        (sandbox / "junk").mkdir()
        (sandbox / "junk" / "f.txt").write_text("x", encoding="utf-8")
        (sandbox / "old.zip").write_bytes(b"z")

        resp = asyncio.run(rs.delete_sandbox_entry(rs.SandboxPathRequest(path="junk")))
        assert resp["status"] == "success" and resp["deleted"] == "junk"
        assert not (sandbox / "junk").exists()

        asyncio.run(rs.delete_sandbox_entry(rs.SandboxPathRequest(path="old.zip")))
        assert not (sandbox / "old.zip").exists()

    def test_reject_absolute_path(self, sandbox):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(rs.delete_sandbox_entry(
                rs.SandboxPathRequest(path=str(sandbox / "junk"))))
        assert ei.value.status_code == 400

    def test_reject_dotdot_escape(self, sandbox):
        for bad in ("..", "../outside", "a/../../b"):
            with pytest.raises(HTTPException) as ei:
                asyncio.run(rs.delete_sandbox_entry(rs.SandboxPathRequest(path=bad)))
            assert ei.value.status_code in (400, 403)

    def test_reject_non_top_level(self, sandbox):
        (sandbox / "outputs" / "task_1").mkdir(parents=True)
        with pytest.raises(HTTPException) as ei:
            asyncio.run(rs.delete_sandbox_entry(
                rs.SandboxPathRequest(path="outputs/task_1")))
        assert ei.value.status_code == 400
        assert (sandbox / "outputs" / "task_1").exists()

    def test_forbid_checkpoints(self, sandbox):
        (sandbox / ".checkpoints").mkdir()
        with pytest.raises(HTTPException) as ei:
            asyncio.run(rs.delete_sandbox_entry(
                rs.SandboxPathRequest(path=".checkpoints")))
        assert ei.value.status_code == 403
        assert (sandbox / ".checkpoints").exists()

    def test_delete_missing_returns_404(self, sandbox):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(rs.delete_sandbox_entry(rs.SandboxPathRequest(path="nope")))
        assert ei.value.status_code == 404

    def test_reject_symlink_escape(self, sandbox, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        link = sandbox / "escape_link"
        try:
            os.symlink(str(outside), str(link), target_is_directory=True)
        except OSError:
            pytest.skip("当前平台/权限不支持创建符号链接")
        with pytest.raises(HTTPException) as ei:
            asyncio.run(rs.delete_sandbox_entry(
                rs.SandboxPathRequest(path="escape_link")))
        assert ei.value.status_code == 403
        assert outside.exists()


# ---------- move ----------

class TestMove:
    def test_move_to_projects_and_tmp(self, sandbox):
        (sandbox / "proj_a").mkdir()
        resp = asyncio.run(rs.move_sandbox_entry(
            rs.SandboxMoveRequest(path="proj_a", dest="projects")))
        assert resp["status"] == "success"
        assert resp["path"] == "projects/proj_a"
        assert (sandbox / "projects" / "proj_a").is_dir()
        assert not (sandbox / "proj_a").exists()

        (sandbox / "scratch.py").write_text("x", encoding="utf-8")
        asyncio.run(rs.move_sandbox_entry(
            rs.SandboxMoveRequest(path="scratch.py", dest="tmp")))
        assert (sandbox / "tmp" / "scratch.py").is_file()

    def test_move_conflict_returns_409(self, sandbox):
        (sandbox / "proj_a").mkdir()
        (sandbox / "projects" / "proj_a").mkdir(parents=True)
        with pytest.raises(HTTPException) as ei:
            asyncio.run(rs.move_sandbox_entry(
                rs.SandboxMoveRequest(path="proj_a", dest="projects")))
        assert ei.value.status_code == 409
        assert (sandbox / "proj_a").exists()  # 未移动

    def test_move_invalid_dest_rejected(self, sandbox):
        (sandbox / "proj_a").mkdir()
        for bad in ("etc", "", "../x", ".checkpoints", "TMP"):
            with pytest.raises(HTTPException) as ei:
                asyncio.run(rs.move_sandbox_entry(
                    rs.SandboxMoveRequest(path="proj_a", dest=bad)))
            assert ei.value.status_code == 400

    def test_move_to_downloads_and_outputs(self, sandbox):
        """归类目标放开四分区：downloads/outputs 均可作为 dest（评审后用户反馈）。"""
        (sandbox / "big_setup.exe").write_bytes(b"x")
        asyncio.run(rs.move_sandbox_entry(
            rs.SandboxMoveRequest(path="big_setup.exe", dest="downloads")))
        assert (sandbox / "downloads" / "big_setup.exe").is_file()
        (sandbox / "report_dir").mkdir()
        resp = asyncio.run(rs.move_sandbox_entry(
            rs.SandboxMoveRequest(path="report_dir", dest="outputs")))
        assert resp["dest"] == "outputs"
        assert (sandbox / "outputs" / "report_dir").is_dir()

    def test_entries_shows_non_task_outputs_children(self, sandbox):
        """outputs/ 下非 task_<id> 子目录（如手动归类进来的）应可见为 dir 条目。"""
        (sandbox / "outputs" / "report_dir").mkdir(parents=True)
        (sandbox / "outputs" / "task_42").mkdir()
        data = asyncio.run(rs.list_sandbox_entries())
        by_path = {e["path"]: e for e in data["entries"]}
        assert by_path["outputs/report_dir"]["type"] == "dir"
        assert by_path["outputs/task_42"]["type"] == "deliverable"
        assert by_path["outputs/task_42"]["task_id"] == 42

    def test_entries_exposes_janitor_config(self, sandbox):
        data = asyncio.run(rs.list_sandbox_entries())
        j = data["janitor"]
        for key in ("enabled", "tmp_ttl_days", "interval_hours", "soft_gb", "hard_gb"):
            assert key in j

    def test_move_forbid_checkpoints(self, sandbox):
        (sandbox / ".checkpoints").mkdir()
        with pytest.raises(HTTPException) as ei:
            asyncio.run(rs.move_sandbox_entry(
                rs.SandboxMoveRequest(path=".checkpoints", dest="tmp")))
        assert ei.value.status_code == 403

    def test_move_non_top_level_rejected(self, sandbox):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(rs.move_sandbox_entry(
                rs.SandboxMoveRequest(path="outputs/task_1", dest="tmp")))
        assert ei.value.status_code == 400


# ---------- clean_tmp ----------

class TestCleanTmp:
    def test_clean_tmp_removes_children_keeps_dir(self, sandbox):
        (sandbox / "tmp" / "sub").mkdir(parents=True)
        (sandbox / "tmp" / "sub" / "f.txt").write_text("x", encoding="utf-8")
        (sandbox / "tmp" / "a.py").write_text("x", encoding="utf-8")

        resp = asyncio.run(rs.clean_tmp())
        assert resp["removed"] == 2
        assert (sandbox / "tmp").is_dir()
        assert os.listdir(sandbox / "tmp") == []

    def test_clean_tmp_idempotent(self, sandbox):
        (sandbox / "tmp").mkdir()
        (sandbox / "tmp" / "a.py").write_text("x", encoding="utf-8")
        assert asyncio.run(rs.clean_tmp())["removed"] == 1
        assert asyncio.run(rs.clean_tmp())["removed"] == 0

    def test_clean_tmp_missing_dir_returns_zero(self, sandbox):
        resp = asyncio.run(rs.clean_tmp())
        assert resp["status"] == "success" and resp["removed"] == 0


# ---------- artifacts ----------

class TestArtifacts:
    def _write_checkpoint(self, sb, task_id, data):
        ckpt_dir = sb / ".checkpoints"
        ckpt_dir.mkdir(exist_ok=True)
        (ckpt_dir / f"task_{task_id}.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_merge_checkpoint_and_outputs(self, sandbox):
        # 检查点 files_dir 指向独立导出目录；outputs/task_5 也有交付物 → 合并
        (sandbox / "exports").mkdir()
        (sandbox / "exports" / "a.csv").write_bytes(b"1")
        (sandbox / "outputs" / "task_5").mkdir(parents=True)
        (sandbox / "outputs" / "task_5" / "report.md").write_bytes(b"22")
        self._write_checkpoint(sandbox, 5, {"files_dir": "exports"})

        resp = asyncio.run(rs.get_task_artifacts(5))
        assert resp["task_id"] == 5
        by_name = {f["name"]: f for f in resp["files"]}
        assert set(by_name) == {"a.csv", "report.md"}
        assert by_name["a.csv"]["size"] == 1
        assert by_name["a.csv"]["source"] == "checkpoint"
        assert by_name["report.md"]["size"] == 2
        assert by_name["report.md"]["source"] == "outputs"
        assert all(f["mtime"] for f in resp["files"])

    def test_same_dir_deduplicated(self, sandbox):
        # 检查点 files_dir 与 outputs/task_<id>/ 是同一目录 → 去重不重复列
        (sandbox / "outputs" / "task_6").mkdir(parents=True)
        (sandbox / "outputs" / "task_6" / "only.txt").write_bytes(b"x")
        self._write_checkpoint(sandbox, 6, {"files_dir": "outputs/task_6"})

        resp = asyncio.run(rs.get_task_artifacts(6))
        assert [f["name"] for f in resp["files"]] == ["only.txt"]

    def test_empty_when_neither_exists(self, sandbox):
        resp = asyncio.run(rs.get_task_artifacts(999))
        assert resp["files"] == []

    def test_files_dir_escape_ignored(self, sandbox, tmp_path):
        # files_dir 越出沙箱（绝对路径/../）→ 该来源被忽略
        outside = tmp_path / "secret"
        outside.mkdir()
        (outside / "s.txt").write_bytes(b"x")
        self._write_checkpoint(sandbox, 7, {"files_dir": str(outside)})
        resp = asyncio.run(rs.get_task_artifacts(7))
        assert resp["files"] == []

        self._write_checkpoint(sandbox, 8, {"files_dir": "../secret"})
        resp = asyncio.run(rs.get_task_artifacts(8))
        assert resp["files"] == []


# ---------- I3：分区目录保护 ----------

class TestPartitionProtection:
    @pytest.mark.parametrize("name", ["projects", "outputs", "tmp", "downloads"])
    def test_delete_partition_dir_forbidden(self, sandbox, name):
        (sandbox / name).mkdir(exist_ok=True)
        with pytest.raises(HTTPException) as ei:
            asyncio.run(rs.delete_sandbox_entry(rs.SandboxPathRequest(path=name)))
        assert ei.value.status_code == 403
        assert (sandbox / name).exists()

    @pytest.mark.parametrize("name", ["projects", "outputs", "downloads"])
    def test_move_partition_dir_forbidden(self, sandbox, name):
        (sandbox / name).mkdir(exist_ok=True)
        with pytest.raises(HTTPException) as ei:
            asyncio.run(rs.move_sandbox_entry(
                rs.SandboxMoveRequest(path=name, dest="tmp")))
        assert ei.value.status_code == 403
        assert (sandbox / name).exists()

    def test_move_tmp_partition_forbidden(self, sandbox):
        # tmp→projects 同样被保留名集合拦住（name==dest 之外的方向）
        (sandbox / "tmp").mkdir(exist_ok=True)
        with pytest.raises(HTTPException) as ei:
            asyncio.run(rs.move_sandbox_entry(
                rs.SandboxMoveRequest(path="tmp", dest="projects")))
        assert ei.value.status_code == 403
        assert (sandbox / "tmp").exists()


# ---------- C1：clean_tmp 链接防护 ----------

class TestCleanTmpLinkGuard:
    def test_tmp_itself_link_rejected_403(self, sandbox, tmp_path, monkeypatch):
        """tmp 被替换成指向沙箱外的 junction/symlink（realpath 伪装）：403 拒绝，
        tmp 内真实文件原样保留。"""
        outside = tmp_path / "outside"
        outside.mkdir()
        (sandbox / "tmp").mkdir()
        (sandbox / "tmp" / "keep.txt").write_text("x", encoding="utf-8")
        _mock_realpath(monkeypatch, {str(sandbox / "tmp"): str(outside)})

        with pytest.raises(HTTPException) as ei:
            asyncio.run(rs.clean_tmp())
        assert ei.value.status_code == 403
        assert (sandbox / "tmp" / "keep.txt").exists()

    def test_link_entry_inside_tmp_not_followed(self, sandbox, tmp_path, monkeypatch):
        """tmp 内混入链接条目：只按链接处理（mock 下 rmdir 失败计入 skipped_links），
        绝不跟随进入目标递归删除；普通条目正常清理。"""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "important.txt").write_text("x", encoding="utf-8")
        evil = sandbox / "tmp" / "evil"
        evil.mkdir(parents=True)
        (evil / "payload.txt").write_text("x", encoding="utf-8")
        (sandbox / "tmp" / "normal.txt").write_text("x", encoding="utf-8")
        # 伪装 tmp/evil 的 realpath 指向沙箱外
        _mock_realpath(monkeypatch, {str(evil): str(outside)})

        resp = asyncio.run(rs.clean_tmp())
        assert resp["removed"] == 1            # normal.txt 被删除
        assert resp["skipped_links"] == 1      # evil 按链接处理：mock 下无法 rmdir → 跳过
        assert evil.exists()                   # 未被跟随递归删除
        assert (evil / "payload.txt").exists()
        assert not (sandbox / "tmp" / "normal.txt").exists()


# ---------- I1：artifacts / entries 链接来源跳过 ----------

class TestLinkSourceSkipped:
    def test_artifacts_outputs_link_skipped(self, sandbox, tmp_path, monkeypatch):
        """outputs/task_7 为逃逸链接（realpath 伪装指向外部）：来源被跳过，
        外部文件名不泄露。"""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "passwords.txt").write_text("x", encoding="utf-8")
        task_dir = sandbox / "outputs" / "task_7"
        task_dir.mkdir(parents=True)
        (task_dir / "inner.txt").write_text("x", encoding="utf-8")
        _mock_realpath(monkeypatch, {str(task_dir): str(outside)})

        resp = asyncio.run(rs.get_task_artifacts(7))
        assert resp["files"] == []

    def test_entries_deliverable_link_skipped(self, sandbox, tmp_path, monkeypatch):
        """outputs 展开：task_9 为逃逸链接时不列入 deliverable 条目。"""
        outside = tmp_path / "outside"
        outside.mkdir()
        task_dir = sandbox / "outputs" / "task_9"
        task_dir.mkdir(parents=True)
        _mock_realpath(monkeypatch, {str(task_dir): str(outside)})

        entries = _entries(sandbox)
        assert _entry_by_name(entries, "task_9") is None

    def test_entries_top_level_link_skipped(self, sandbox, tmp_path, monkeypatch):
        """顶层条目为逃逸链接：不列举不统计（防外部聚合信息泄露）。"""
        outside = tmp_path / "outside"
        outside.mkdir()
        evil = sandbox / "evil"
        evil.mkdir()
        (sandbox / "normal").mkdir()
        _mock_realpath(monkeypatch, {str(evil): str(outside)})

        entries = _entries(sandbox)
        assert _entry_by_name(entries, "evil") is None
        assert _entry_by_name(entries, "normal") is not None


# ---------- I2：move 目标分区链接防护 ----------

class TestMoveDestLinkGuard:
    def test_dest_partition_link_rejected_403(self, sandbox, tmp_path, monkeypatch):
        """projects 是指向沙箱外的链接（realpath 伪装）：403 拒绝，源条目不移动。"""
        outside = tmp_path / "outside"
        outside.mkdir()
        (sandbox / "projects").mkdir()
        (sandbox / "proj_a").mkdir()
        _mock_realpath(monkeypatch, {str(sandbox / "projects"): str(outside)})

        with pytest.raises(HTTPException) as ei:
            asyncio.run(rs.move_sandbox_entry(
                rs.SandboxMoveRequest(path="proj_a", dest="projects")))
        assert ei.value.status_code == 403
        assert (sandbox / "proj_a").exists()
        assert os.listdir(outside) == []       # 未写出沙箱


# ---------- I4：遍历预算按条目间隔检查 ----------

class TestBudgetInterval:
    def test_flat_dir_budget_interrupts(self, sandbox, monkeypatch):
        """预算 0 + 每 2 条查一次：10 个文件的扁平目录在第 2 个文件处中断，
        partial=True 且 file_count 远小于总数（不会跑完 10 次 getsize 才看表）。

        确定性假时钟（二期评审修复轮）：真实 wall-clock 下整个遍历可能落在
        同一 timer tick 内（所有预算检查都不触发）造成偶发失败——把 worker
        的 _time.time 换成递增假时钟后第 2 条必触发，语义不变。"""
        monkeypatch.setattr(rs, "_STATS_TIME_BUDGET", 0.0)
        monkeypatch.setattr(rs, "_BUDGET_CHECK_INTERVAL", 2)
        _tick = itertools.count()
        monkeypatch.setattr(
            rs, "_time", types.SimpleNamespace(time=lambda: next(_tick)))
        big = sandbox / "big"
        big.mkdir()
        for i in range(10):
            (big / f"f{i}.txt").write_text("x", encoding="utf-8")

        with rs._stats_lock:
            gen = rs._stats_generation
            rs._stats_active = 1  # 直接调 worker：补齐调度侧计数，结束时归零
        rs._stats_worker("big", str(big), gen)

        with rs._stats_lock:
            entry = rs._stats_cache.get("big")
        assert entry is not None
        assert entry["partial"] is True
        assert entry["file_count"] < 10


# ---------- I5：统计线程池并发上限与队列去重 ----------

class TestStatsPool:
    @pytest.fixture()
    def fake_threads(self, monkeypatch):
        """不真正启动 worker 的 Thread：记录创建参数（target/args/daemon）。"""
        spawned = []

        class _FakeThread:
            def __init__(self, target=None, args=(), daemon=None, **kw):
                self.target = target
                self.args = args
                spawned.append({"target": target, "args": args, "daemon": daemon})

            def start(self):
                pass

        monkeypatch.setattr(rs, "threading", types.SimpleNamespace(Thread=_FakeThread))
        return spawned

    def test_bounded_concurrency_and_queue(self, sandbox, fake_threads):
        """6 个目录首次列举：最多 _STATS_MAX_WORKERS 个 worker 并发，其余排队。"""
        for i in range(6):
            (sandbox / f"dir{i}").mkdir()
        _entries(sandbox)
        assert len(fake_threads) == rs._STATS_MAX_WORKERS  # 4 个立即开跑
        with rs._stats_lock:
            assert rs._stats_active == rs._STATS_MAX_WORKERS
            assert len(rs._stats_queue) == 6 - rs._STATS_MAX_WORKERS  # 2 个排队

    def test_same_path_deduplicated(self, sandbox, fake_threads):
        """同一路径重复触发不重复排队/开线程。"""
        (sandbox / "big").mkdir()
        assert rs._get_or_schedule_stats(str(sandbox), "big", str(sandbox / "big")) is None
        assert rs._get_or_schedule_stats(str(sandbox), "big", str(sandbox / "big")) is None
        assert len(fake_threads) == 1
        with rs._stats_lock:
            assert len(rs._stats_queue) == 0
            assert rs._stats_queued == {"big"}


# ---------- I6：invalidate 代际丢弃在途写回 ----------

class TestInvalidateGeneration:
    @pytest.fixture()
    def fake_threads(self, monkeypatch):
        spawned = []

        class _FakeThread:
            def __init__(self, target=None, args=(), daemon=None, **kw):
                self.target = target
                self.args = args
                spawned.append({"target": target, "args": args})

            def start(self):
                pass

        monkeypatch.setattr(rs, "threading", types.SimpleNamespace(Thread=_FakeThread))
        return spawned

    def test_stale_worker_write_dropped(self, sandbox, fake_threads):
        """在途 worker 完成后若代际已失效（clean_tmp/delete/move 触发），
        写回被丢弃；重新列举后以新代际重算并写入。"""
        (sandbox / "big").mkdir()
        (sandbox / "big" / "a.txt").write_text("x", encoding="utf-8")

        _entries(sandbox)  # 调度 big（代际 g0，fake 线程不跑）
        assert len(fake_threads) == 1
        old_worker = fake_threads[0]
        with rs._stats_lock:
            gen0 = rs._stats_generation

        rs._invalidate_stats()  # 模拟 clean_tmp/delete/move 的失效
        with rs._stats_lock:
            assert rs._stats_generation == gen0 + 1
            assert len(rs._stats_queue) == 0 and rs._stats_queued == set()

        # 旧代 worker 姗姗来迟：写回被丢弃
        old_worker["target"](*old_worker["args"])
        with rs._stats_lock:
            assert rs._stats_cache.get("big") is None

        # 重新列举：以新代际重算，完成后正常写回
        _entries(sandbox)
        assert len(fake_threads) == 2
        new_worker = fake_threads[1]
        new_worker["target"](*new_worker["args"])
        with rs._stats_lock:
            entry = rs._stats_cache.get("big")
        assert entry is not None and entry["file_count"] == 1


# ---------- Minor #7：sandbox root 变更缓存失效 ----------

class TestRootChangeInvalidates:
    def test_root_change_clears_cache(self, sandbox, tmp_path, monkeypatch):
        """运行中切换 sandbox_dir：旧根缓存（同 key）不得串到新根显示。"""
        (sandbox / "big").mkdir()
        with rs._stats_lock:
            rs._stats_root = str(sandbox)
            rs._stats_cache["big"] = {
                "size": 111, "file_count": 1, "partial": False,
                "ts": time.time(),
            }
        sb2 = tmp_path / "workspace2"
        sb2.mkdir()
        (sb2 / "big").mkdir()
        monkeypatch.setattr(rs, "load_config", lambda: {"sandbox_dir": str(sb2)})

        # 新根首次调度：旧根缓存整体失效，big 重新统计（返回 None）
        assert rs._get_or_schedule_stats(str(sb2), "big", str(sb2 / "big")) is None
        with rs._stats_lock:
            assert rs._stats_root == str(sb2)
            assert rs._stats_cache.get("big") is None
