"""System-level API endpoints: version, self-upgrade, server logs."""
import os
from fastapi import APIRouter, HTTPException

from core.version import get_version

router = APIRouter()


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
