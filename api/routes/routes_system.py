"""System-level API endpoints: version, self-upgrade, server logs."""
import os
import json
from fastapi import APIRouter, HTTPException

from core.version import get_version

router = APIRouter()


@router.get("/api/theme")
async def get_theme():
    """界面主题配置（ui_theme 节）：前端启动与 theme_updated 广播后拉取。
    图片字段转可访问 URL（/api/upload/<名>，粘贴图片落盘处）。"""
    from api.config import load_config
    theme = (load_config() or {}).get("ui_theme") or {}

    def _img_url(name):
        name = name or ""
        if name and ".." not in name and "/" not in name:
            return f"/api/upload/{name}"
        return ""

    return {
        "primary_color": theme.get("primary_color") or "",
        "sidebar_color": theme.get("sidebar_color") or "",
        "page_color": theme.get("page_color") or "",
        "logo_url": _img_url(theme.get("logo_file")),
        "chat_bg_url": _img_url(theme.get("chat_bg_image")),
        "app_name": theme.get("app_name") or "",
        "glass": bool(theme.get("glass")),
        "bordered": bool(theme.get("bordered")),
        "animations": bool(theme.get("animations")),
        "dark": bool(theme.get("dark")),
        "decor": theme.get("decor") or "none",
        "custom_css": theme.get("custom_css") or "",
    }


def _read_upload_b64(name: str):
    """读 uploads/ 下的文件为 (data_url, None) 或 (None, 原因)。限图片、5MB。"""
    import base64
    if not name or ".." in name or "/" in name:
        return None, "bad name"
    try:
        from api.routes.uploads import _uploads_dir
        path = os.path.join(_uploads_dir(), name)
        if not os.path.isfile(path):
            return None, "not found"
        if os.path.getsize(path) > 5 * 1024 * 1024:
            return None, "too large"
        ext = os.path.splitext(name)[1].lower().lstrip(".") or "png"
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
                "webp": "image/webp", "svg": "image/svg+xml"}.get(ext, "image/png")
        with open(path, "rb") as f:
            return f"data:{mime};base64," + base64.b64encode(f.read()).decode(), None
    except Exception as e:
        return None, str(e)


@router.get("/api/theme/export")
async def export_theme():
    """导出主题包 JSON：含内嵌的 Logo/背景图（base64 data URL）与自定义 CSS，
    分享给好友后对方导入即可完整还原（用户反馈：此前只导出文件名，图丢了）。"""
    from api.config import load_config
    theme = (load_config() or {}).get("ui_theme") or {}
    pkg = {k: theme.get(k) for k in (
        "primary_color", "sidebar_color", "page_color", "logo_file", "chat_bg_image",
        "app_name", "glass", "bordered", "animations", "dark", "decor", "custom_css")}
    if theme.get("logo_file"):
        data_url, err = _read_upload_b64(theme["logo_file"])
        if data_url:
            pkg["logo_data"] = data_url
    if theme.get("chat_bg_image"):
        data_url, err = _read_upload_b64(theme["chat_bg_image"])
        if data_url:
            pkg["chat_bg_data"] = data_url
    return {"name": theme.get("app_name") or "Open-AGC 主题",
            "format": "open-agc-theme@1",
            "theme": pkg}


def _save_upload_b64(data_url: str, prefix: str):
    """把 data URL 图片写入 uploads/，返回文件名；失败返回 None。"""
    import base64
    import re as _re
    import time as _time
    m = _re.match(r"data:image/(png|jpe?g|gif|webp|svg\+xml);base64,(.+)",
                  data_url, _re.I | _re.S)
    if not m:
        return None
    try:
        raw = base64.b64decode(m.group(2))
        if not raw or len(raw) > 5 * 1024 * 1024:
            return None
        ext = {"jpeg": "jpg", "svg+xml": "svg"}.get(m.group(1).lower(),
                                                    m.group(1).lower())
        name = f"{prefix}_{int(_time.time())}.{ext}"
        from api.routes.uploads import _uploads_dir
        target_dir = _uploads_dir()
        os.makedirs(target_dir, exist_ok=True)
        with open(os.path.join(target_dir, name), "wb") as f:
            f.write(raw)
        return name
    except Exception:
        return None


# ui_theme 节的全部合法字段（POST /api/theme replace 模式的全量集）
_THEME_FIELDS = ("primary_color", "sidebar_color", "page_color", "logo_file",
                 "chat_bg_image", "app_name", "glass", "bordered", "animations",
                 "dark", "decor", "custom_css")


@router.post("/api/theme")
async def save_theme(body: dict):
    """保存界面主题（设置页导入/主题市场应用共用）。

    mode=merge（默认）：只改提供的字段——市场预设是局部的，不该抹掉用户
    的 Logo/自定义 CSS（生产实证：应用预设后 logo_file 被清空）；
    mode=replace：整节替换（「恢复默认」用）。
    校验与 customize_theme 工具同一口径：颜色十六进制、开关布尔、decor 枚举、
    图片限 uploads/ 文件名、custom_css 过工具同款消毒。"""
    from api.config import load_config, save_config
    from tools.theme_tool import CustomizeThemeTool, _HEX_RE

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body 必须是对象")
    mode = "replace" if body.get("mode") == "replace" else "merge"
    theme_in = body.get("theme", body)  # 允许 {theme:{...}} 或直接平铺
    if not isinstance(theme_in, dict):
        raise HTTPException(status_code=400, detail="theme 必须是对象")
    # merge 模式下只处理「出现过的」字段；replace 模式全量落默认值
    provided = set(theme_in.keys()) if mode == "merge" else set(
        _THEME_FIELDS) | {"logo_data", "chat_bg_data"}

    existing = (load_config() or {}).get("ui_theme") or {}
    out = dict(existing) if mode == "merge" else {}
    for key in ("primary_color", "sidebar_color", "page_color"):
        if key not in provided:
            continue
        v = str(theme_in.get(key) or "").strip()
        if v and not _HEX_RE.match(v):
            raise HTTPException(status_code=400, detail=f"{key} 颜色格式不正确: {v}")
        out[key] = ("#" + v) if v and not v.startswith("#") else v
    # 内嵌图片数据（导入主题包）：优先于文件名引用，解码写入 uploads/
    for data_key, file_key, prefix in (
            ("logo_data", "logo_file", "theme_logo"),
            ("chat_bg_data", "chat_bg_image", "theme_bg")):
        data_url = theme_in.get(data_key)
        if isinstance(data_url, str) and data_url.startswith("data:"):
            saved_name = _save_upload_b64(data_url, prefix)
            if not saved_name:
                raise HTTPException(status_code=400,
                                    detail=f"{data_key} 图片数据无效或过大")
            theme_in[file_key] = saved_name
            provided.add(file_key)
    for key in ("logo_file", "chat_bg_image"):
        if key not in provided:
            continue
        v = str(theme_in.get(key) or "").strip()
        if v and (os.path.basename(v) != v or ".." in v):
            raise HTTPException(status_code=400, detail=f"{key} 只接受 uploads/ 下的文件名")
        out[key] = v
    if "app_name" in provided:
        app_name = str(theme_in.get("app_name") or "").strip()
        if len(app_name) > 30:
            raise HTTPException(status_code=400, detail="app_name 过长（>30 字符）")
        out["app_name"] = app_name
    for key in ("glass", "bordered", "animations", "dark"):
        if key in provided:
            out[key] = bool(theme_in.get(key))
    if "decor" in provided:
        decor = str(theme_in.get("decor") or "none").strip().lower()
        if decor not in ("none", "petals", "stars", "geometric"):
            raise HTTPException(status_code=400, detail=f"decor 非法: {decor}")
        out["decor"] = decor
    if "custom_css" in provided:
        css = str(theme_in.get("custom_css") or "")
        if css.strip():
            ok, cleaned = CustomizeThemeTool()._sanitize_css(css)
            if not ok:
                raise HTTPException(status_code=400, detail=f"custom_css 被拒: {cleaned}")
            out["custom_css"] = cleaned
        else:
            out["custom_css"] = ""

    config = load_config() or {}
    config["ui_theme"] = out
    save_config(config)
    try:
        from api.state import _broadcast_to_websockets
        _broadcast_to_websockets({"type": "theme_updated", "theme": out})
    except Exception:
        pass
    return {"status": "success", "theme": out}


# 主题市场内置预设（远程索引不可达时的兜底；与 marketplace/themes.json 同步维护）
_THEME_PRESETS = [
    {"name": "默认蓝", "desc": "Element Plus 经典蓝，清爽克制",
     "theme": {"primary_color": "", "sidebar_color": "", "dark": False, "decor": "none"}},
    {"name": "猫娘粉", "desc": "柔和粉 + 玫瑰紫侧边栏，飘落花瓣",
     "theme": {"primary_color": "#E88FB0", "sidebar_color": "#8E5B78", "dark": False, "decor": "petals"}},
    {"name": "暗夜黑", "desc": "全站深色 + 靛蓝主题色，星空点缀",
     "theme": {"primary_color": "#6366f1", "sidebar_color": "#111827",
               "dark": True, "decor": "stars"}},
    {"name": "薄荷绿", "desc": "清新绿 + 浅色侧边栏，几何纹理",
     "theme": {"primary_color": "#10b981", "sidebar_color": "#d1f2e5", "dark": False, "decor": "geometric"}},
    {"name": "暖阳橙", "desc": "温暖橙 + 琥珀侧边栏，轻快明亮",
     "theme": {"primary_color": "#f59e0b", "sidebar_color": "#7c2d12", "dark": False, "decor": "none"}},
    {"name": "紫藤梦境", "desc": "紫罗兰 + 毛玻璃质感，梦幻花瓣",
     "theme": {"primary_color": "#8b5cf6", "sidebar_color": "#4c1d95",
               "dark": False, "glass": True, "decor": "petals"}},
]


@router.get("/api/theme/market")
async def theme_market():
    """主题市场：内置预设 + 远程索引（config.theme_market_url 或默认仓库
    marketplace/themes.json）合并，远程失败时仅用内置预设。"""
    from api.config import load_config
    url = (load_config() or {}).get("theme_market_url") or (
        "https://raw.githubusercontent.com/deanwinchester/open-agc/main/marketplace/themes.json")
    remote = []
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for item in (data.get("themes") or []):
            if isinstance(item, dict) and item.get("name") and isinstance(item.get("theme"), dict):
                remote.append({"name": item["name"], "desc": item.get("desc", ""),
                               "author": item.get("author", ""), "theme": item["theme"],
                               "source": "market"})
    except Exception as e:
        print(f"[Theme] Market fetch failed: {e}")
    presets = [dict(p, source="preset") for p in _THEME_PRESETS]
    return {"themes": presets + remote}


# ── Version / Upgrade API ──

@router.get("/api/version")
async def get_api_version():
    import sys as _sys
    from core.auto_upgrade import AutoUpgrader, get_channel
    upgrader = AutoUpgrader()
    current = get_version()
    latest = upgrader.fetch_latest_release()
    return {
        "current": current,
        "latest": latest or current,
        # 必须比较版本大小而非仅判不等：本地版本高于线上（如预发布开发中）时不提示升级
        "update_available": bool(latest and upgrader.is_upgrade_available()),
        # 部署形态（desktop/docker/source）与平台，供前端按通道显示升级文案
        "channel": get_channel(),
        "platform": _sys.platform,
    }


@router.post("/api/upgrade")
async def upgrade_server():
    import asyncio
    from core.auto_upgrade import AutoUpgrader
    upgrader = AutoUpgrader()
    # perform_upgrade 是同步下载+安装（分钟级），移出事件循环
    success = await asyncio.get_running_loop().run_in_executor(None, upgrader.perform_upgrade)
    if not success:
        raise HTTPException(status_code=500, detail=upgrader.last_message or "Upgrade failed")
    return {
        "status": "ok",
        "message": upgrader.last_message or "Upgrade completed",
        # desktop Windows 升级后主进程会自动退出并由 apply_update.bat 重启
        "restart": upgrader.restart_required,
        "channel": upgrader.channel,
    }


# ── Logs API ──

@router.get("/api/logs")
async def get_server_logs(lines: int = 200):
    """Return the last N lines of the agent log file."""
    from api.state import _AGENT_LOG_FILE
    log_path = _AGENT_LOG_FILE
    if not log_path or not os.path.exists(log_path):
        return {"lines": [], "total": 0}
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        all_lines = content.split("\n")
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {"lines": tail, "total": len(all_lines)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
