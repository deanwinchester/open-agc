# -*- coding: utf-8 -*-
"""--pyrun 内部通道回归测试。

PyInstaller 冻结后 sys.executable 是 Open-AGC 本体而非 python 解释器，
execute_python 直接 Popen([sys.executable, script]) 会把整个 App 再拉起
一次（生产实证：agent 执行 python 代码反复开多个窗口，代码根本没跑）。
约定 [exe, --pyrun, script] 由 gui_app.main 用 runpy 执行后退出。
"""
import sys

import gui_app


def test_pyrun_executes_target_and_returns(tmp_path, monkeypatch, capsys):
    script = tmp_path / "agent_code.py"
    script.write_text("print('PYRUN_OK', 1 + 1)", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["Open-AGC", "--pyrun", str(script)])
    gui_app.main()  # 应执行脚本后直接返回，不进入 App 启动流程
    assert "PYRUN_OK 2" in capsys.readouterr().out


def test_pyrun_passes_extra_args(tmp_path, monkeypatch):
    script = tmp_path / "args_code.py"
    script.write_text(
        "import sys, pathlib\n"
        "pathlib.Path(sys.argv[1]).write_text('|'.join(sys.argv[2:]))\n",
        encoding="utf-8")
    out = tmp_path / "out.txt"
    monkeypatch.setattr(sys, "argv",
                        ["Open-AGC", "--pyrun", str(script), str(out), "a", "b"])
    gui_app.main()
    assert out.read_text() == "a|b"


def test_pyrun_tool_command_construction():
    """tools/python_repl 在 frozen 下必须走 --pyrun 通道。"""
    import inspect
    import tools.python_repl as repl
    src = inspect.getsource(repl)
    assert '--pyrun' in src
    assert "getattr(sys, 'frozen', False)" in src
