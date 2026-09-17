import os
import time
import threading
from typing import Any, Dict, Optional
from pydantic import Field

from tools.base import BaseTool

# 坐标定位规程：computer_control 被唤醒时随检索结果一并注入（模型对工具
# 结果读得最认真，且正好是它即将开始操控的时刻）。弱 grounding 模型靠
# 自由发挥估像素会偏差数百像素（生产实证），显式规程把它变成读数题。
GROUNDING_GUIDE = """
--- 电脑操控坐标定位规程（必读，按此执行）---
点击坐标禁止凭感觉估计，按以下优先级定位：
0a. 【文字锚点】：截图结果里的「文字锚点(全图坐标)」清单（OCR 精确坐标）——
    目标带文字（按钮/输入框/联系人名）时直接从中取坐标点击，最可靠。
0b. 【locate 定位服务】：无文字的图标/图形目标，用 locate 动作（target 描述 +
    click=true）让专用定位模型给坐标并直接点击，一次完成。
0c. 都没有再读网格估坐标。
1. 先全图截图，看清红色网格：四条边上的红色数字就是坐标刻度。
2. 定位目标在图上位于【哪两条竖线】与【哪两条横线】之间，先回答这个问题。
3. 再按格内比例读出坐标（如目标在 x=400 与 x=500 线之间偏右约 60%，则 x≈460）。
4. 目标小于 60px（任务栏图标/小按钮）时：先 region 放大该区域确认，放大图
   里的网格数字直接就是全图坐标，读数点击即可（无需手动换算）。
5. 精准点击流程（弱模型为系统强制，禁止跳步）：mouse_move 移到目标 →
   截图看红点（鼠标标记，标注数字是全图坐标）是否对准目标 → 对准了才
   mouse_click；没对准就按差值再移一次再验证。悬停验证模式下直接点击
   会被工具拒绝并提示该流程。
6. 点击后必须重新截图验证状态变化；没有变化就重新读网格定位，禁止原坐标
   重复盲试。
7. 多窗口重叠时先看截图结果里的「当前前台窗口」——目标不在前台就用
   activate_window 切换（窗口标题关键词，如「微信」），系统直接把它带到
   前台，比点任务栏图标可靠得多（不需要像素定位）。不知道有哪些窗口时
   先 list_windows。
8. 操作特定应用时：activate_window 切到前台后，用 screenshot+window 参数
   只截该窗口（目标在画面里占比更大、定位更准）；找任务栏图标用
   screenshot+window="taskbar" 只截任务栏条带。
9. 检查应用是否在运行：Windows 进程名常与品牌名不同（微信=WeChat.exe），
   tasklist 后用 findstr 过滤（cmd 没有 grep）。
"""

# 工具共享状态（模块级——绝不能放 BaseModel 类属性：pydantic v2 会把
# 下划线开头的类属性全部转成 ModelPrivateAttr 描述符，未赋值首次使用即崩
# 「cannot unpack ModelPrivateAttr」，生产实证）
_STATE = {
    "scale": 1.0,          # 缩放比（saved_px / real_px）
    "view_size": (0, 0),   # 最近全图视图尺寸（点击越界检查；region 不更新）
    "action": "",          # 最近一次动作（mouse_move/mouse_click）
    "moved": False,        # 本次点击前是否已 mouse_move（严格模式用）
    "seen": False,         # mouse_move 后是否已截图确认（严格模式用）
}


def _strict_mode(agent) -> bool:
    """弱模型强制悬停验证模式。

    config.json:
      computer_strict_mode: "auto"(默认) | "always" | "never"
      computer_strict_models: 弱模型名关键词列表（auto 时按包含匹配，
        默认 ["qwen", "llamacpp/", "ollama/"]）——k3/GPT-4o 等强模型
        不在列表内，自由操作不受限。
    """
    try:
        import json as _json
        from core.paths import get_data_path
        cfg_path = get_data_path("config.json")
        cfg = {}
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = _json.load(f)
        mode = str(cfg.get("computer_strict_mode", "auto")).lower()
        if mode == "always":
            return True
        if mode == "never":
            return False
        # auto：按模型名匹配弱模型列表
        model = (getattr(agent, "model", "") or "").lower() if agent else ""
        if not model:
            return False
        keys = cfg.get("computer_strict_models") or ["qwen", "llamacpp/", "ollama/"]
        return any(str(k).lower() in model for k in keys)
    except Exception:
        return False


class ComputerTool(BaseTool):
    name: str = "computer_control"
    description: str = ("物理操控本机鼠标和键盘（点击、移动、输入、按键、截图、"
                        "窗口切换）。browser_automation 等工具无法完成的 GUI 操作才用它。"
                        "切换窗口用 activate_window（标题关键词直达前台，比点任务栏"
                        "图标可靠）；操作特定应用时 screenshot+window 只截该窗口，"
                        "找任务栏图标用 screenshot+window=\"taskbar\"。"
                        "坐标一律按最近一张【全图截图】的像素坐标输入（工具自动按"
                        "缩放比换算回真实屏幕，无需自己换算）；region/window 放大截图仅"
                        "用于观察细节，不改变点击坐标系。"
                        "输入中文等非 ASCII 文本用 paste_text（剪贴板粘贴），"
                        "type_text 仅适合纯 ASCII。")

    def __init__(self, **data):
        super().__init__(**data)
        # Import pyautogui lazily to avoid issues if not installed or running headlessly
        global pyautogui
        try:
            # Linux 下 mouseinfo 在 import 时即连接 X（os.environ['DISPLAY']，
            # 缺失直接 KeyError 崩掉 import）——先探测可用的 display 再导入。
            # UOS 桌面 X 常在 :1 而非 :0（生产实证）。
            import sys as _sys
            if _sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
                try:
                    os.environ["DISPLAY"] = ComputerTool._pick_linux_display()
                except Exception:
                    pass
            import pyautogui
            # Failsafe: moving mouse to corner will abort
            pyautogui.FAILSAFE = True
            # Add a slight delay after every pyautogui call
            pyautogui.PAUSE = 0.5
        except (ImportError, SystemExit) as e:
            # SystemExit：mouseinfo 在 Linux 无 tkinter 时直接 sys.exit()
            # （打包环境 spec 排除了 tkinter），不能用 except Exception 漏掉它——
            # SystemExit 继承 BaseException，会穿透 ASGI 让整个会话报错。
            print(f"[ComputerTool] pyautogui not available: {e}. "
                  "Install python3-tk or disable computer_control tool.")

    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": ("mouse_move/mouse_click/type_text/paste_text/press_key/hotkey/"
                                            "screenshot/list_windows/activate_window/locate。"
                                            "locate 用专用定位模型把自然语言目标（target 参数）翻译成"
                                            "坐标（配 click=true 直接点击），找图标/按钮优先用它；"
                                            "activate_window 把指定窗口切到前台（比点任务栏图标可靠）；"
                                            "输入中文或非 ASCII 文本必须用 paste_text（剪贴板粘贴），"
                                            "type_text 仅适合纯 ASCII。")
                        },
                        "x": {
                            "type": "integer",
                            "description": "鼠标操作的 X 坐标。"
                        },
                        "y": {
                            "type": "integer",
                            "description": "鼠标操作的 Y 坐标。"
                        },
                        "text": {
                            "type": "string",
                            "description": "action=type_text/paste_text 时要输入/粘贴的文本。"
                        },
                        "region": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": ("action=screenshot 时的区域裁剪 [x,y,w,h]（全图图像坐标系）。"
                                            "任务栏/小图标看不清时先全图截图，再对可疑区域放大。")
                        },
                        "window": {
                            "type": "string",
                            "description": ("窗口标题关键词（大小写不敏感的子串匹配，如「微信」）。"
                                            "action=activate_window 时指定要切到前台的窗口；"
                                            "action=screenshot 时先把它激活到前台再只截该窗口区域。"
                                            "特殊值：taskbar=只截任务栏条带（找任务栏图标用）。")
                        },
                        "target": {
                            "type": "string",
                            "description": ("action=locate 时的目标描述，越具体越准，如"
                                            "「微信聊天列表里的搜索框」「任务栏绿色的微信图标」。")
                        },
                        "click": {
                            "type": "boolean",
                            "description": ("action=locate 时是否定位后直接点击（默认 false，"
                                            "只返回坐标）。已信任定位结果时设 true 一步完成。")
                        },
                        "grid": {
                            "type": "boolean",
                            "description": "action=screenshot 时是否叠加坐标网格（默认 true，便于读坐标）。"
                        },
                        "key": {
                            "type": "string",
                            "description": "action=press_key 时的按键名（如 enter、tab、esc）。"
                        },
                        "keys": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "action=hotkey 时的组合键列表（如 ['command', 'c']）。"
                        }
                    },
                    "required": ["action"]
                }
            }
        }

    @staticmethod
    def _get_lock():
        lock = getattr(ComputerTool, '__execute_lock', None)
        if lock is None:
            lock = threading.Lock()
            ComputerTool.__execute_lock = lock
        return lock

    def _to_real(self, x: float, y: float) -> tuple:
        """图像坐标系 → 真实屏幕坐标（按最近截图的缩放比换算）。"""
        s = _STATE["scale"] or 1.0
        return int(round(x / s)), int(round(y / s))

    def _check_view_bounds(self, x, y):
        """点击坐标越界检查（相对最近全图视图）。越界点击比不点更糟——
        会误关/误操作窗口（生产实证：模型从放大图读了 775 > 720 的 y 值，
        换算后飞出屏幕触发 fail-safe）。返回 None=合法，否则返回错误文本。"""
        vw, vh = _STATE["view_size"]
        if not vw or not vh:
            return None
        try:
            fx, fy = float(x), float(y)
        except (TypeError, ValueError):
            return f"Error: 坐标必须是数字（收到 {x!r}, {y!r}）"
        if fx < 0 or fy < 0 or fx > vw or fy > vh:
            return (f"Error: 坐标 ({x}, {y}) 超出全图视图范围 {vw}x{vh}。"
                    "若坐标读自 region 放大图，请先换算回全图坐标系："
                    "全图 = 放大区域原点 + 放大图内坐标；"
                    "或直接重新截一张全图再点。")
        return None

    @staticmethod
    def _paste_text(text: str) -> str:
        """经剪贴板粘贴文本（中文/非 ASCII/长文本的可靠输入方式）。"""
        try:
            import pyperclip
            pyperclip.copy(text)
        except Exception as e:
            # pyperclip 不可用时按平台兜底
            import subprocess as _sp
            import sys as _sys
            try:
                if _sys.platform.startswith('win'):
                    _sp.run(['powershell', '-NoProfile', '-Command',
                             'Set-Clipboard -Value $input'],
                            input=text.encode('utf-8'), check=True,
                            capture_output=True, timeout=10)
                else:
                    for cmd in (['xclip', '-selection', 'clipboard'],
                                ['xsel', '--clipboard', '--input'],
                                ['wl-copy']):
                        try:
                            _sp.run(cmd, input=text.encode('utf-8'), check=True,
                                    capture_output=True, timeout=10)
                            break
                        except (FileNotFoundError, _sp.CalledProcessError):
                            continue
                    else:
                        return (f"Error: 无法写入剪贴板（pyperclip 失败: {e}；"
                                "xclip/xsel/wl-copy 均不可用）")
            except Exception as e2:
                return f"Error: 无法写入剪贴板: {e}; 兜底也失败: {e2}"
        pyautogui.hotkey('ctrl', 'v')
        return f"Pasted {len(text)} chars via clipboard"

    @staticmethod
    def _pick_linux_display() -> str:
        """探测可用的 X display。UOS 等桌面 X 可能在 :1 而非 :0（多席位/
        X 重启过），DISPLAY 缺失或错指时 pyautogui 截图必崩（生产实证：
        KeyError 'DISPLAY' / DisplayConnectionError Permission denied）。
        用 python-xlib 逐个试连 :0/:1/:2，第一个通的为准。"""
        cands = []
        cur = os.environ.get("DISPLAY")
        if cur:
            cands.append(cur)
        for d in (":0", ":1", ":2"):
            if d not in cands:
                cands.append(d)
        for d in cands:
            try:
                from Xlib import display as _xd
                _xd.Display(d).close()
                return d
            except Exception:
                continue
        return cur or ":0"

    @staticmethod
    def _grab_screen():
        """截图主入口。Linux 上先选对 DISPLAY 再截（见 _pick_linux_display）；
        pyautogui 失败时兜底 gnome-screenshot 子进程。"""
        import sys as _sys
        if not _sys.platform.startswith("linux"):
            return pyautogui.screenshot()
        os.environ["DISPLAY"] = ComputerTool._pick_linux_display()
        try:
            return pyautogui.screenshot()
        except Exception as e:
            import subprocess
            import tempfile
            try:
                from PIL import Image
                tmp = tempfile.mktemp(suffix=".png")
                subprocess.run(["gnome-screenshot", "-f", tmp],
                               check=True, capture_output=True, timeout=15)
                return Image.open(tmp)
            except Exception as e2:
                raise RuntimeError(
                    f"pyautogui 截图失败（{e}）；gnome-screenshot 兜底也失败（{e2}）")

    @staticmethod
    def _enum_windows() -> list:
        """枚举可见顶层窗口。返回 [{hwnd, title, rect=(l,t,r,b), minimized, exe}]，
        按 Z-order 自上而下（Windows EnumWindows 顺序）。Linux 走 wmctrl -lG。"""
        import sys as _sys
        wins = []
        if _sys.platform.startswith("win"):
            import win32gui
            try:
                import win32process
                import psutil
            except Exception:
                win32process = None

            def _cb(hwnd, acc):
                try:
                    if not win32gui.IsWindowVisible(hwnd):
                        return
                    t = (win32gui.GetWindowText(hwnd) or "").strip()
                    if not t:
                        return
                    rect = win32gui.GetWindowRect(hwnd)
                    exe = ""
                    if win32process:
                        try:
                            _, pid = win32process.GetWindowThreadProcessId(hwnd)
                            exe = psutil.Process(pid).name()
                        except Exception:
                            pass
                    acc.append({"hwnd": hwnd, "title": t, "rect": rect,
                                "minimized": bool(win32gui.IsIconic(hwnd)),
                                "exe": exe})
                except Exception:
                    return
            win32gui.EnumWindows(_cb, wins)
        elif _sys.platform.startswith("linux"):
            wins = ComputerTool._enum_windows_x11()
        return wins

    @staticmethod
    def _enum_windows_x11() -> list:
        """Linux 窗口枚举：优先 wmctrl（解析简单），未安装则 python-xlib 直连
        X 协议枚举（纯 Python 无额外依赖，UOS 默认不带 wmctrl 也能用）。"""
        import subprocess
        try:
            out = subprocess.run(["wmctrl", "-lG"], capture_output=True,
                                 timeout=10, text=True)
            if out.returncode == 0 and out.stdout.strip():
                wins = []
                for line in out.stdout.splitlines():
                    # 格式: 0x03e00003  0  x  y  w  h  host  标题（标题可含空格）
                    parts = line.split(None, 7)
                    if len(parts) < 8:
                        continue
                    wid, x, y, w, h = parts[0], int(parts[2]), int(parts[3]), \
                        int(parts[4]), int(parts[5])
                    wins.append({"hwnd": wid, "title": parts[7].strip(),
                                 "rect": (x, y, x + w, y + h),
                                 "minimized": False, "exe": ""})
                return wins
        except FileNotFoundError:
            pass  # wmctrl 未安装，走 xlib
        return ComputerTool._enum_windows_xlib()

    @staticmethod
    def _xlib_display():
        from Xlib import display as _xd
        os.environ.setdefault("DISPLAY", ComputerTool._pick_linux_display())
        return _xd.Display(os.environ["DISPLAY"])

    @staticmethod
    def _enum_windows_xlib() -> list:
        """python-xlib 枚举 _NET_CLIENT_LIST 里的顶层窗口（标题/几何/隐藏态）。"""
        from Xlib import X as _X
        from Xlib import Xatom as _Xatom
        disp = ComputerTool._xlib_display()
        root = disp.screen().root
        a_client = disp.intern_atom("_NET_CLIENT_LIST")
        a_name = disp.intern_atom("_NET_WM_NAME")
        a_state = disp.intern_atom("_NET_WM_STATE")
        a_hidden = disp.intern_atom("_NET_WM_STATE_HIDDEN")
        prop = root.get_full_property(a_client, _X.AnyPropertyType)
        wins = []
        for wid in (prop.value if prop else []):
            try:
                w = disp.create_resource_object("window", wid)
                name = ""
                p = w.get_full_property(a_name, _X.AnyPropertyType)
                if p and p.value:
                    name = p.value.decode("utf-8", "replace") \
                        if isinstance(p.value, bytes) else str(p.value)
                if not name:
                    p2 = w.get_full_property(_Xatom.WM_NAME, _X.AnyPropertyType)
                    if p2 and p2.value:
                        name = p2.value.decode("utf-8", "replace") \
                            if isinstance(p2.value, bytes) else str(p2.value)
                name = name.strip()
                if not name:
                    continue
                geom = w.get_geometry()
                top = w.translate_coords(root, 0, 0)
                states = w.get_full_property(a_state, _X.AnyPropertyType)
                hidden = bool(states and a_hidden in states.value)
                x, y = top.x, top.y
                wins.append({"hwnd": wid, "title": name,
                             "rect": (x, y, x + geom.width, y + geom.height),
                             "minimized": hidden, "exe": ""})
            except Exception:
                continue
        disp.close()
        return wins

    @staticmethod
    def _match_window(wins: list, query: str):
        """按标题匹配窗口。优先级：完全相同 > 前缀 > 子串；同级取列表靠前
        （Windows 枚举按 Z-order，最前的最可能是用户正在用的）。"""
        q = (query or "").strip().lower()
        if not q:
            return None
        for pred in (lambda t: t == q,
                     lambda t: t.startswith(q),
                     lambda t: q in t):
            for w in wins:
                if pred(w["title"].lower()):
                    return w
        return None

    @staticmethod
    def _activate_win(w: dict):
        """把窗口带到前台（最小化先还原）。SetForegroundWindow 有前台锁，
        经典解法：先送一次 Alt 按下/松开拿到前台权限再调用。"""
        import sys as _sys
        if _sys.platform.startswith("win"):
            import ctypes
            import win32con
            import win32gui
            hwnd = w["hwnd"]
            if w.get("minimized"):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.3)
            try:
                ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)  # ALT down
                win32gui.SetForegroundWindow(hwnd)
            finally:
                ctypes.windll.user32.keybd_event(0x12, 0, 2, 0)  # KEYUP
        else:
            ComputerTool._activate_win_x11(w)
        time.sleep(0.4)  # 等窗口重绘，紧跟的截图才不会截到残影

    @staticmethod
    def _activate_win_x11(w: dict):
        """Linux 窗口激活：优先 wmctrl，未安装则 python-xlib 发
        _NET_ACTIVE_WINDOW 客户端消息（EWMH 标准，KDE/GNOME/深度 DDE 都认），
        最小化窗口由窗口管理器自动还原，再 raise 到顶层。"""
        import subprocess
        try:
            out = subprocess.run(["wmctrl", "-i", "-a", str(w["hwnd"])],
                                 capture_output=True, timeout=10)
            if out.returncode == 0:
                return
        except FileNotFoundError:
            pass  # wmctrl 未安装，走 xlib
        from Xlib import X as _X
        from Xlib import protocol as _proto
        disp = ComputerTool._xlib_display()
        root = disp.screen().root
        win = disp.create_resource_object("window", w["hwnd"])
        a_active = disp.intern_atom("_NET_ACTIVE_WINDOW")
        data = [2, _X.CurrentTime, 0, 0, 0]  # source=2 表示直接用户请求
        ev = _proto.event.ClientMessage(window=win, client_type=a_active,
                                        data=(32, data))
        mask = _X.SubstructureRedirectMask | _X.SubstructureNotifyMask
        root.send_event(ev, event_mask=mask)
        try:
            win.configure(stack_mode=_X.Above)
        except Exception:
            pass
        disp.flush()
        disp.close()

    @staticmethod
    def _taskbar_rect():
        """Windows 任务栏矩形（真实屏幕坐标）。失败返回 None。"""
        import sys as _sys
        if not _sys.platform.startswith("win"):
            return None
        try:
            import win32gui
            hwnd = win32gui.FindWindow("Shell_TrayWnd", None)
            return win32gui.GetWindowRect(hwnd) if hwnd else None
        except Exception:
            return None

    @staticmethod
    def _format_window_list(wins: list, limit: int = 30) -> str:
        lines = []
        for w in wins[:limit]:
            l, t, r, b = w["rect"]
            flag = "(最小化)" if w.get("minimized") else ""
            exe = f" [{w['exe']}]" if w.get("exe") else ""
            lines.append(f"- {w['title']}{exe} 位置({l},{t}) "
                         f"尺寸({r - l}x{b - t}){flag}")
        if len(wins) > limit:
            lines.append(f"... 共 {len(wins)} 个，仅列前 {limit} 个")
        return "\n".join(lines) if lines else "(无可见窗口)"

    def _find_and_activate(self, query: str):
        """找窗口并切前台，返回激活后的最新窗口信息（rect 可能变化，重新枚举）。
        找不到返回错误文本（字符串）。"""
        wins = self._enum_windows()
        w = self._match_window(wins, query)
        if not w:
            return (f"Error: 没找到标题包含「{query}」的窗口。当前可见窗口：\n"
                    + self._format_window_list(wins))
        self._activate_win(w)
        # 最小化还原/尺寸变化后 rect 会变，重新枚举拿最新位置
        for w2 in self._enum_windows():
            if w2["hwnd"] == w["hwnd"]:
                return w2
        return w


    def execute(self, **kwargs) -> str:
        with self._get_lock():
            action = kwargs.get("action")

            try:
                if action == 'mouse_move':
                    x = kwargs.get('x')
                    y = kwargs.get('y')
                    if x is None or y is None:
                        return "Error: x and y coordinates required for mouse_move."
                    bad = self._check_view_bounds(x, y)
                    if bad:
                        return bad
                    rx, ry = self._to_real(x, y)
                    pyautogui.moveTo(rx, ry, duration=0.5)
                    _STATE["action"] = 'mouse_move'
                    _STATE["moved"] = True
                    _STATE["seen"] = False
                    return f"Mouse moved to image-coords ({x}, {y}) -> screen ({rx}, {ry})"

                elif action == 'mouse_click':
                    x = kwargs.get('x')
                    y = kwargs.get('y')
                    if x is not None and y is not None:
                        bad = self._check_view_bounds(x, y)
                        if bad:
                            return bad
                        # 严格模式（弱模型）：强制 hover-verify——必须
                        # mouse_move → screenshot（看到光标位置）→ 才能点击，
                        # 否则模型直接开点命中率极低（生产实证连点四五个不中）
                        _agent = kwargs.get('_agent_context')
                        if _strict_mode(_agent) and not (_STATE["moved"] and _STATE["seen"]):
                            return ("Error: 当前模型处于悬停验证模式，禁止直接点击。必须按序执行：\n"
                                    "1. mouse_move 移到目标位置\n"
                                    "2. screenshot 确认图上红点（光标标记）对准了目标\n"
                                    "3. 对准后才允许 mouse_click；没对准就再 mouse_move 修正后重复第 2 步")
                        rx, ry = self._to_real(x, y)
                        pyautogui.click(rx, ry)
                        hint = ""
                        if _STATE["action"] != 'mouse_move':
                            # 没有悬停验证就点——弱模型容易直接开点打偏（生产实证），
                            # 结果里轻推一下 hover-verify 流程
                            hint = ("（提示：重要目标建议先 mouse_move→截图确认红点对准→"
                                    "再 mouse_click，命中率明显更高）")
                        _STATE["action"] = 'mouse_click'
                        _STATE["moved"] = False
                        _STATE["seen"] = False
                        return f"Clicked at image-coords ({x}, {y}) -> screen ({rx}, {ry}){hint}"
                    else:
                        pyautogui.click()
                        _STATE["action"] = 'mouse_click'
                        return "Clicked at current location"

                elif action == 'type_text':
                    text = kwargs.get('text')
                    if not text:
                        return "Error: text required for type_text."
                    if not all(ord(c) < 128 for c in text):
                        # pyautogui.write 只支持 ASCII——含中文/非 ASCII 自动转剪贴板
                        # 粘贴（agent 此前每次都手写 python 折腾 pyperclip+Ctrl+V，
                        # 一个动作替代一整段脚本）
                        return self._paste_text(text)
                    pyautogui.write(text, interval=0.05)
                    return f"Typed text: {text}"

                elif action == 'paste_text':
                    text = kwargs.get('text')
                    if not text:
                        return "Error: text required for paste_text."
                    return self._paste_text(text)

                elif action == 'press_key':
                    key = kwargs.get('key')
                    if not key:
                        return "Error: key required for press_key."
                    pyautogui.press(key)
                    return f"Pressed key: {key}"

                elif action == 'hotkey':
                    keys = kwargs.get('keys')
                    if not keys or not isinstance(keys, list):
                        return "Error: list of keys required for hotkey."
                    pyautogui.hotkey(*keys)
                    return f"Pressed hotkey: {'+'.join(keys)}"

                elif action == 'list_windows':
                    wins = self._enum_windows()
                    return ("可见窗口列表（Z-order 自上而下）：\n"
                            + self._format_window_list(wins))

                elif action == 'activate_window':
                    q = kwargs.get('window') or kwargs.get('text')
                    if not q:
                        return "Error: activate_window 需要 window 参数（窗口标题关键词，如「微信」）。"
                    res = self._find_and_activate(str(q))
                    if isinstance(res, str):
                        return res
                    l, t, r, b = res["rect"]
                    # 切了窗口，之前的悬停验证上下文作废（画面已变）
                    _STATE["moved"] = False
                    _STATE["seen"] = False
                    return (f"已切换到前台: {res['title']} "
                            f"位置({l},{t}) 尺寸({r - l}x{b - t})。"
                            "请重新截图确认窗口状态后再操作。")

                elif action == 'locate':
                    target = kwargs.get('target') or kwargs.get('text')
                    if not target:
                        return ("Error: locate 需要 target 参数"
                                "（目标的自然语言描述，如「搜索框」「微信图标」）。")
                    # 专用定位模型（UI-TARS 等）把文字描述翻译成坐标——弱模型
                    # 自己估像素偏差数百像素（生产实证），定位服务是系统级精度。
                    img = self._grab_screen()
                    full_w, full_h = img.size
                    ratio = 1.0
                    if max(full_w, full_h) > 1920:
                        ratio = 1920 / max(full_w, full_h)
                        img = img.resize((int(full_w * ratio), int(full_h * ratio)))
                    from tools.screen_grounder import locate_element
                    res = locate_element(img, str(target))
                    if isinstance(res, str):
                        return res
                    # 图像像素 → 真实屏幕 → 全图视图坐标（与点击坐标系一致）
                    rx, ry = int(res[0] / ratio), int(res[1] / ratio)
                    cur_scale = _STATE["scale"] or 1.0
                    vx, vy = int(round(rx * cur_scale)), int(round(ry * cur_scale))
                    bad = self._check_view_bounds(vx, vy)
                    if bad:
                        return (f"Error: 定位服务返回的点换算后越界（{rx},{ry}），"
                                "结果不可信，请改用截图+网格/OCR 锚点手动定位。")
                    if kwargs.get('click'):
                        pyautogui.moveTo(rx, ry, duration=0.3)
                        pyautogui.click(rx, ry)
                        _STATE["action"] = 'mouse_click'
                        _STATE["moved"] = False
                        _STATE["seen"] = False
                        return (f"Located+clicked 「{target}」view ({vx},{vy}) -> "
                                f"screen ({rx},{ry})。请截图验证是否达到预期状态。")
                    return (f"定位结果：「{target}」中心点全图坐标 ({vx}, {vy})。"
                            "可 mouse_move 到该坐标截图确认后再点击，"
                            "或下次 locate 时带 click=true 一步完成。")

                elif action == 'screenshot':
                    # 悬停验证状态：mouse_move 后截图即视为「已确认光标位置」，
                    # 严格模式下此后才放行 mouse_click
                    if _STATE["moved"]:
                        _STATE["seen"] = True
                    # 存到数据目录的 screenshots/ 下（时间戳命名），不能写 CWD——
                    # 在源码运行时 CWD 是项目根目录，会污染仓库（生产实证）。
                    from core.paths import get_data_dir
                    shots_dir = os.path.join(get_data_dir(), "screenshots")
                    os.makedirs(shots_dir, exist_ok=True)
                    import time as _time
                    screenshot_path = os.path.join(
                        shots_dir, f"screenshot_{_time.strftime('%Y%m%d_%H%M%S')}.jpg")
                    # 全分辨率截图（2K/4K PNG 数 MB）注入会把本地模型的上下文和
                    # 视觉编码打爆（卡死/InternalServerError 实证）——长边压到
                    # 1080p 按 1:1 原生注入（坐标零失真）；JPEG q80 控制体积（~300KB）。
                    # Linux 多 display 环境（UOS X 常在 :1）先 _grab_screen 探测再截。
                    img = self._grab_screen()
                    full_w, full_h = img.size
                    # 仅超大屏（>1920，如 4K）才降采样：图像剪枝已保证上下文只
                    # 保留最近一张，单张全尺寸不再撑爆上下文；1920x1080 按 1:1
                    # 原生分辨率注入，坐标零失真、图标清晰（压缩图导致模型
                    # grounding 不可靠——生产实证）
                    max_edge = 1920
                    full_scale = max_edge / max(full_w, full_h) if max(full_w, full_h) > max_edge else 1.0

                    # window 参数：只截指定窗口（先激活到前台，否则截到的是
                    # 上层遮挡窗口的内容）；window="taskbar" 只截任务栏条带。
                    # 内部转成 region 走同一套放大/标注逻辑（标注仍显示全图
                    # 坐标，点击坐标系不变）
                    win_query = kwargs.get('window')
                    win_region = None
                    if win_query:
                        q = str(win_query).strip()
                        if q.lower() == "taskbar":
                            tb = self._taskbar_rect()
                            if not tb:
                                return "Error: 获取任务栏位置失败（仅 Windows 支持 window=\"taskbar\"）"
                            win_region = tb
                        else:
                            res = self._find_and_activate(q)
                            if isinstance(res, str):
                                return res
                            win_region = res["rect"]

                    # 区域放大：region 按全图图像坐标系（缩放视图）给出，换算到
                    # 真实坐标裁剪，裁剪图不再降采样——任务栏/小图标放大到原生
                    # 分辨率，模型才能读准（全图视图里 20px 图标根本点不准，生产实证）
                    region = kwargs.get('region')
                    if win_region and not region:
                        l, t, r, b = win_region
                        region = [int(l * full_scale), int(t * full_scale),
                                  int((r - l) * full_scale), int((b - t) * full_scale)]
                    region_note = ""
                    if region and isinstance(region, (list, tuple)) and len(region) == 4:
                        rx = max(0, int(region[0] / full_scale))
                        ry = max(0, int(region[1] / full_scale))
                        rw = int(region[2] / full_scale)
                        rh = int(region[3] / full_scale)
                        img = img.crop((rx, ry, min(rx + rw, full_w), min(ry + rh, full_h)))
                        region_note = (
                            f"（区域放大：全图图像坐标 ({region[0]},{region[1]}) 起 "
                            f"{region[2]}x{region[3]}，原生分辨率；"
                            "放大图里的网格数字就是全图坐标，直接读数点击）"
                        )

                    real_w, real_h = img.size
                    if max(img.size) > max_edge and not region_note:
                        _r = max_edge / max(img.size)
                        img = img.resize((int(img.size[0] * _r), int(img.size[1] * _r)))
                    saved_w, saved_h = img.size

                    # 坐标网格：小模型读绝对坐标全靠猜，网格让它直接读数。
                    # 四边都标注（任务栏在底部，光有顶边标注它得从 y=700 往下
                    # 脑补——生产实证）；字体随分辨率放大（1920 下 ~24px 才读得清）。
                    # region 放大图：标注直接显示【全图坐标】，模型读数即可点击，
                    # 免去「区域原点+细节坐标」的手动换算（k3 实测这一步会漂移
                    # ~20px，足以点错相邻图标）
                    if kwargs.get('grid', True):
                        try:
                            from PIL import ImageDraw, ImageFont
                            # 网格标注用的坐标系：全图视图=图像坐标恒等；
                            # region 放大图=全图坐标（crop 内位置按缩放比换算）
                            if region_note:
                                g_ox, g_oy, g_vs = float(region[0]), float(region[1]), full_scale
                            else:
                                g_ox, g_oy, g_vs = 0.0, 0.0, 1.0
                            font_size = max(14, saved_h // 45)
                            try:
                                font = ImageFont.truetype("arial.ttf", font_size)
                            except Exception:
                                try:
                                    font = ImageFont.truetype("DejaVuSans.ttf", font_size)
                                except Exception:
                                    font = ImageFont.load_default()
                            d = ImageDraw.Draw(img, 'RGBA')
                            import math as _math
                            _span_x = saved_w * g_vs
                            _start_x = _math.ceil(g_ox / 100) * 100
                            for gv in range(_start_x, int(g_ox + _span_x), 100):
                                px = (gv - g_ox) / g_vs
                                d.line([(px, 0), (px, saved_h)], fill=(255, 60, 60, 80), width=1)
                                d.text((px + 3, 3), str(gv), fill=(255, 60, 60, 230), font=font)
                                d.text((px + 3, saved_h - font_size - 4), str(gv), fill=(255, 60, 60, 230), font=font)
                            _span_y = saved_h * g_vs
                            _start_y = _math.ceil(g_oy / 100) * 100
                            for gv in range(_start_y, int(g_oy + _span_y), 100):
                                py = (gv - g_oy) / g_vs
                                d.line([(0, py), (saved_w, py)], fill=(255, 60, 60, 80), width=1)
                                d.text((3, py + 3), str(gv), fill=(255, 60, 60, 230), font=font)
                                _tw = d.textlength(str(gv), font=font)
                                d.text((saved_w - _tw - 4, py + 3), str(gv), fill=(255, 60, 60, 230), font=font)
                        except Exception:
                            pass

                    # 鼠标光标标记：移动后截图确认对准了再点击（hover-verify 范式）。
                    # 红点+十字标在光标处，标注数字是【全图坐标】——模型对比
                    # 目标坐标与光标坐标，对准了才 click。
                    try:
                        from PIL import ImageDraw, ImageFont
                        mx_real, my_real = pyautogui.position()
                        if region_note:
                            cmx = mx_real - rx
                            cmy = my_real - ry
                        else:
                            cmx = mx_real * full_scale
                            cmy = my_real * full_scale
                        # 只画在可视范围内
                        if 0 <= cmx < saved_w and 0 <= cmy < saved_h:
                            d2 = ImageDraw.Draw(img, 'RGBA')
                            mfx = int(round(mx_real * full_scale))
                            mfy = int(round(my_real * full_scale))
                            fs2 = max(12, saved_h // 55)
                            try:
                                f2 = ImageFont.truetype("arial.ttf", fs2)
                            except Exception:
                                try:
                                    f2 = ImageFont.truetype("DejaVuSans.ttf", fs2)
                                except Exception:
                                    f2 = ImageFont.load_default()
                            r_m = max(8, fs2 // 2)
                            d2.ellipse([cmx - r_m, cmy - r_m, cmx + r_m, cmy + r_m],
                                       outline=(255, 0, 0, 255), width=3)
                            d2.line([(cmx - r_m, cmy), (cmx + r_m, cmy)], fill=(255, 0, 0, 255), width=2)
                            d2.line([(cmx, cmy - r_m), (cmx, cmy + r_m)], fill=(255, 0, 0, 255), width=2)
                            # 标注只写坐标数字（"鼠标"等中文在 arial 下渲染成
                            # 方框，纯数字哪都能显示）
                            d2.text((cmx + r_m + 4, cmy - fs2 - 2),
                                    f"({mfx},{mfy})", fill=(255, 0, 0, 255), font=f2)
                    except Exception:
                        pass

                    img.convert("RGB").save(screenshot_path, "JPEG", quality=80)

                    # 前台窗口标题：多窗口重叠时模型经常搞混「当前谁在前面」
                    # （生产实证：微信窗口被 WPS 盖住后，模型对着 WPS 的搜索框
                    # 连点三次当微信搜索框）——截图结果里直接告诉它
                    try:
                        fg_title = pyautogui.getActiveWindowTitle() or "(无)"
                    except Exception:
                        fg_title = "(未知)"
                    fg_note = f"当前前台窗口: {fg_title}"
                    # 点击坐标系锚定最近一张【全图】截图：region 放大仅用于观察，
                    # 不更新缩放比——否则 region（原生分辨率，scale=1.0）会把后续
                    # 全图坐标的换算废掉，点偏半个屏幕（生产实证：region 任务栏
                    # 条带后 click(325,705) 被按 1:1 点到了屏幕中部）
                    if not region_note:
                        _STATE["scale"] = saved_w / real_w if real_w else 1.0
                        _STATE["view_size"] = (saved_w, saved_h)
                    cur_scale = _STATE["scale"] or 1.0
                    if region_note:
                        scale_note = (
                            f"真实屏幕 {real_w}x{real_h}，图像 {saved_w}x{saved_h}"
                            f"（缩放比 {cur_scale:.4f}）。"
                            "点击坐标一律按最近一张【全图截图】的坐标系输入"
                            "（本放大图只用于观察细节）；"
                        ) + region_note
                    else:
                        scale_note = (
                            f"真实屏幕 {real_w}x{real_h}，图像 {saved_w}x{saved_h}"
                            f"（缩放比 {cur_scale:.4f}）。"
                            "操控电脑工具的坐标按你看到的图像像素坐标输入即可（自动换算）；"
                            "若改用 execute_python+pyautogui 直接点击，"
                            "真实坐标 = 图像坐标 ÷ 缩放比。"
                        )
                    # 鼠标位置的文本注记（与图上红点标记一致，全图坐标）
                    try:
                        _mx, _my = pyautogui.position()
                        _mvx = int(round(_mx * full_scale))
                        _mvy = int(round(_my * full_scale))
                        fg_note += f"；当前鼠标位置(全图坐标): ({_mvx}, {_mvy})"
                    except Exception:
                        pass
                    # OCR 文字锚点：弱视觉模型按文字找目标（坐标由 OCR 精确
                    # 给出，与模型 grounding 能力脱钩——qwen3.8 实测视觉定位
                    # 偏差数百像素，OCR 锚点偏差 <10px）。坐标换算到点击用的
                    # 全图坐标系（region 放大图：全图=区域原点+图内坐标×缩放比）。
                    # 性能：CPU 上每次 ~9s，默认只在悬停验证模式（弱模型）下跑；
                    # config computer_ocr_anchors = strict(默认)/always/never。
                    ocr_note = ""
                    try:
                        import json as _json
                        from core.paths import get_data_path as _gdp
                        _ocr_mode = "strict"
                        try:
                            _c = _json.load(open(_gdp("config.json"), encoding="utf-8"))
                            _ocr_mode = str(_c.get("computer_ocr_anchors", "strict")).lower()
                        except Exception:
                            pass
                        _run_ocr = (_ocr_mode == "always") or (
                            _ocr_mode != "never" and _strict_mode(kwargs.get('_agent_context')))
                        if _run_ocr:
                            from tools.screen_ocr import ocr_anchors, format_anchors
                            _anchors = ocr_anchors(screenshot_path)
                            if region_note:
                                ocr_note = format_anchors(
                                    _anchors, view_scale=full_scale,
                                    offset=(region[0], region[1]))
                            else:
                                ocr_note = format_anchors(_anchors, view_scale=1.0)
                    except Exception as _ocr_e:
                        print(f"[ComputerTool] OCR anchors failed: {_ocr_e}")
                    import base64
                    try:
                        with open(screenshot_path, "rb") as f:
                            b64 = base64.b64encode(f.read()).decode("ascii")
                        img_url = f"data:image/jpeg;base64,{b64}"
                        return (
                            f"Screenshot saved to {screenshot_path}\n"
                            f"{fg_note}\n"
                            f"{scale_note}\n"
                            + (f"{ocr_note}\n" if ocr_note else "")
                            + f"[SCREENSHOT_DATA:{img_url}]"
                        )
                    except Exception:
                        return f"Screenshot saved to {screenshot_path}"

                else:
                    return f"Error: Unknown action '{action}'"

            except Exception as e:
                return f"Error executing computer control ({action}): {str(e)}"
