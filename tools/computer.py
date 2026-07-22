import os
import time
import threading
from typing import Any, Dict, Optional
from pydantic import Field

from tools.base import BaseTool

class ComputerTool(BaseTool):
    name: str = "computer_control"
    description: str = "物理操控本机鼠标和键盘（点击、移动、输入、按键、截图）。browser_automation 等工具无法完成的 GUI 操作才用它。"
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
        except ImportError as e:
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
                            "description": "mouse_move/mouse_click/type_text/press_key/hotkey/screenshot"
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
                            "description": "action=type_text 时要输入的文本。"
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

    def execute(self, **kwargs) -> str:
        with self._get_lock():
            action = kwargs.get("action")

            try:
                if action == 'mouse_move':
                    x = kwargs.get('x')
                    y = kwargs.get('y')
                    if x is None or y is None:
                        return "Error: x and y coordinates required for mouse_move."
                    pyautogui.moveTo(x, y, duration=0.5)
                    return f"Mouse moved to ({x}, {y})"

                elif action == 'mouse_click':
                    x = kwargs.get('x')
                    y = kwargs.get('y')
                    if x is not None and y is not None:
                        pyautogui.click(x, y)
                        return f"Clicked at ({x}, {y})"
                    else:
                        pyautogui.click()
                        return "Clicked at current location"

                elif action == 'type_text':
                    text = kwargs.get('text')
                    if not text:
                        return "Error: text required for type_text."
                    pyautogui.write(text, interval=0.05)
                    return f"Typed text: {text}"

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
                    screenshot_path = os.path.abspath("screenshot.png")
                    pyautogui.screenshot(screenshot_path)
                    import base64
                    try:
                        with open(screenshot_path, "rb") as f:
                            b64 = base64.b64encode(f.read()).decode("ascii")
                        img_url = f"data:image/png;base64,{b64}"
                        return (
                            f"Screenshot saved to {screenshot_path}\n"
                            f"[SCREENSHOT_DATA:{img_url}]"
                        )
                    except Exception:
                        return f"Screenshot saved to {screenshot_path}"

                else:
                    return f"Error: Unknown action '{action}'"

            except Exception as e:
                return f"Error executing computer control ({action}): {str(e)}"
