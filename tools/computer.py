import os
import time
import threading
from typing import Any, Dict, Optional
from pydantic import Field

from tools.base import BaseTool

class ComputerTool(BaseTool):
    name: str = "computer_control"
    description: str = ("物理操控本机鼠标和键盘（点击、移动、输入、按键、截图）。"
                        "browser_automation 等工具无法完成的 GUI 操作才用它。"
                        "坐标一律按你【看到的截图图像】的像素坐标输入（工具自动按"
                        "缩放比换算回真实屏幕，无需自己换算）。"
                        "输入中文等非 ASCII 文本用 paste_text（剪贴板粘贴），"
                        "type_text 仅适合纯 ASCII。")

    # 最近一次截图的缩放比（saved_px / real_px）。点击坐标按截图图像坐标系输入，
    # 换算到真实屏幕坐标 = 输入 / _last_scale。无截图时为 1.0（即按真实坐标）。
    _last_scale: float = 1.0
    def __init__(self, **data):
        super().__init__(**data)
        # Import pyautogui lazily to avoid issues if not installed or running headlessly
        global pyautogui
        try:
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
                            "description": ("mouse_move/mouse_click/type_text/paste_text/press_key/hotkey/screenshot。"
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
        s = ComputerTool._last_scale or 1.0
        return int(round(x / s)), int(round(y / s))

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

    def execute(self, **kwargs) -> str:
        with self._get_lock():
            action = kwargs.get("action")

            try:
                if action == 'mouse_move':
                    x = kwargs.get('x')
                    y = kwargs.get('y')
                    if x is None or y is None:
                        return "Error: x and y coordinates required for mouse_move."
                    rx, ry = self._to_real(x, y)
                    pyautogui.moveTo(rx, ry, duration=0.5)
                    return f"Mouse moved to image-coords ({x}, {y}) -> screen ({rx}, {ry})"

                elif action == 'mouse_click':
                    x = kwargs.get('x')
                    y = kwargs.get('y')
                    if x is not None and y is not None:
                        rx, ry = self._to_real(x, y)
                        pyautogui.click(rx, ry)
                        return f"Clicked at image-coords ({x}, {y}) -> screen ({rx}, {ry})"
                    else:
                        pyautogui.click()
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

                elif action == 'screenshot':
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
                    # 1280、JPEG q70，体积降到 ~100KB 级，llama.cpp/vLLM 都能秒处。
                    img = pyautogui.screenshot()
                    full_w, full_h = img.size
                    max_edge = 1280
                    full_scale = max_edge / max(full_w, full_h) if max(full_w, full_h) > max_edge else 1.0

                    # 区域放大：region 按全图图像坐标系（缩放视图）给出，换算到
                    # 真实坐标裁剪，裁剪图不再降采样——任务栏/小图标放大到原生
                    # 分辨率，模型才能读准（全图视图里 20px 图标根本点不准，生产实证）
                    region = kwargs.get('region')
                    region_note = ""
                    if region and isinstance(region, (list, tuple)) and len(region) == 4:
                        rx = int(region[0] / full_scale)
                        ry = int(region[1] / full_scale)
                        rw = int(region[2] / full_scale)
                        rh = int(region[3] / full_scale)
                        img = img.crop((rx, ry, min(rx + rw, full_w), min(ry + rh, full_h)))
                        region_note = (
                            f"（区域放大：全图图像坐标 ({region[0]},{region[1]}) 起 "
                            f"{region[2]}x{region[3]}，原生分辨率；"
                            "点击请换算回全图坐标：全图 = 区域原点 + 细节坐标）"
                        )

                    real_w, real_h = img.size
                    if max(img.size) > max_edge and not region_note:
                        _r = max_edge / max(img.size)
                        img = img.resize((int(img.size[0] * _r), int(img.size[1] * _r)))
                    saved_w, saved_h = img.size

                    # 坐标网格：小模型读绝对坐标全靠猜，网格让它直接读数（可选关闭）
                    if kwargs.get('grid', True):
                        try:
                            from PIL import ImageDraw
                            d = ImageDraw.Draw(img, 'RGBA')
                            for gx in range(100, saved_w, 100):
                                d.line([(gx, 0), (gx, saved_h)], fill=(255, 60, 60, 80), width=1)
                                d.text((gx + 2, 2), str(gx), fill=(255, 60, 60, 220))
                            for gy in range(100, saved_h, 100):
                                d.line([(0, gy), (saved_w, gy)], fill=(255, 60, 60, 80), width=1)
                                d.text((2, gy + 2), str(gy), fill=(255, 60, 60, 220))
                        except Exception:
                            pass

                    img.convert("RGB").save(screenshot_path, "JPEG", quality=70)
                    # 记录缩放比：点击坐标按图像坐标系输入，执行时据此换算回
                    # 真实屏幕（模型按看到的图估坐标天然准确，生产实证不记录
                    # 缩放比时点击系统性偏移到 2/3 处）
                    ComputerTool._last_scale = saved_w / real_w if real_w else 1.0
                    scale_note = (
                        f"真实屏幕 {real_w}x{real_h}，图像 {saved_w}x{saved_h}"
                        f"（缩放比 {ComputerTool._last_scale:.4f}）。"
                        "操控电脑工具的坐标按你看到的图像像素坐标输入即可（自动换算）；"
                        "若改用 execute_python+pyautogui 直接点击，"
                        "真实坐标 = 图像坐标 ÷ 缩放比。"
                    ) + region_note
                    import base64
                    try:
                        with open(screenshot_path, "rb") as f:
                            b64 = base64.b64encode(f.read()).decode("ascii")
                        img_url = f"data:image/jpeg;base64,{b64}"
                        return (
                            f"Screenshot saved to {screenshot_path}\n"
                            f"{scale_note}\n"
                            f"[SCREENSHOT_DATA:{img_url}]"
                        )
                    except Exception:
                        return f"Screenshot saved to {screenshot_path}"

                else:
                    return f"Error: Unknown action '{action}'"

            except Exception as e:
                return f"Error executing computer control ({action}): {str(e)}"
