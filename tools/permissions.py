"""
Permission Manager — detect destructive shell commands and enforce auth policies.
"""
import re
import os
import json
from typing import Optional, Tuple


# ── Destructive command patterns ──
DESTRUCTIVE_PATTERNS = [
    # File system destruction
    (r'\brm\s+-rf\s+/', 'rm -rf / (删除根目录)', 'fs_destroy'),
    (r'\brm\s+-rf\s+\$HOME', 'rm -rf $HOME (删除用户目录)', 'fs_destroy'),
    (r'\brm\s+-rf\s+~', 'rm -rf ~ (删除用户目录)', 'fs_destroy'),
    (r'(?:^|[|&;]\s*)rm\s+-rf\s+\S*(?:System32|Windows|etc|bin|usr|home|root)', '删除系统关键目录', 'fs_destroy'),
    (r'\bdel\s+/[fsq]\s+C:\\', 'del C:\\ (Windows系统盘)', 'fs_destroy'),
    (r'\bformat\s+C:', 'format C: (格式化系统盘)', 'fs_destroy'),
    (r'\bdd\s+if=.*\s+of=/dev/sd', 'dd 覆写磁盘', 'fs_destroy'),
    # Git destructive
    (r'\bgit\s+push\s+--force\b', 'git push --force (强制推送)', 'git_destructive'),
    (r'\bgit\s+push\s+-f\b', 'git push -f (强制推送)', 'git_destructive'),
    (r'\bgit\s+reset\s+--hard\b', 'git reset --hard (硬重置)', 'git_destructive'),
    (r'\bgit\s+clean\s+-[fdx]+', 'git clean (清理未追踪文件)', 'git_destructive'),
    # Package uninstall
    (r'\bpip\s+uninstall\s+-y\s+\S+', 'pip uninstall (卸载包)', 'pkg_uninstall'),
    (r'\bnpm\s+uninstall\s+-g\b', 'npm uninstall -g (全局卸载)', 'pkg_uninstall'),
    # Network dangerous
    (r'\bcurl.*\|\s*(?:ba)?sh\b', 'curl | sh (管道执行远程脚本)', 'network_unsafe'),
    (r'\bwget.*\|\s*(?:ba)?sh\b', 'wget | sh (管道执行远程脚本)', 'network_unsafe'),
    # Docker destructive
    (r'\bdocker\s+rm\s+-f\s+\$\(.*\)', 'docker rm -f 批量删除容器', 'docker_destroy'),
    (r'\bdocker\s+system\s+prune\s+-af?\b', 'docker system prune (清理全部)', 'docker_destroy'),
    # Database destructive
    (r'\bDROP\s+(?:TABLE|DATABASE)\b', 'DROP TABLE/DATABASE', 'db_destroy'),
    (r'\bTRUNCATE\s+(?:TABLE\s+)?\S+', 'TRUNCATE TABLE', 'db_destroy'),
]


def check_command_permission(command: str, config: dict = None,
                              session_id: int = None) -> Tuple[bool, str]:
    """Check if a shell command needs user authorization.

    Returns:
        (allowed, message) — (True, "") if OK, (False, reason) if blocked.
    """
    cmd_lower = command.lower().strip()

    for pattern, description, category in DESTRUCTIVE_PATTERNS:
        if re.search(pattern, cmd_lower, re.IGNORECASE):
            # Check if already authorized
            if _is_authorized(category, config, session_id):
                return (True, "")

            return (False,
                f"⛔ 敏感操作: {description}\n\n"
                f"命令: {command[:200]}\n"
                f"类别: {category}\n\n"
                f"该操作可能造成不可逆的破坏。如需执行，请:\n"
                f"1. 在 Settings > 沙箱授权管理中确认\n"
                f"2. 或者在 config.json 的 tool_permissions.{category} 中添加 'session_allow'\n"
                f"3. 或者使用 ask_user_question 工具请求用户明确批准"
            )

    return (True, "")


def _is_authorized(category: str, config: dict = None,
                   session_id: int = None) -> bool:
    """Check if a command category is already authorized."""
    if not config:
        return False

    perms = config.get("tool_permissions", {})
    if isinstance(perms, str):
        try:
            perms = json.loads(perms)
        except Exception:
            return False

    cat_perms = perms.get(category, {})
    if not isinstance(cat_perms, dict):
        return False

    # Check for any allow
    for action, status in cat_perms.items():
        if status in ("allow", "session_allow"):
            return True

    return False


def grant_permission(category: str, action: str, status: str = "session_allow",
                     config_path: str = None):
    """Grant permission for a command category. Persists to config.json."""
    if not config_path:
        from core.paths import get_data_path
        config_path = get_data_path("config.json")

    config = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            pass

    perms = config.get("tool_permissions", {})
    if isinstance(perms, str):
        try:
            perms = json.loads(perms)
        except Exception:
            perms = {}

    if category not in perms:
        perms[category] = {}
    perms[category][action] = status

    config["tool_permissions"] = perms
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
