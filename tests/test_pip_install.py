# -*- coding: utf-8 -*-
"""pip_install 工具与内嵌优先 python 环境链路测试。"""
import os
import subprocess
import sys
from unittest import mock

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class TestPipInstallValidation:
    def test_rejects_bad_package_names(self):
        from tools.pip_install import PipInstallTool
        t = PipInstallTool()
        for bad in ["", "rm -rf /", "pkg;echo hi", "pkg$(whoami)", "../x"]:
            out = t.execute(package=bad)
            assert out.startswith("Error: 非法包名"), (bad, out)

    def test_accepts_version_constraints(self):
        from tools.pip_install import _PKG_RE
        for good in ["requests", "openpyxl>=3.1", "flask==2.0", "uvicorn[standard]"]:
            assert _PKG_RE.match(good), good


class TestPipInstallRun:
    def test_success_path_uses_target_and_index(self, tmp_path, monkeypatch):
        from tools import pip_install as pi
        monkeypatch.setattr(pi, "pylibs_dir", lambda: str(tmp_path))
        calls = {}

        def fake_run(cmd, **kw):
            calls["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, stdout="Successfully installed six-1.16.0", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        out = pi.PipInstallTool().execute(package="six")
        assert "安装成功" in out
        cmd = calls["cmd"]
        assert "--target" in cmd and str(tmp_path) in cmd
        assert "six" in cmd[-1]

    def test_failure_returns_tail(self, tmp_path, monkeypatch):
        from tools import pip_install as pi
        monkeypatch.setattr(pi, "pylibs_dir", lambda: str(tmp_path))
        monkeypatch.setattr(subprocess, "run",
                            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom"))
        out = pi.PipInstallTool().execute(package="six")
        assert "失败" in out and "boom" in out


class TestEmbeddedFirstResolution:
    """内嵌优先（用户拍板）：auto 模式不再优先真机 Python。"""

    def test_auto_mode_prefers_embedded(self, monkeypatch):
        from tools import python_repl
        python_repl._PY_CACHE.clear()
        tool = python_repl.PythonREPLTool()
        monkeypatch.setattr(python_repl.PythonREPLTool, "_find_system_python",
                            staticmethod(lambda: ["/usr/bin/python3"]))
        cmd, desc, src = tool._resolve_python()
        assert src == "embedded", (cmd, desc, src)
        python_repl._PY_CACHE.clear()


class TestModuleNotFoundHint:
    def test_hint_appended_on_missing_module(self):
        from tools.python_repl import PythonREPLTool
        out = PythonREPLTool().execute(code="import definitely_missing_mod_xyz\n")
        assert "ModuleNotFoundError" in out
        assert "pip_install" in out, out[-300:]

    def test_no_hint_on_success(self):
        from tools.python_repl import PythonREPLTool
        out = PythonREPLTool().execute(code="print('hi')\n")
        assert "缺包提示" not in out


class TestPyrunModuleChannel:
    """gui_app --pyrun -m <module>：模块通道（pip 等）。"""

    def test_module_channel_runs(self, tmp_path):
        (tmp_path / "hello_mod.py").write_text("print('MODULE_OK')\n", encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(tmp_path)
        r = subprocess.run(
            [sys.executable, os.path.join(PROJECT_ROOT, "gui_app.py"),
             "--pyrun", "-m", "hello_mod"],
            capture_output=True, text=True, timeout=60, env=env)
        assert r.returncode == 0, r.stderr[-300:]
        assert "MODULE_OK" in r.stdout

    def test_module_channel_error_guard(self, tmp_path):
        (tmp_path / "bad_mod.py").write_text("import nope_missing_xyz\n", encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(tmp_path)
        r = subprocess.run(
            [sys.executable, os.path.join(PROJECT_ROOT, "gui_app.py"),
             "--pyrun", "-m", "bad_mod"],
            capture_output=True, text=True, timeout=60, env=env)
        assert r.returncode == 1
        assert "ModuleNotFoundError" in r.stderr


class TestRealInstallChain:
    """真实链路：pip_install 装 six 到临时扩展库 → 同 PYTHONPATH 下可 import。"""

    @pytest.mark.skipif(os.environ.get("AGC_SKIP_NET_TESTS") == "1",
                        reason="离线环境跳过真实安装")
    def test_install_and_import(self, tmp_path, monkeypatch):
        from tools import pip_install as pi
        monkeypatch.setattr(pi, "pylibs_dir", lambda: str(tmp_path))
        out = pi.PipInstallTool().execute(package="six")
        if out.startswith("Error"):
            pytest.skip("网络不可达，跳过真实安装: " + out[:120])
        env = os.environ.copy()
        env["PYTHONPATH"] = str(tmp_path)
        r = subprocess.run([sys.executable, "-c", "import six; print('SIX_OK', six.__version__)"],
                           capture_output=True, text=True, timeout=60, env=env)
        assert r.returncode == 0 and "SIX_OK" in r.stdout
