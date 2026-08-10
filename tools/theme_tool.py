# -*- coding: utf-8 -*-
"""界面风格定制工具：用户通过会话调整主题色/Logo（用户需求）。

写 config.json 的 ui_theme 节（唯一事实源），随后 WS 广播 theme_updated，
前端实时应用，无需刷新。Logo 只接受沙箱 uploads/ 下的文件名（粘贴图片
落盘的位置），防止引用任意路径。
"""
import os
import re
from typing import Any, Dict

from tools.base import BaseTool

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


class CustomizeThemeTool(BaseTool):
    name: str = "customize_theme"
    description: str = (
        "定制界面风格：主题色 primary_color、左侧菜单背景色 sidebar_color（均为十六进制"
        "如 #4CAF50）、Logo 与会话区背景图（logo= / chat_bg_image= uploads/ 下的文件名，"
        "如 paste_20260804_xxx.png；传 \"reset\" 恢复默认）。用户说「把主题色改成…」"
        "「侧边栏换成…色」「用这个图片当 logo/背景」等时使用。改动实时生效，无需刷新页面。"
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
                        "primary_color": {
                            "type": "string",
                            "description": "主题色十六进制值（如 #4CAF50）；'reset' 恢复默认蓝。留空表示不改。",
                        },
                        "sidebar_color": {
                            "type": "string",
                            "description": "左侧菜单背景色十六进制值（如 #1f2d3d）；'reset' 恢复默认。留空表示不改。",
                        },
                        "logo": {
                            "type": "string",
                            "description": "uploads/ 下的图片文件名（粘贴图片已自动保存到那里）；"
                                           "'reset' 恢复默认 Logo。留空表示不改。",
                        },
                        "chat_bg_image": {
                            "type": "string",
                            "description": "会话区背景图：uploads/ 下的图片文件名；'reset' 恢复无背景。留空表示不改。",
                        },
                        "glass": {
                            "type": "string",
                            "description": "毛玻璃效果：'on' 开 / 'off' 关（侧边栏、卡片半透明磨砂）。留空不改。",
                        },
                        "bordered": {
                            "type": "string",
                            "description": "描边风格：'on' 给气泡/卡片加边框 / 'off' 关。留空不改。",
                        },
                        "animations": {
                            "type": "string",
                            "description": "动画效果：'on' 开（消息入场、悬停过渡动画）/ 'off' 关。留空不改。",
                        },
                        "dark": {
                            "type": "string",
                            "description": "暗色模式：'on' 开（全站深色，Element Plus 暗色变量接管）/ 'off' 关。留空不改。",
                        },
                        "page_color": {
                            "type": "string",
                            "description": "页面底色十六进制值（如 #1a1a2e 深色、#fdf6f0 米色）；文字/卡片/边框色"
                                           "按亮度自动派生，深底色自动切换暗色控件。'reset' 恢复默认。留空不改。",
                        },
                        "decor": {
                            "type": "string",
                            "enum": ["none", "petals", "stars", "geometric"],
                            "description": "全局装饰图案：petals=飘落花瓣 / stars=星空 / geometric=几何纹理 / none=无。留空不改。",
                        },
                        "custom_css": {
                            "type": "string",
                            "description": "自定义 CSS（注入全站，<style id=\"custom-theme-css\">）。预设样式不够用时"
                                           "自由发挥：边框、圆角、阴影、装饰、动画等。会安全消毒（禁外部 URL/"
                                           "@import/javascript 等）；传 'reset' 清空。留空不改。",
                        },
                        "app_name": {
                            "type": "string",
                            "description": "左上角应用名称（如你的名字/人格名）；'reset' 恢复 Open-AGC。留空不改。",
                        },
                    },
                    "required": [],
                },
            },
        }

    _CSS_BLOCKLIST = ("javascript:", "expression(", "@import", "behavior:",
                      "-moz-binding", "<script", "</")
    _CSS_MAX_LEN = 20000

    def _sanitize_css(self, css: str):
        """自定义 CSS 安全消毒：阻断脚本化/外联/外部资源加载。
        url() 仅允许站内相对路径（/api/upload/、/static/ 等）与 data: 图片。
        返回 (ok, cleaned_or_error)。"""
        if len(css) > self._CSS_MAX_LEN:
            return False, f"CSS 超长（{len(css)} > {self._CSS_MAX_LEN} 字符）"
        low = css.lower()
        for bad in self._CSS_BLOCKLIST:
            if bad in low:
                return False, f"CSS 含被禁内容: {bad}"
        # 检查 url() 引用：禁外部 http(s)//协议相对
        for m in re.finditer(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", css, re.I):
            target = m.group(1).strip()
            if target.startswith(("http://", "https://", "//")):
                return False, f"CSS url() 引用外部地址: {target[:60]}"
            if target.startswith("data:") and not re.match(
                    r"data:image/(png|jpe?g|gif|webp|svg\+xml)", target, re.I):
                return False, "data: 仅允许图片类型"
        return True, css

    def execute(self, **kwargs) -> str:
        from api.config import load_config, save_config

        primary = (kwargs.get("primary_color") or "").strip()
        sidebar = (kwargs.get("sidebar_color") or "").strip()
        page_color = (kwargs.get("page_color") or "").strip()
        logo = (kwargs.get("logo") or "").strip()
        bg_image = (kwargs.get("chat_bg_image") or "").strip()
        toggles = {k: (kwargs.get(k) or "").strip().lower()
                   for k in ("glass", "bordered", "animations", "dark")}
        decor = (kwargs.get("decor") or "").strip().lower()
        custom_css = kwargs.get("custom_css")
        custom_css = custom_css if isinstance(custom_css, str) else ""
        app_name = (kwargs.get("app_name") or "").strip()
        if (not primary and not sidebar and not page_color and not logo and not bg_image
                and not any(toggles.values()) and not decor
                and not custom_css.strip() and not app_name):
            return ("Error: 至少提供一个参数（primary_color/sidebar_color/page_color/logo/"
                    "chat_bg_image/glass/bordered/animations/dark/decor/custom_css/app_name）。")

        changes = []
        for key, value in (("primary_color", primary), ("sidebar_color", sidebar),
                           ("page_color", page_color)):
            if not value:
                continue
            if value.lower() == "reset":
                changes.append((key, ""))
                continue
            if not _HEX_RE.match(value):
                return (f"Error: 颜色格式不正确（{value}），"
                        f"请给十六进制值，如 #4CAF50。")
            if not value.startswith("#"):
                value = "#" + value
            changes.append((key, value))

        for key, value in (("logo_file", logo), ("chat_bg_image", bg_image)):
            if not value:
                continue
            if value.lower() == "reset":
                changes.append((key, ""))
                continue
            # 只允许 uploads/ 下的纯文件名（粘贴图片落盘处）
            safe = os.path.basename(value)
            if safe != value or ".." in value:
                return f"Error: {key} 只接受 uploads/ 下的文件名（如 paste_xxx.png）。"
            try:
                from api.routes.uploads import _uploads_dir
                if not os.path.isfile(os.path.join(_uploads_dir(), safe)):
                    return (f"Error: uploads/{safe} 不存在。"
                            f"让用户先把图片粘贴到聊天窗口（会自动保存到 uploads/）。")
            except Exception:
                pass  # 目录判定失败不阻断（文件名形态已校验）
            changes.append((key, safe))

        # 开关类（毛玻璃/描边/动画）与装饰图案
        for key, value in toggles.items():
            if not value:
                continue
            if value in ("on", "true", "1", "开", "yes"):
                changes.append((key, True))
            elif value in ("off", "false", "0", "关", "reset", "no"):
                changes.append((key, False))
            else:
                return f"Error: {key} 只接受 on/off（收到 {value}）。"
        if decor:
            if decor not in ("none", "petals", "stars", "geometric"):
                return (f"Error: decor 只支持 none/petals/stars/geometric"
                        f"（收到 {decor}）。")
            changes.append(("decor", decor))
        # 自定义 CSS（消毒后存入；'reset' 清空）
        if custom_css.strip():
            if custom_css.strip().lower() == "reset":
                changes.append(("custom_css", ""))
            else:
                ok, cleaned = self._sanitize_css(custom_css)
                if not ok:
                    return f"Error: {cleaned}"
                changes.append(("custom_css", cleaned))
        # 应用名称（左上角；'reset' 恢复 Open-AGC）
        if app_name:
            if app_name.lower() == "reset":
                changes.append(("app_name", ""))
            else:
                if len(app_name) > 30:
                    return "Error: app_name 过长（>30 字符）。"
                changes.append(("app_name", app_name))

        try:
            config = load_config() or {}
            theme = config.get("ui_theme") or {}
            for key, value in changes:
                theme[key] = value
            config["ui_theme"] = theme
            save_config(config)
        except Exception as e:
            return f"Error: 保存主题配置失败: {e}"

        # 实时通知前端应用新主题（所有已连接客户端）
        try:
            from api.state import _broadcast_to_websockets
            _broadcast_to_websockets({
                "type": "theme_updated",
                "theme": changes and {k: v for k, v in changes} or {},
            })
        except Exception:
            pass  # 广播失败不影响持久化（刷新页面也会读到）

        parts = []
        labels = {"primary_color": "主题色", "sidebar_color": "侧边栏背景色",
                  "page_color": "页面底色",
                  "logo_file": "Logo", "chat_bg_image": "会话背景图",
                  "glass": "毛玻璃", "bordered": "描边", "animations": "动画",
                  "dark": "暗色模式",
                  "decor": "装饰图案", "custom_css": "自定义 CSS",
                  "app_name": "应用名称"}
        defaults = {"primary_color": "默认蓝", "sidebar_color": "默认深色", "page_color": "默认",
                    "logo_file": "默认熊猫图标", "chat_bg_image": "无背景"}
        for key, value in changes:
            label = labels.get(key, key)
            if key in ("glass", "bordered", "animations", "dark"):
                parts.append(f"{label} → {'开' if value else '关'}")
            elif key == "decor":
                parts.append(f"{label} → {value}")
            elif key == "custom_css":
                parts.append(f"{label} → {'已清空' if not value else f'已注入（{len(value)} 字符）'}")
            elif not value:
                parts.append(f"{label} → {defaults.get(key, '默认')}")
            elif key in ("logo_file", "chat_bg_image"):
                parts.append(f"{label} → uploads/{value}")
            else:
                parts.append(f"{label} → {value}")
        return "✅ 界面风格已更新：" + "；".join(parts) + "。已实时生效。"
