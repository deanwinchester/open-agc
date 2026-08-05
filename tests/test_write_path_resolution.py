# -*- coding: utf-8 -*-
"""write_file/edit_file 相对路径解析回归：此前按进程 CWD（仓库根）解析，
agent 按「outputs/<主题>/」约定写产出落到仓库根 outputs/ 而非
workspace/outputs/（生产实证）。修复：sandbox_mode 开启时相对路径按
sandbox_dir 解析。"""
import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tools.filesystem import WriteFileTool, EditFileTool, _resolve_relative  # noqa: E402


@pytest.fixture()
def sandbox_env(tmp_path, monkeypatch):
    sb = tmp_path / "workspace"
    sb.mkdir()
    cfg = {"sandbox_mode": True, "sandbox_dir": str(sb)}
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr("core.paths.get_data_path", lambda f: str(cfg_file))
    # 故意把 CWD 放仓库根（模拟真实服务进程 CWD）
    monkeypatch.chdir(tmp_path)
    return sb


class TestResolveRelative:
    def test_relative_resolves_to_sandbox(self, sandbox_env):
        out = WriteFileTool().execute(path="outputs/方案.md", content="hi")
        assert "Successfully" in out
        assert (sandbox_env / "outputs" / "方案.md").is_file()
        # 不得写到 CWD（仓库根）下
        assert not os.path.exists(os.path.join(os.getcwd(), "outputs", "方案.md"))

    def test_absolute_path_unchanged(self, sandbox_env, tmp_path):
        # 解析函数对绝对路径原样返回（沙箱拦截是 check_sandbox 的职责，与本修复无关）
        target = str(tmp_path / "abs.md")
        assert _resolve_relative(target, {"sandbox_mode": True}) == target

    def test_edit_file_relative(self, sandbox_env):
        (sandbox_env / "a.txt").write_text("hello world", encoding="utf-8")
        out = EditFileTool().execute(path="a.txt", old_string="world",
                                     new_string="沙箱")
        assert "Error" not in out
        assert (sandbox_env / "a.txt").read_text(encoding="utf-8") == "hello 沙箱"

    def test_sandbox_mode_off_keeps_cwd(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"sandbox_mode": False}), encoding="utf-8")
        monkeypatch.setattr("core.paths.get_data_path", lambda f: str(cfg_file))
        monkeypatch.chdir(tmp_path)
        WriteFileTool().execute(path="rel_off.txt", content="x")
        assert (tmp_path / "rel_off.txt").is_file()
