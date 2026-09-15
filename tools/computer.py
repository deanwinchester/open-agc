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
点击坐标禁止凭感觉估计，必须走以下流程：
1. 先全图截图，看清红色网格：四条边上的红色数字就是坐标刻度。
2. 定位目标在图上位于【哪两条竖线】与【哪两条横线】之间，先回答这个问题。
3. 再按格内比例读出坐标（如目标在 x=400 与 x=500 线之间偏右约 60%，则 x≈460）。
4. 目标小于 60px（任务栏图标/小按钮）时：先 region 放大该区域确认，再把
   细节坐标换算回全图坐标（全图 = 区域原点 + 放大图内坐标）后点击。
5. 点击后必须重新截图验证状态变化；没有变化就重新读网格定位，禁止原坐标
   重复盲试。
6. 多窗口重叠时先看截图结果里的「当前前台窗口」——目标不在前台就先
   alt+tab 或点任务栏图标激活，再截图定位。
7. 检查应用是否在运行：Windows 进程名常与品牌名不同（微信=WeChat.exe），
   tasklist 后用 findstr 过滤（cmd 没有 grep）。
"""

class ComputerTool(BaseTool):
    name: str = "computer_control"
    description: str = ("物理操控本机鼠标和键盘（点击、移动、输入、按键、截图）。"
                        "browser_automation 等工具无法完成的 GUI 操作才用它。"
                        "坐标一律按最近一张【全图截图】的像素坐标输入（工具自动按"
                        "缩放比换算回真实屏幕，无需自己换算）；region 放大截图仅"
                        "用于观察细节，不改变点击坐标系。"
                        "输入中文等非 ASCII 文本用 paste_text（剪贴板粘贴），"
                        "type_text 仅适合纯 ASCII。")

    # 最近一次截图的缩放比（saved_px / real_px）。点击坐标按截图图像坐标系输入，
    # 换算到真实屏幕坐标 = 输入 / _last_scale。无截图时为 1.0（即按真实坐标）。
    _last_scale: float = 1.0
    # 最近一张全图截图的视图尺寸（点击越界检查用；region 放大不更新它）
    _last_view_size: tuple = (0, 0)
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

    def _check_view_bounds(self, x, y):
        """点击坐标越界检查（相对最近全图视图）。越界点击比不点更糟——
        会误关/误操作窗口（生产实证：模型从放大图读了 775 > 720 的 y 值，
        换算后飞出屏幕触发 fail-safe）。返回 None=合法，否则返回错误文本。"""
        vw, vh = ComputerTool._last_view_size
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
                    return f"Mouse moved to image-coords ({x}, {y}) -> screen ({rx}, {ry})"

                elif action == 'mouse_click':
                    x = kwargs.get('x')
                    y = kwargs.get('y')
                    if x is not None and y is not None:
                        bad = self._check_view_bounds(x, y)
                        if bad:
                            return bad
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
                    # 1080p 按 1:1 原生注入（坐标零失真）；JPEG q80 控制体积（~300KB）。
                    img = pyautogui.screenshot()
                    full_w, full_h = img.size
                    # 仅超大屏（>1920，如 4K）才降采样：图像剪枝已保证上下文只
                    # 保留最近一张，单张全尺寸不再撑爆上下文；1920x1080 按 1:1
                    # 原生分辨率注入，坐标零失真、图标清晰（压缩图导致模型
                    # grounding 不可靠——生产实证）
                    max_edge = 1920
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
                            "点击请换算回【全图坐标】：全图 = 区域原点 + 细节坐标）"
                        )

                    real_w, real_h = img.size
                    if max(img.size) > max_edge and not region_note:
                        _r = max_edge / max(img.size)
                        img = img.resize((int(img.size[0] * _r), int(img.size[1] * _r)))
                    saved_w, saved_h = img.size

                    # 坐标网格：小模型读绝对坐标全靠猜，网格让它直接读数。
                    # 四边都标注（任务栏在底部，光有顶边标注它得从 y=700 往下
                    # 脑补——生产实证）；字体随分辨率放大（1920 下 ~24px 才读得清）
                    if kwargs.get('grid', True):
                        try:
                            from PIL import ImageDraw, ImageFont
                            font_size = max(14, saved_h // 45)
                            try:
                                font = ImageFont.truetype("arial.ttf", font_size)
                            except Exception:
                                try:
                                    font = ImageFont.truetype("DejaVuSans.ttf", font_size)
                                except Exception:
                                    font = ImageFont.load_default()
                            d = ImageDraw.Draw(img, 'RGBA')
                            for gx in range(100, saved_w, 100):
                                d.line([(gx, 0), (gx, saved_h)], fill=(255, 60, 60, 80), width=1)
                                d.text((gx + 3, 3), str(gx), fill=(255, 60, 60, 230), font=font)
                                d.text((gx + 3, saved_h - font_size - 4), str(gx), fill=(255, 60, 60, 230), font=font)
                            for gy in range(100, saved_h, 100):
                                d.line([(0, gy), (saved_w, gy)], fill=(255, 60, 60, 80), width=1)
                                d.text((3, gy + 3), str(gy), fill=(255, 60, 60, 230), font=font)
                                _tw = d.textlength(str(gy), font=font)
                                d.text((saved_w - _tw - 4, gy + 3), str(gy), fill=(255, 60, 60, 230), font=font)
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
                        ComputerTool._last_scale = saved_w / real_w if real_w else 1.0
                        ComputerTool._last_view_size = (saved_w, saved_h)
                    cur_scale = ComputerTool._last_scale or 1.0
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
