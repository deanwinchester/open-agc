# -*- coding: utf-8 -*-
"""阶段5 Task4: edit_file 增强 + apply_patch 工具测试。

edit_file 矩阵: 唯一替换 / 多处拒绝(行号+上下文) / replace_all / 未找到(相近行提示) / 空串 / UTF-8 中文
apply_patch: 全部成功 / 部分失败报告正确性 / 顺序应用语义 / 沙箱拒绝其中一路径 / 缺失文件块
"""
import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tools.base import SandboxBlocked
from tools.filesystem import ApplyPatchTool, EditFileTool


@pytest.fixture
def workdir(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return d


@pytest.fixture
def wl(workdir):
    """Session whitelist so sandbox (data/config.json) allows the tmp workdir."""
    return {str(workdir)}


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ───────────────────────── edit_file 矩阵 ─────────────────────────

class TestEditFile:
    def test_unique_replace(self, workdir, wl):
        f = str(workdir / "a.txt")
        _write(f, "hello\nworld\n")
        r = EditFileTool().execute(path=f, old_string="world", new_string="世界",
                                   _session_whitelist=wl)
        assert r.startswith("Successfully edited")
        assert _read(f) == "hello\n世界\n"

    def test_multi_match_rejected_with_line_context(self, workdir, wl):
        f = str(workdir / "m.txt")
        _write(f, "head\nfoo = 1\nmid\nfoo = 1\ntail\nfoo = 1\n")
        r = EditFileTool().execute(path=f, old_string="foo = 1", new_string="foo = 9",
                                   _session_whitelist=wl)
        assert r.startswith("Error")
        assert "出现 3 次" in r
        # 每个匹配的行号 + 1 行上下文，供模型消歧
        assert "L2: foo = 1" in r
        assert "L4: foo = 1" in r
        assert "L6: foo = 1" in r
        assert "replace_all=true" in r
        assert _read(f) == "head\nfoo = 1\nmid\nfoo = 1\ntail\nfoo = 1\n"  # 未改动

    def test_replace_all(self, workdir, wl):
        f = str(workdir / "r.txt")
        _write(f, "x=1\ny\nx=1\nz\nx=1\n")
        r = EditFileTool().execute(path=f, old_string="x=1", new_string="x=2",
                                   replace_all=True, _session_whitelist=wl)
        assert "Replaced 3 occurrences" in r
        assert _read(f) == "x=2\ny\nx=2\nz\nx=2\n"

    def test_replace_all_string_true(self, workdir, wl):
        """模型有时把布尔传成字符串，"true" 应按 True 处理。"""
        f = str(workdir / "rs.txt")
        _write(f, "a=1\na=1\n")
        r = EditFileTool().execute(path=f, old_string="a=1", new_string="a=2",
                                   replace_all="true", _session_whitelist=wl)
        assert "Replaced 2 occurrences" in r
        assert _read(f) == "a=2\na=2\n"

    def test_replace_all_string_false_rejected(self, workdir, wl):
        """字符串 "false" 必须按 False 处理：多处匹配仍拒绝（原实现会误判为真）。"""
        f = str(workdir / "rsf.txt")
        _write(f, "a=1\na=1\n")
        r = EditFileTool().execute(path=f, old_string="a=1", new_string="a=2",
                                   replace_all="false", _session_whitelist=wl)
        assert r.startswith("Error")
        assert "出现 2 次" in r
        assert _read(f) == "a=1\na=1\n"  # 未被全部替换

    def test_multi_match_context_disambiguates(self, workdir, wl):
        """完全相同的多处匹配：错误含各自前后 1 行上下文，可据此消歧。"""
        f = str(workdir / "dup.txt")
        _write(f, "aaa\nfoo = 1\nbbb\nmid\nccc\nfoo = 1\nddd\n")
        r = EditFileTool().execute(path=f, old_string="foo = 1", new_string="x",
                                   _session_whitelist=wl)
        assert r.startswith("Error")
        assert "出现 2 次" in r
        # 匹配 1（L2）的前/后行
        assert "L1: aaa" in r and "> L2: foo = 1" in r and "L3: bbb" in r
        # 匹配 2（L6）的前/后行
        assert "L5: ccc" in r and "> L6: foo = 1" in r and "L7: ddd" in r
        assert _read(f) == "aaa\nfoo = 1\nbbb\nmid\nccc\nfoo = 1\nddd\n"

    def test_multi_match_multiline_range_covered(self, workdir, wl):
        """多行 old_string：错误完整覆盖其行范围，并含前后各 1 行。"""
        f = str(workdir / "ml.txt")
        _write(f, "p0\nA\nB\np1\nq0\nA\nB\nq1\n")
        r = EditFileTool().execute(path=f, old_string="A\nB", new_string="X\nY",
                                   _session_whitelist=wl)
        assert "出现 2 次" in r
        # 匹配 1 覆盖 L2-L3，前后文 L1/L4
        assert "L1: p0" in r and "> L2: A" in r and "> L3: B" in r and "L4: p1" in r
        # 匹配 2 覆盖 L6-L7，前后文 L5/L8
        assert "L5: q0" in r and "> L6: A" in r and "> L7: B" in r and "L8: q1" in r
        assert _read(f) == "p0\nA\nB\np1\nq0\nA\nB\nq1\n"

    def test_not_found_hint_with_near_lines(self, workdir, wl):
        f = str(workdir / "n.txt")
        _write(f, "foo = 1\nbar = 2\n")
        r = EditFileTool().execute(path=f, old_string="foo = 2", new_string="x",
                                   _session_whitelist=wl)
        assert r.startswith("Error")
        assert "未找到" in r
        assert "read_file" in r  # 建议先读文件确认
        assert "相近行" in r and "L1: foo = 1" in r  # 相近行提示
        assert _read(f) == "foo = 1\nbar = 2\n"

    def test_empty_old_string(self, workdir, wl):
        f = str(workdir / "e.txt")
        _write(f, "content\n")
        r = EditFileTool().execute(path=f, old_string="", new_string="x",
                                   _session_whitelist=wl)
        assert r.startswith("Error")
        assert "不能为空" in r
        assert "write_file" in r  # 提示替代方案
        assert _read(f) == "content\n"

    def test_utf8_chinese(self, workdir, wl):
        f = str(workdir / "中文.txt")
        _write(f, "# 配置文件\n作者 = 张三\n# 结束\n")
        r = EditFileTool().execute(path=f, old_string="作者 = 张三", new_string="作者 = 李四",
                                   _session_whitelist=wl)
        assert r.startswith("Successfully edited")
        assert _read(f) == "# 配置文件\n作者 = 李四\n# 结束\n"

    def test_missing_file(self, workdir, wl):
        r = EditFileTool().execute(path=str(workdir / "nope.txt"), old_string="a",
                                   new_string="b", _session_whitelist=wl)
        assert "does not exist" in r


# ───────────────────────── apply_patch ─────────────────────────

class TestApplyPatch:
    def test_schema_structure(self):
        s = ApplyPatchTool().get_openai_schema()
        fn = s["function"]
        assert fn["name"] == "apply_patch"
        props = fn["parameters"]["properties"]
        assert "patches" in props
        assert fn["parameters"]["required"] == ["patches"]
        item = props["patches"]["items"]
        assert item["required"] == ["path", "edits"]
        edit_item = item["properties"]["edits"]["items"]
        assert edit_item["required"] == ["old_string", "new_string"]
        assert "replace_all" in edit_item["properties"]

    def test_all_success(self, workdir, wl):
        fa = str(workdir / "a.py")
        fb = str(workdir / "b.py")
        _write(fa, "import os\nX = 1\n")
        _write(fb, "def f():\n    return 1\n")
        r = ApplyPatchTool().execute(patches=[
            {"path": fa, "edits": [
                {"old_string": "import os", "new_string": "import sys"},
                {"old_string": "X = 1", "new_string": "X = 2"},
            ]},
            {"path": fb, "edits": [
                {"old_string": "return 1", "new_string": "return 2"},
            ]},
        ], _session_whitelist=wl)
        assert r.count("OK") == 3
        assert "Summary: 3/3 处编辑已应用，0 处失败" in r
        assert "全部成功" in r
        assert _read(fa) == "import sys\nX = 2\n"
        assert _read(fb) == "def f():\n    return 2\n"

    def test_partial_failure_report(self, workdir, wl):
        fa = str(workdir / "a.txt")
        fb = str(workdir / "b.txt")
        _write(fa, "alpha\nbeta\n")
        _write(fb, "one\ntwo\n")
        r = ApplyPatchTool().execute(patches=[
            {"path": fa, "edits": [
                {"old_string": "alpha", "new_string": "ALPHA"},
                {"old_string": "不存在的内容", "new_string": "x"},
            ]},
            {"path": fb, "edits": [
                {"old_string": "two", "new_string": "TWO"},
            ]},
        ], _session_whitelist=wl)
        # 逐块报告: 每条编辑一行 ok/error + 原因
        assert f"[1.1] {fa}: OK" in r
        assert f"[1.2] {fa}: ERROR" in r and "未找到" in r
        assert f"[2.1] {fb}: OK" in r
        # 末尾汇总行
        assert "Summary: 2/3 处编辑已应用，1 处失败" in r
        assert f"{fa}#2" in r  # 失败项明确标注
        # 顺序应用语义: 成功的编辑已落盘（无静默部分提交——全部明示）
        assert _read(fa) == "ALPHA\nbeta\n"
        assert _read(fb) == "one\nTWO\n"

    def test_sequential_edits_depend_on_order(self, workdir, wl):
        """同一文件内后续编辑作用于前面编辑的结果。"""
        f = str(workdir / "seq.txt")
        _write(f, "A\n")
        r = ApplyPatchTool().execute(patches=[
            {"path": f, "edits": [
                {"old_string": "A", "new_string": "B"},
                {"old_string": "B", "new_string": "C"},  # 依赖上一步的输出
            ]},
        ], _session_whitelist=wl)
        assert "Summary: 2/2" in r
        assert _read(f) == "C\n"

    def test_replace_all_inside_patch(self, workdir, wl):
        f = str(workdir / "ra.txt")
        _write(f, "k=1\nk=1\n")
        r = ApplyPatchTool().execute(patches=[
            {"path": f, "edits": [
                {"old_string": "k=1", "new_string": "k=2", "replace_all": True},
            ]},
        ], _session_whitelist=wl)
        assert "共 2 处" in r
        assert _read(f) == "k=2\nk=2\n"

    def test_multi_match_without_replace_all_fails_that_edit_only(self, workdir, wl):
        f = str(workdir / "mm.txt")
        _write(f, "v=1\nv=1\nuniq\n")
        r = ApplyPatchTool().execute(patches=[
            {"path": f, "edits": [
                {"old_string": "v=1", "new_string": "v=2"},          # 多处 → 失败
                {"old_string": "uniq", "new_string": "UNIQ"},          # 仍继续应用
            ]},
        ], _session_whitelist=wl)
        assert "[1.1]" in r and "出现 2 次" in r
        assert f"[1.2] {f}: OK" in r
        assert _read(f) == "v=1\nv=1\nUNIQ\n"

    def test_nested_replace_all_string_false_rejected(self, workdir, wl):
        """patch 嵌套 replace_all 的字符串 "false" 同样按 False 处理：多处匹配拒绝。"""
        f = str(workdir / "nrf.txt")
        _write(f, "w=1\nw=1\nuniq\n")
        r = ApplyPatchTool().execute(patches=[
            {"path": f, "edits": [
                {"old_string": "w=1", "new_string": "w=2", "replace_all": "false"},  # → 失败
                {"old_string": "uniq", "new_string": "UNIQ"},                         # 仍应用
            ]},
        ], _session_whitelist=wl)
        assert "[1.1]" in r and "出现 2 次" in r
        assert f"[1.2] {f}: OK" in r
        assert "Summary: 1/2 处编辑已应用，1 处失败" in r
        assert _read(f) == "w=1\nw=1\nUNIQ\n"

    def test_missing_file_block(self, workdir, wl):
        ok = str(workdir / "ok.txt")
        _write(ok, "before\n")
        r = ApplyPatchTool().execute(patches=[
            {"path": str(workdir / "ghost.txt"), "edits": [
                {"old_string": "a", "new_string": "b"},
            ]},
            {"path": ok, "edits": [
                {"old_string": "before", "new_string": "after"},
            ]},
        ], _session_whitelist=wl)
        assert "文件不存在" in r
        assert f"[2.1] {ok}: OK" in r
        assert "Summary: 1/2 处编辑已应用，1 处失败" in r
        assert _read(ok) == "after\n"

    def test_invalid_patches_arg(self, workdir, wl):
        tool = ApplyPatchTool()
        assert "Error" in tool.execute(patches=None, _session_whitelist=wl)
        assert "Error" in tool.execute(patches=[], _session_whitelist=wl)
        assert "Error" in tool.execute(patches="not json{", _session_whitelist=wl)
        # 容错: JSON 字符串形式也接受
        f = str(workdir / "j.txt")
        _write(f, "q\n")
        r = tool.execute(patches=json.dumps(
            [{"path": f, "edits": [{"old_string": "q", "new_string": "Q"}]}],
            ensure_ascii=False), _session_whitelist=wl)
        assert "Summary: 1/1" in r
        assert _read(f) == "Q\n"

    def test_sandbox_denies_one_path_nothing_applied(self, workdir, tmp_path, monkeypatch):
        """沙箱拒绝其中一路径 → 抛 SandboxBlocked，且任何文件都未被修改（路径预检先于应用）。"""
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"sandbox_mode": True, "sandbox_dir": str(workdir)}),
                       encoding="utf-8")
        monkeypatch.setattr("core.paths.get_data_path", lambda name: str(cfg))

        inside = str(workdir / "inside.txt")
        outside = str(tmp_path / "outside.txt")  # 在 sandbox_dir 之外
        _write(inside, "old inside\n")
        _write(outside, "old outside\n")

        with pytest.raises(SandboxBlocked):
            ApplyPatchTool().execute(patches=[
                {"path": inside, "edits": [{"old_string": "old inside", "new_string": "new inside"}]},
                {"path": outside, "edits": [{"old_string": "old outside", "new_string": "new outside"}]},
            ])
        # 预检语义: 一处都未应用
        assert _read(inside) == "old inside\n"
        assert _read(outside) == "old outside\n"

    def test_sandbox_allows_whitelisted(self, workdir, tmp_path, monkeypatch):
        """同一沙箱配置下，sandbox_dir 内的路径正常应用。"""
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"sandbox_mode": True, "sandbox_dir": str(workdir)}),
                       encoding="utf-8")
        monkeypatch.setattr("core.paths.get_data_path", lambda name: str(cfg))

        inside = str(workdir / "in.txt")
        _write(inside, "a\n")
        r = ApplyPatchTool().execute(patches=[
            {"path": inside, "edits": [{"old_string": "a", "new_string": "b"}]},
        ])
        assert "Summary: 1/1" in r
        assert _read(inside) == "b\n"
