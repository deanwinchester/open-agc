# -*- coding: utf-8 -*-
"""resolve_sandbox_dir 回归测试：打包后沙箱目录不得落在 _internal。

此前沙箱默认 "./workspace" 按 CWD 解析，frozen 下 CWD=_MEIPASS
（/opt/open-agc/_internal，root 所有），agent 写文件必炸（生产实证）。
"""
import os
import sys

from core import paths
from core.paths import resolve_sandbox_dir


def test_absolute_passthrough(tmp_path):
    p = str(tmp_path / "abs_ws")
    assert resolve_sandbox_dir(p) == os.path.abspath(p)


def test_default_source_mode_is_project_root():
    # 非 frozen：解析到项目根目录下的 workspace
    root = os.path.dirname(os.path.dirname(os.path.abspath(paths.__file__)))
    assert resolve_sandbox_dir(None) == os.path.join(root, "workspace")
    assert resolve_sandbox_dir("./workspace") == os.path.join(root, "workspace")


def test_frozen_relative_goes_under_data_dir(tmp_path, monkeypatch):
    """frozen + 相对路径 → <base>/workspace（与 data 并列），绝不落在 _internal。"""
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setenv("OPEN_AGC_DATA_DIR", str(tmp_path / "base"))
    out = resolve_sandbox_dir("./workspace")
    assert out == os.path.join(str(tmp_path), "base", "workspace")
    assert "_internal" not in out
    assert f"{os.sep}data{os.sep}" not in out  # workspace 与 data 并列


def test_frozen_absolute_still_passthrough(tmp_path, monkeypatch):
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setenv("OPEN_AGC_DATA_DIR", str(tmp_path))
    p = os.path.abspath(str(tmp_path / "custom_ws"))
    assert resolve_sandbox_dir(p) == p
