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


def _w(title, hwnd=1, minimized=False):
    return {"hwnd": hwnd, "title": title, "rect": (0, 0, 100, 100),
            "minimized": minimized, "exe": "x.exe"}


class TestMatchWindow:
    """_match_window：完全相同 > 前缀 > 子串；同级取列表靠前（Z-order）。"""

    def test_exact_beats_substring(self):
        wins = [_w("微信（工作机）", 1), _w("微信", 2)]
        assert ComputerTool._match_window(wins, "微信")["hwnd"] == 2

    def test_prefix_beats_substring(self):
        wins = [_w("笔记-微信收藏", 1), _w("微信（工作机）", 2)]
        assert ComputerTool._match_window(wins, "微信")["hwnd"] == 2

    def test_substring_match(self):
        wins = [_w("GitLab - Pipelines", 1), _w("某品牌窗口", 2)]
        assert ComputerTool._match_window(wins, "pipeline")["hwnd"] == 1

    def test_case_insensitive(self):
        wins = [_w("WeChat", 1)]
        assert ComputerTool._match_window(wins, "wechat")["hwnd"] == 1

    def test_zorder_first_among_same_level(self):
        wins = [_w("窗口A-设置", 1), _w("窗口B-设置", 2)]
        assert ComputerTool._match_window(wins, "设置")["hwnd"] == 1

    def test_no_match_returns_none(self):
        assert ComputerTool._match_window([_w("A")], "不存在") is None

    def test_empty_query_returns_none(self):
        assert ComputerTool._match_window([_w("A")], "") is None
        assert ComputerTool._match_window([_w("A")], None) is None


class TestEnumWindowsX11:
    """Linux 窗口枚举：wmctrl 优先、未装时回落 python-xlib。"""

    def test_wmctrl_parsing(self, monkeypatch):
        import subprocess as _sp
        sample = (
            "0x03e00003  0 100 200 1280 800 uos-host 微信\n"
            "0x01a00007  0 0 0 1920 1080 uos-host Google Chrome - GitLab\n"
        )

        class R:
            returncode = 0
            stdout = sample

        monkeypatch.setattr(_sp, "run", lambda *a, **k: R())
        wins = ComputerTool._enum_windows_x11()
        assert len(wins) == 2
        assert wins[0]["title"] == "微信"
        assert wins[0]["rect"] == (100, 200, 1380, 1000)
        assert wins[1]["title"] == "Google Chrome - GitLab"

    def test_fallback_to_xlib_when_wmctrl_missing(self, monkeypatch):
        import subprocess as _sp

        def no_wmctrl(*a, **k):
            raise FileNotFoundError("wmctrl not found")

        monkeypatch.setattr(_sp, "run", no_wmctrl)
        sentinel = [{"hwnd": 1, "title": "x", "rect": (0, 0, 1, 1),
                     "minimized": False, "exe": ""}]
        monkeypatch.setattr(ComputerTool, "_enum_windows_xlib",
                            staticmethod(lambda: sentinel))
        assert ComputerTool._enum_windows_x11() == sentinel

    def test_fallback_to_xlib_when_wmctrl_errors(self, monkeypatch):
        import subprocess as _sp

        class R:
            returncode = 1
            stdout = ""

        monkeypatch.setattr(_sp, "run", lambda *a, **k: R())
        sentinel = [{"hwnd": 2, "title": "y", "rect": (0, 0, 1, 1),
                     "minimized": False, "exe": ""}]
        monkeypatch.setattr(ComputerTool, "_enum_windows_xlib",
                            staticmethod(lambda: sentinel))
        assert ComputerTool._enum_windows_x11() == sentinel

