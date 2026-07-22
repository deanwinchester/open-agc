# -*- coding: utf-8 -*-
"""阶段 5 Task 3: 检索强化

- search_file_content: context_lines (-C 语义, rg 与 Python re 回退双路径)、
  output_mode (content/files_with_matches/count)、head_limit 截断标注
- read_file: offset/limit 分页、总行数/范围标注、越界处理、大文件分页建议
- list_dir: 深度/排序/show_size/沙箱拒绝
"""
import json
import os
import shutil
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tools.base import SandboxBlocked  # noqa: E402
from tools.filesystem import ListDirTool, ReadFileTool  # noqa: E402
from tools.search import GrepSearchTool  # noqa: E402

_HAS_RG = shutil.which("rg") is not None
requires_rg = pytest.mark.skipif(not _HAS_RG, reason="requires ripgrep")

A_LINES = [
    "line1 alpha",
    "line2 beta",
    "line3 MATCH_HERE",
    "line4 gamma",
    "line5 delta",
    "line6 epsilon",
    "line7 MATCH_HERE",
    "line8 zeta",
]


@pytest.fixture
def no_sandbox(monkeypatch, tmp_path):
    """Point get_data_path at a nonexistent config -> sandbox check is skipped."""
    monkeypatch.setattr("core.paths.get_data_path",
                        lambda name: str(tmp_path / "no_config" / name))


@pytest.fixture
def force_fallback(monkeypatch):
    """Force the Python re fallback path by hiding ripgrep."""
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)


@pytest.fixture
def tree(tmp_path):
    base = tmp_path / "tree"
    (base / "sub").mkdir(parents=True)
    (base / "a.txt").write_text("\n".join(A_LINES), encoding="utf-8")
    (base / "sub" / "b.py").write_text("x = 1\ny = MATCH_HERE\nz = 3\n", encoding="utf-8")
    return base


# ── search_file_content 新参数 ─────────────────────────────────────────────

class TestSearchSchema:
    def test_new_params_in_schema(self):
        fn = GrepSearchTool().get_openai_schema()["function"]
        props = fn["parameters"]["properties"]
        assert "context_lines" in props and props["context_lines"]["type"] == "integer"
        assert "output_mode" in props
        assert props["output_mode"]["enum"] == ["content", "files_with_matches", "count"]
        assert "head_limit" in props and props["head_limit"]["type"] == "integer"

    def test_invalid_output_mode(self, no_sandbox, tree):
        out = GrepSearchTool().execute(pattern="MATCH", path=str(tree), output_mode="bogus")
        assert out.startswith("Error") and "output_mode" in out


def _assert_ctx1_semantics(out, tree):
    """Shared -C 1 expectations for both rg and fallback paths."""
    a = str(tree / "a.txt")
    # match lines use ':', context lines use '-'
    assert f"{a}:3:line3 MATCH_HERE" in out
    assert f"{a}:7:line7 MATCH_HERE" in out
    assert f"{a}-2-line2 beta" in out
    assert f"{a}-4-line4 gamma" in out
    assert f"{a}-6-line6 epsilon" in out
    assert f"{a}-8-line8 zeta" in out
    # non-contiguous groups separated by '--'; line5 is NOT in any window
    assert "\n--\n" in out
    assert "line5 delta" not in out


@requires_rg
class TestSearchRgPath:
    def test_context_lines(self, no_sandbox, tree):
        out = GrepSearchTool().execute(pattern="MATCH_HERE", path=str(tree), context_lines=1)
        _assert_ctx1_semantics(out, tree)

    def test_no_context_by_default(self, no_sandbox, tree):
        out = GrepSearchTool().execute(pattern="MATCH_HERE", path=str(tree))
        assert "line2 beta" not in out
        assert ":3:line3 MATCH_HERE" in out
        assert "\n--\n" not in out

    def test_output_mode_files_with_matches(self, no_sandbox, tree):
        out = GrepSearchTool().execute(pattern="MATCH_HERE", path=str(tree),
                                       output_mode="files_with_matches")
        assert str(tree / "a.txt") in out
        assert str(tree / "sub" / "b.py") in out
        assert "MATCH_HERE" not in out.replace(str(tree), "")  # paths only, no content

    def test_output_mode_count(self, no_sandbox, tree):
        out = GrepSearchTool().execute(pattern="MATCH_HERE", path=str(tree),
                                       output_mode="count")
        lines = out.splitlines()
        assert any(l.endswith("a.txt:2") for l in lines)
        assert any(l.endswith("b.py:1") for l in lines)

    def test_head_limit_truncation(self, no_sandbox, tmp_path):
        big = tmp_path / "big.txt"
        big.write_text("\n".join(f"row{i} MATCH" for i in range(60)), encoding="utf-8")
        out = GrepSearchTool().execute(pattern="MATCH", path=str(big), head_limit=10)
        assert "Truncated" in out
        assert sum(1 for l in out.splitlines() if ":row" in l) == 10


class TestSearchFallbackPath:
    def test_context_lines(self, no_sandbox, force_fallback, tree):
        out = GrepSearchTool().execute(pattern="MATCH_HERE", path=str(tree), context_lines=1)
        _assert_ctx1_semantics(out, tree)

    def test_context_merges_adjacent_windows(self, no_sandbox, force_fallback, tmp_path):
        # matches at lines 2 and 4 with ctx=1 -> windows [1,3] and [3,5] merge (rg semantics)
        f = tmp_path / "m.txt"
        f.write_text("l1\nl2 MATCH\nl3\nl4 MATCH\nl5\n", encoding="utf-8")
        out = GrepSearchTool().execute(pattern="MATCH", path=str(f), context_lines=1)
        assert "--" not in out  # single merged group
        assert "-3-l3" in out   # shared context line appears once
        assert out.count("-3-l3") == 1

    def test_no_context_by_default(self, no_sandbox, force_fallback, tree):
        out = GrepSearchTool().execute(pattern="MATCH_HERE", path=str(tree))
        assert "line2 beta" not in out
        assert ":3:line3 MATCH_HERE" in out

    def test_output_mode_files_with_matches(self, no_sandbox, force_fallback, tree):
        out = GrepSearchTool().execute(pattern="MATCH_HERE", path=str(tree),
                                       output_mode="files_with_matches")
        assert str(tree / "a.txt") in out
        assert str(tree / "sub" / "b.py") in out
        assert "MATCH_HERE" not in out.replace(str(tree), "")

    def test_output_mode_count(self, no_sandbox, force_fallback, tree):
        out = GrepSearchTool().execute(pattern="MATCH_HERE", path=str(tree),
                                       output_mode="count")
        lines = out.splitlines()
        assert any(l.endswith("a.txt:2") for l in lines)
        assert any(l.endswith("b.py:1") for l in lines)

    def test_include_filter(self, no_sandbox, force_fallback, tree):
        out = GrepSearchTool().execute(pattern="MATCH_HERE", path=str(tree),
                                       include="*.py", output_mode="count")
        assert "b.py:1" in out
        assert "a.txt" not in out

    def test_head_limit_truncation_and_annotation(self, no_sandbox, force_fallback, tmp_path):
        big = tmp_path / "big.txt"
        big.write_text("\n".join(f"row{i} MATCH" for i in range(60)), encoding="utf-8")
        g = GrepSearchTool()
        out = g.execute(pattern="MATCH", path=str(big), head_limit=10)
        assert "Truncated" in out
        assert sum(1 for l in out.splitlines() if ":row" in l) == 10
        # default head_limit=50 also truncates 60 matches
        out2 = g.execute(pattern="MATCH", path=str(big))
        assert "Truncated" in out2
        assert sum(1 for l in out2.splitlines() if ":row" in l) == 50
        # raised head_limit shows everything without annotation
        out3 = g.execute(pattern="MATCH", path=str(big), head_limit=100)
        assert "Truncated" not in out3
        assert sum(1 for l in out3.splitlines() if ":row" in l) == 60

    def test_no_matches(self, no_sandbox, force_fallback, tree):
        out = GrepSearchTool().execute(pattern="ZZZ_NOPE", path=str(tree))
        assert "No matches found" in out


# ── read_file 分页 ─────────────────────────────────────────────────────────

class TestReadFilePaging:
    def test_schema_has_offset_limit(self):
        props = ReadFileTool().get_openai_schema()["function"]["parameters"]["properties"]
        assert "offset" in props and "limit" in props

    def test_default_whole_file_byte_compatible(self, no_sandbox, tree):
        path = str(tree / "a.txt")
        out = ReadFileTool().execute(path=path)
        expected = f"--- Content of {path} (Lines 1 to 8 of 8) ---\n" + "\n".join(
            f"{i:4d} | {line}" for i, line in enumerate(A_LINES, 1))
        assert out == expected

    def test_offset_limit_range_and_hint(self, no_sandbox, tree):
        out = ReadFileTool().execute(path=str(tree / "a.txt"), offset=3, limit=2)
        assert "(Lines 3 to 4 of 8)" in out
        assert "   3 | line3 MATCH_HERE" in out
        assert "   2 |" not in out
        # paging hint points at the next unread line
        assert "offset=5" in out and "继续读取" in out

    def test_limit_beyond_total(self, no_sandbox, tree):
        out = ReadFileTool().execute(path=str(tree / "a.txt"), offset=6, limit=100)
        assert "(Lines 6 to 8 of 8)" in out
        assert "继续读取" not in out  # reached EOF, no further-page hint

    def test_offset_beyond_eof(self, no_sandbox, tree):
        out = ReadFileTool().execute(path=str(tree / "a.txt"), offset=99)
        assert "超出文件末尾" in out and "8" in out

    def test_last_page_has_no_hint(self, no_sandbox, tree):
        out = ReadFileTool().execute(path=str(tree / "a.txt"), offset=7, limit=2)
        assert "(Lines 7 to 8 of 8)" in out
        assert "继续读取" not in out

    def test_huge_file_suggests_paging_but_returns_all(self, no_sandbox, tmp_path):
        big = tmp_path / "huge.txt"
        big.write_text("\n".join(f"line{i}" for i in range(1, 2101)), encoding="utf-8")
        out = ReadFileTool().execute(path=str(big))
        assert "建议用 offset/limit 分页" in out  # suggestion present
        assert "2100 | line2100" in out          # no silent truncation
        # with explicit pagination the suggestion disappears
        out2 = ReadFileTool().execute(path=str(big), offset=1, limit=50)
        assert "建议" not in out2
        assert "(Lines 1 to 50 of 2100)" in out2 and "offset=51" in out2


# ── list_dir ───────────────────────────────────────────────────────────────

class TestListDir:
    def test_schema(self):
        fn = ListDirTool().get_openai_schema()["function"]
        props = fn["parameters"]["properties"]
        assert fn["parameters"]["required"] == ["path"]
        assert props["depth"]["type"] == "integer"
        assert props["sort"]["enum"] == ["name", "mtime"]
        assert props["show_size"]["type"] == "boolean"

    def test_depth_1_hides_grandchildren(self, no_sandbox, tree):
        out = ListDirTool().execute(path=str(tree), depth=1)
        assert "sub/" in out and "a.txt" in out
        assert "b.py" not in out

    def test_depth_2_shows_grandchildren(self, no_sandbox, tree):
        out = ListDirTool().execute(path=str(tree), depth=2)
        assert "b.py" in out
        assert "depth=2" in out

    def test_depth_clamped_to_3(self, no_sandbox, tree):
        out = ListDirTool().execute(path=str(tree), depth=9)
        assert "depth=3" in out

    def test_sort_name_dirs_first(self, no_sandbox, tree):
        out = ListDirTool().execute(path=str(tree), sort="name")
        assert out.index("sub/") < out.index("a.txt")

    def test_sort_mtime_newest_first(self, no_sandbox, tmp_path):
        d = tmp_path / "mt"
        d.mkdir()
        old = d / "old.txt"
        new = d / "new.txt"
        old.write_text("o", encoding="utf-8")
        new.write_text("n", encoding="utf-8")
        os.utime(old, (1_000_000_000, 1_000_000_000))
        os.utime(new, (1_700_000_000, 1_700_000_000))
        out = ListDirTool().execute(path=str(d), sort="mtime")
        assert out.index("new.txt") < out.index("old.txt")

    def test_show_size_toggle(self, no_sandbox, tree):
        out = ListDirTool().execute(path=str(tree), show_size=True)
        a_line = next(l for l in out.splitlines() if "a.txt" in l)
        assert " B" in a_line  # human-readable size present
        out2 = ListDirTool().execute(path=str(tree), show_size=False)
        a_line2 = next(l for l in out2.splitlines() if "a.txt" in l)
        assert " B" not in a_line2
        assert "a.txt" in a_line2  # entry still listed

    def test_not_a_directory(self, no_sandbox, tree):
        out = ListDirTool().execute(path=str(tree / "a.txt"))
        assert out.startswith("Error")

    def test_sandbox_rejects_outside_path(self, monkeypatch, tmp_path):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"sandbox_mode": True, "sandbox_dir": str(allowed)}),
                       encoding="utf-8")
        monkeypatch.setattr("core.paths.get_data_path", lambda name: str(cfg))
        with pytest.raises(SandboxBlocked):
            ListDirTool().execute(path=str(outside))
        # inside the sandbox it works
        out = ListDirTool().execute(path=str(allowed))
        assert "entries" in out
