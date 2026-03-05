import os
import time
from typing import Any, Dict, Optional
from pydantic import Field

from tools.base import BaseTool

class ComputerTool(BaseTool):
    name: str = "computer_control"
    description: str = (
        "Control the physical computer mouse and keyboard. "
        "Allows you to click, move the mouse, type text, and press keys."
    )

    def __init__(self, **data):
        super().__init__(**data)
        # Import pyautogui lazily to avoid issues if not installed or running headlessly
        global pyautogui
        import pyautogui
        # Failsafe: moving mouse to corner will abort
        pyautogui.FAILSAFE = True
        # Add a slight delay after every pyautogui call
        pyautogui.PAUSE = 0.5 

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
                            "description": "The action to perform: 'mouse_move', 'mouse_click', 'type_text', 'press_key', 'hotkey', 'screenshot'."
                        },
                        "x": {
                            "type": "integer",
                            "description": "X coordinate for mouse actions."
                        },
                        "y": {
                            "type": "integer",
                            "description": "Y coordinate for mouse actions."
                        },
                        "text": {
                            "type": "string",
                            "description": "Text to type when action is 'type_text'."
                        },
                        "key": {
                            "type": "string",
                            "description": "Key to press when action is 'press_key' (e.g., 'enter', 'tab', 'esc')."
                        },
                        "keys": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of keys for hotkey combinations (e.g., ['command', 'c'])."
                        }
                    },
                    "required": ["action"]
                }
            }
        }

    def execute(self, **kwargs) -> str:
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
                # Save screenshot to a default location for now
                screenshot_path = os.path.abspath("screenshot.png")
                pyautogui.screenshot(screenshot_path)
                return f"Screenshot saved to {screenshot_path}"
                
            else:
                return f"Error: Unknown action '{action}'"
                
        except Exception as e:
            return f"Error executing computer control ({action}): {str(e)}"
