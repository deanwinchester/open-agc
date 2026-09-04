# -*- coding: utf-8 -*-
"""routes_plugins 用户插件目录解析回归测试。

此前 _user_plugins_dir 在模块 import 期用 __file__ 相对路径计算并
makedirs——frozen 下 __file__ 位于 /opt/open-agc/_internal（root 所有），
全新机器首启 import 即 Permission denied，服务整体起不来（生产实证）。
修复后走 core.paths.get_data_dir()（遵循 OPEN_AGC_DATA_DIR）。
"""
import os

from api.routes.routes_plugins import _get_user_plugins_dir


def test_user_plugins_dir_honors_data_dir_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_AGC_DATA_DIR", str(tmp_path))
    d = _get_user_plugins_dir()
    assert d.startswith(str(tmp_path))
    assert os.path.isdir(d)  # 已自动创建
    assert "_internal" not in d


def test_all_plugin_dirs_uses_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_AGC_DATA_DIR", str(tmp_path))
    from api.routes.routes_plugins import _all_plugin_dirs
    dirs = _all_plugin_dirs()
    assert len(dirs) == 1
    assert dirs[0].startswith(str(tmp_path))
