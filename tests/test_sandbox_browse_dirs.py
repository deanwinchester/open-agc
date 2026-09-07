# -*- coding: utf-8 -*-
"""沙箱目录浏览接口（/api/sandbox/browse_dirs）回归测试——沙箱页可视化
选择目录用（非技术用户不熟路径），只读浏览：列子目录、给父目录、
报权限、目录不存在 400。"""
import os

import pytest

from api.routes.routes_sandbox import browse_dirs


@pytest.mark.anyio
async def test_browse_lists_subdirs(tmp_path):
    root = tmp_path / "browse_root"
    root.mkdir()
    (root / "dirA").mkdir()
    (root / "dirB").mkdir()
    (root / ".hidden").mkdir()          # 隐藏目录不展示
    (root / "file.txt").write_text("x")  # 文件不展示
    r = await browse_dirs(str(root))
    names = [d["name"] for d in r["dirs"]]
    assert names == ["dirA", "dirB"]
    assert r["path"] == os.path.abspath(str(root))
    assert r["parent"] == os.path.dirname(os.path.abspath(str(root)))
    assert r["writable"] is True


@pytest.mark.anyio
async def test_browse_defaults_to_home():
    r = await browse_dirs(None)
    assert r["path"] == os.path.abspath(os.path.expanduser("~"))


@pytest.mark.anyio
async def test_browse_missing_dir_400(tmp_path):
    with pytest.raises(Exception) as exc:
        await browse_dirs(str(tmp_path / "nonexistent"))
    assert "400" in str(exc.value)


@pytest.mark.anyio
async def test_browse_navigate_into(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "inner").mkdir()
    r = await browse_dirs(str(sub))
    assert [d["name"] for d in r["dirs"]] == ["inner"]
    assert r["parent"] == os.path.abspath(str(tmp_path))
