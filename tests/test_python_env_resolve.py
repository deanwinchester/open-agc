# -*- coding: utf-8 -*-
"""execute_python 解释器解析（真机优先/嵌入兜底）回归测试。

python_env=auto（默认）：真机 PATH 上第一个 >=3.10 的 python 优先，找不到
回退嵌入解释器（frozen=--pyrun，源码=当前解释器）；system 仅真机，找不到
明确报错；embedded 仅嵌入。agent 需被告知运行环境（schema 描述 + 返回行）。
"""
import sys

import pytest

from tools.python_repl import PythonREPLTool


@pytest.fixture(autouse=True)
def _reset_cache():
    from tools.python_repl import _PY_CACHE
    _PY_CACHE.clear()
    yield
    _PY_CACHE.clear()


def _no_system_python(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)


def _fake_system_python(monkeypatch, exe="/usr/bin/python3.11"):
    monkeypatch.setattr("shutil.which", lambda name: exe if name == "python3.11" else None)

    class _R:
        stdout = "3.11\n"
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _R())


def _set_mode(monkeypatch, mode):
    monkeypatch.setattr("json.load", lambda f: {"python_env": mode})


def test_auto_prefers_system_python(monkeypatch):
    _set_mode(monkeypatch, "auto")
    _fake_system_python(monkeypatch)
    cmd, info, src = PythonREPLTool()._resolve_python()
    assert src == "system"
    assert cmd == ["/usr/bin/python3.11"]
    assert "真机" in info


def test_auto_falls_back_to_embedded(monkeypatch):
    _set_mode(monkeypatch, "auto")
    _no_system_python(monkeypatch)
    cmd, info, src = PythonREPLTool()._resolve_python()
    assert src == "embedded"
    if getattr(sys, "frozen", False):
        assert cmd[1] == "--pyrun"
    else:
        assert cmd == [sys.executable]


def test_system_mode_missing_errors(monkeypatch):
    _set_mode(monkeypatch, "system")
    _no_system_python(monkeypatch)
    cmd, info, src = PythonREPLTool()._resolve_python()
    assert src == "missing"
    assert cmd is None
    assert "没有 Python >= 3.10" in info


def test_embedded_mode_ignores_system(monkeypatch):
    _set_mode(monkeypatch, "embedded")
    tool = PythonREPLTool()
    calls = []
    monkeypatch.setattr(tool, "_find_system_python",
                        lambda: calls.append(1) or ["/usr/bin/python3.11"])
    cmd, info, src = tool._resolve_python()
    assert src == "embedded"
    assert calls == []  # embedded 模式下不查找真机


def test_find_system_python_skips_old_versions(monkeypatch):
    """真机只有 Python 3.7 等老版本时不得使用（agent 代码多为现代语法）。"""
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/python3")

    class _R:
        stdout = "3.7\n"
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _R())
    assert PythonREPLTool._find_system_python() is None


def test_schema_description_includes_runtime(monkeypatch):
    _set_mode(monkeypatch, "auto")
    _fake_system_python(monkeypatch)
    schema = PythonREPLTool().get_openai_schema()
    assert "运行环境" in schema["function"]["description"]
    assert "真机" in schema["function"]["description"]
