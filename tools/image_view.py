# -*- coding: utf-8 -*-
"""image_view 工具：读取本地图片并注入视觉上下文（视觉模型专用）。

图片回传路径（与 computer_control 截图同一通道）：工具结果文本中嵌入
[IMAGE_DATA:<data-url>] 标记，agent 主循环在工具结果处理后提取该标记，
把图片作为下一条 user 消息的 image_url 内容块注入对话。litellm 对
tool message 携带图片的支持有限，注入 user 消息是最稳定的方式。
"""
import base64
import io
import os
from typing import Any, Dict, Optional

from tools.base import BaseTool
from core.llm_client import IMAGE_MARKER, MIME_TYPES

SUPPORTED_EXTS = set(MIME_TYPES.keys())  # png jpg jpeg gif webp bmp

# 模型名启发式：litellm.supports_vision 对自定义渠道（如 kimi_code/k3）一律
# 返回 False，因此先按名字判断。先查视觉关键字，再查非视觉关键字。
_VISION_NAME_HINTS = (
    "vision", "gpt-4o", "gpt-4.1", "gpt-4-turbo", "chatgpt-4o", "o4-mini",
    "claude-3", "claude-sonnet", "claude-opus", "claude-haiku",
    "gemini", "-vl", "glm-4v", "glm-4.5v", "pixtral", "llava",
    "minicpm-v", "internvl", "kimi-latest", "k3",
)
_NON_VISION_NAME_HINTS = (
    "gpt-3.5", "deepseek-chat", "deepseek-reasoner", "deepseek-coder",
    "moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k",
    "kimi-k2", "qwq", "o1-mini", "o1-preview",
    "glm-4-air", "glm-4-flash", "glm-4-0520",
)

# 无 PIL 时的原始读取上限（有 PIL 时总会缩放，放宽到 50MB）
_RAW_SIZE_LIMIT = 10 * 1024 * 1024
_ABS_SIZE_LIMIT = 50 * 1024 * 1024


def is_vision_model(model_name: str, capability_flag: Optional[bool] = None) -> bool:
    """判断模型是否具备视觉能力。

    优先级：agent 显式能力标记（vision_capable）> 模型名启发式 >
    litellm.supports_vision（对未知渠道不可靠，仅作兜底）> 默认 False。
    """
    if capability_flag is not None:
        return bool(capability_flag)
    name = (model_name or "").lower()
    if not name:
        return False
    if any(h in name for h in _VISION_NAME_HINTS):
        return True
    if any(h in name for h in _NON_VISION_NAME_HINTS):
        return False
    try:
        import litellm
        return bool(litellm.supports_vision(model_name))
    except Exception:
        return False


def _resolve_model_name(agent) -> str:
    """从 agent 上下文取当前模型名。"""
    if agent is None:
        return ""
    model = getattr(agent, "model", "") or ""
    if not model:
        model = getattr(getattr(agent, "llm", None), "default_model", "") or ""
    return str(model)


def _config_vision_flag(config: dict, model_name: str) -> Optional[bool]:
    """从 config.json 读视觉能力覆盖配置。

    - ``vision_capable``：布尔值，显式开/关当前模型的视觉能力。
    - ``vision_models``：字符串列表（或逗号分隔字符串），按模型名包含匹配；
      命中即视为视觉模型。用于自定义渠道（如 qwen38/Qwen3.8-27B）无法被
      litellm.supports_vision 识别的场景。
    """
    if not isinstance(config, dict):
        return None
    if "vision_capable" in config and config.get("vision_capable") is not None:
        return bool(config.get("vision_capable"))
    vision_models = config.get("vision_models") or []
    if isinstance(vision_models, str):
        vision_models = [s.strip() for s in vision_models.split(",") if s.strip()]
    name = (model_name or "").lower()
    for entry in vision_models:
        e = str(entry).strip().lower()
        if e and e in name:
            return True
    return None


def _load_image_data_url(path: str, ext: str, max_size: int):
    """读取并（可选）缩放图片，返回 (data_url, orig_w, orig_h, scaled, note)。"""
    try:
        from PIL import Image
    except ImportError:
        Image = None

    if Image is None:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        mime = MIME_TYPES.get(ext, "image/png")
        return (f"data:{mime};base64,{b64}", None, None, False,
                "（PIL 不可用，未缩放，按原图读取）")

    with Image.open(path) as im:
        orig_w, orig_h = im.size
        fmt = (im.format or "").upper()
        scaled = max(orig_w, orig_h) > max_size
        if scaled:
            im.thumbnail((max_size, max_size), Image.LANCZOS)
        buf = io.BytesIO()
        if fmt == "JPEG" and im.mode in ("RGB", "L"):
            im.save(buf, format="JPEG", quality=85)
            mime = "image/jpeg"
        else:
            if im.mode in ("P", "CMYK"):
                im = im.convert("RGB")
            im.save(buf, format="PNG")
            mime = "image/png"
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:{mime};base64,{b64}", orig_w, orig_h, scaled, ""


class ImageViewTool(BaseTool):
    name: str = "image_view"
    description: str = (
        "读取本地图片并让模型直接看到图像内容（需视觉模型）。"
        "查看截图、照片、图表、UI 界面时用；max_size 控制长边缩放（默认 1024，省 token）。"
        "受沙箱限制。"
    )

    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "图片文件路径（绝对或相对）。"
                        },
                        "max_size": {
                            "type": "integer",
                            "description": "长边缩放上限（像素），默认 1024。传 0 表示不缩放。"
                        }
                    },
                    "required": ["path"]
                }
            }
        }

    def execute(self, **kwargs) -> str:
        import json
        from core.paths import get_data_path

        path = kwargs.get("path")
        if not path:
            return "Error: No file path provided."

        raw_max = kwargs.get("max_size", 1024)
        if raw_max is None:
            raw_max = 1024
        try:
            max_size = int(raw_max)
        except (TypeError, ValueError):
            return "Error: max_size must be an integer."
        if max_size < 0:
            return "Error: max_size must be >= 0 (0 means no scaling)."
        if max_size == 0:
            max_size = 1 << 30  # 0 = 不缩放

        # 加载配置（与 read_file 同一模式）
        config = {}
        config_path = get_data_path("config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
            except Exception:
                pass

        # 配置开关
        if config.get("image_view_enabled", True) is False:
            return ("Error: image_view 已被配置禁用（config.json: image_view_enabled=false）。"
                    "如需使用请开启该开关。")

        # 视觉能力检查：config 显式配置 > agent 能力标记 > 模型名启发式
        agent = kwargs.get("_agent_context")
        model_name = _resolve_model_name(agent)
        capability_flag = _config_vision_flag(config, model_name)
        if capability_flag is None:
            capability_flag = getattr(agent, "vision_capable", None) if agent is not None else None
        if not is_vision_model(model_name, capability_flag):
            return (f"Error: 当前模型 '{model_name or '未知'}' 未识别为视觉模型，无法查看图片。"
                    "请切换到具备视觉能力的模型（如 gpt-4o、claude-sonnet 等）后再试，"
                    "或改用 read_file / execute_python 以文本方式处理该文件。")

        # 沙箱限制（与 read_file 同一模式）
        if config.get("sandbox_mode", True):
            whitelist = kwargs.get("_session_whitelist", None)
            self.check_sandbox(path, config=config, session_whitelist=whitelist)

        if not os.path.isfile(path):
            return f"Error: File not found: {path}"

        ext = os.path.splitext(path)[1].lower().lstrip(".")
        if ext not in SUPPORTED_EXTS:
            return (f"Error: 不支持的图片格式 '.{ext}'。"
                    f"支持: {', '.join(sorted(SUPPORTED_EXTS))}。")

        file_size = os.path.getsize(path)
        if file_size > _ABS_SIZE_LIMIT:
            return (f"Error: 图片文件过大（{file_size // (1024 * 1024)}MB > 50MB），"
                    "请先压缩后再查看。")

        try:
            from PIL import Image as _PILImage  # noqa: F401
            has_pil = True
        except ImportError:
            has_pil = False
        if not has_pil and file_size > _RAW_SIZE_LIMIT:
            return ("Error: PIL/Pillow 不可用且图片超过 10MB，无法缩放，拒绝读取。"
                    "请安装 Pillow 或先压缩图片。")

        try:
            data_url, orig_w, orig_h, scaled, note = _load_image_data_url(path, ext, max_size)
        except Exception as e:
            return f"Error: 无法读取图片 {path}: {e}"

        if orig_w is not None:
            scale_info = f"，已缩放至长边 {max_size}px" if scaled else "，未缩放"
            size_info = f"（{orig_w}x{orig_h}{scale_info}）"
        else:
            size_info = ""
        return (
            f"图片已加载: {path} {size_info}{note}\n"
            f"图像已注入视觉上下文，请直接查看并分析图像内容。\n"
            f"{IMAGE_MARKER}{data_url}]"
        )
