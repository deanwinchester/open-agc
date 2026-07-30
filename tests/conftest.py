# -*- coding: utf-8 -*-
"""全局测试隔离：沙箱 Janitor（二期）的数据文件（pins/清单日志）重定向到
临时目录，避免 clean_tmp 等既有用例向真实 data/ 写清单。"""
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture(autouse=True)
def _janitor_data_dir(tmp_path, monkeypatch):
    import core.sandbox_janitor as sj
    data_dir = tmp_path / "janitor_data"
    data_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(sj, "get_data_path", lambda name: str(data_dir / name))
    yield
