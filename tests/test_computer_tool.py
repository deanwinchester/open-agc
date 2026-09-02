"""tools.computer 的导入容错回归测试。

mouseinfo（pyautogui 依赖）在 Linux 无 tkinter 时会 sys.exit() 抛
SystemExit（继承 BaseException）。ComputerTool 若只 catch ImportError，
SystemExit 会穿透 ASGI 让整个 WebSocket 会话报错（UOS deb 实测）。
"""
import builtins

from tools.computer import ComputerTool


def test_init_survives_systemexit_from_pyautogui(monkeypatch):
    """pyautogui 导入抛 SystemExit 时，ComputerTool 初始化不应抛出。"""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == 'pyautogui':
            raise SystemExit('NOTE: You must install tkinter on Linux')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', fake_import)
    ComputerTool()  # 不应抛 SystemExit


def test_init_survives_importerror(monkeypatch):
    """pyautogui 未安装（ImportError）时，ComputerTool 初始化不应抛出。"""
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == 'pyautogui':
            raise ImportError('No module named pyautogui')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', fake_import)
    ComputerTool()  # 不应抛 ImportError
