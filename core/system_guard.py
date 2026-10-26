"""系统完整性防护：查询放行，系统级变更确认，不可逆操作硬拒。"""
import ast
import hashlib
import json
import re
import shlex
import subprocess
from typing import Optional


ONE_SHOT_CATEGORIES = frozenset({
    "pkg_remove", "system_write", "system_service", "pkexec",
    "plugin_install", "mcp_add", "config_risk", "sandbox_off",
})


def approval_operation(tool_name: str, payload: dict) -> str:
    """生成稳定且不泄露参数明文的单次审批操作标识。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{tool_name}:{digest}"


def require_one_shot_approval(operation: str, session_id: int = None,
                              tool_name: str = "", category: str = "",
                              description: str = "") -> None:
    """消费当前操作的单次授权；未获授权时抛出 SandboxBlocked。"""
    if session_id is not None:
        try:
            from api.state import _session_permission_once
            once = _session_permission_once.get(session_id)
            if once and operation in once:
                once.discard(operation)
                return
        except Exception:
            pass
    from tools.base import SandboxBlocked
    raise SandboxBlocked(operation, sandbox_dir="permission", tool_name=tool_name,
                         category=category, description=description)
DENY_REASON = "该操作可直接导致系统不可用，已按系统保护策略禁止"
PKG_REMOVE_REASON = "软件包移除可能破坏系统依赖，将移除的包清单见下方模拟预览"
SYSTEM_PATHS = ("/etc", "/usr", "/lib", "/lib64", "/boot", "/var/lib/dpkg")


def _result(action: str, category: Optional[str] = None,
            reason: str = "", preview: Optional[str] = None) -> dict:
    return {"action": action, "category": category,
            "reason": reason, "preview": preview}


def _is_system_path(path: str) -> bool:
    path = path.strip().strip("\"'").rstrip()
    return any(path == root or path.startswith(root + "/") for root in SYSTEM_PATHS)


def _package_preview(command: str) -> str:
    """只运行包管理器自带的模拟模式，绝不把预览变成真实卸载。"""
    simulated = command
    if re.search(r"\bapt-get\b", command, re.IGNORECASE):
        simulated = re.sub(r"\bapt-get\b", "apt-get -s", command,
                           count=1, flags=re.IGNORECASE)
    elif re.search(r"\bapt(?:itude)?\b", command, re.IGNORECASE):
        simulated = re.sub(r"\bapt(?:itude)?\b", lambda m: m.group(0) + " -s",
                           command, count=1, flags=re.IGNORECASE)
    elif re.search(r"\bdpkg\b", command, re.IGNORECASE):
        simulated = re.sub(r"\bdpkg\b", "dpkg --dry-run", command,
                           count=1, flags=re.IGNORECASE)
    try:
        from core.process import NO_WINDOW_KW
        completed = subprocess.run(
            simulated, shell=True, capture_output=True, text=True,
            timeout=60, **NO_WINDOW_KW)
        output = (completed.stdout or "") + (completed.stderr or "")
        if completed.returncode:
            output = f"模拟预览退出码 {completed.returncode}\n" + output
        return output[-4000:] if output else "模拟预览未产生输出"
    except Exception as exc:
        return f"模拟预览失败：{exc}"[:4000]


def _denied_command(command: str) -> bool:
    # 不依赖 ^ 锚点：sudo、管道和 && 后的命令同样受检。
    patterns = (
        r"(?:^|[|&;\s])(?:sudo\s+)?(?:shutdown|reboot|poweroff|halt)\b",
        r"(?:^|[|&;\s])(?:sudo\s+)?init\s+[06](?:\s|$)",
        r"(?:^|[|&;\s])(?:sudo\s+)?mkfs(?:\.[\w-]+)?\b",
        r"\bdd\b[^|&;\n]*\bof\s*=\s*[\"']?/dev/",
        r"\b(?:pkill|killall)\b[^|&;\n]*(?:xorg|lightdm|dde(?:-session)?)\b",
        r"\bdpkg\b[^|&;\n]*\s--force-[\w-]+",
    )
    if any(re.search(pattern, command, re.IGNORECASE) for pattern in patterns):
        return True
    for match in re.finditer(r"\b(chmod|chown)\b([^|&;\n]*)", command, re.IGNORECASE):
        args = match.group(2)
        recursive = re.search(r"(?:^|\s)-[^\s]*R[^\s]*", args)
        target = re.search(
            r"(?:^|\s)[\"']?(?:/|/etc(?:/|\s|$)|/usr(?:/|\s|$)|/lib(?:64)?(?:/|\s|$)|/boot(?:/|\s|$))",
            args)
        if recursive and target:
            return True
    return False


def _package_remove(command: str) -> bool:
    # apt-cache/dpkg-query 也是独立查询命令，不能因包名叫 remove 而误判。
    if re.search(r"\bapt(?:-get)?(?![\w-])[^|&;\n]*\s(?:remove|purge|autoremove)\b",
                 command, re.IGNORECASE):
        return True
    if re.search(r"\baptitude(?![\w-])[^|&;\n]*\s(?:remove|purge|autoremove)\b",
                 command, re.IGNORECASE):
        return True
    return bool(re.search(r"\bdpkg(?![\w-])[^|&;\n]*(?:\s-P(?:\s|$)|\s--purge(?:\s|=|$))",
                          command, re.IGNORECASE))


def _service_change(command: str) -> bool:
    for match in re.finditer(
            r"\bsystemctl\s+(?:stop|disable|mask)\s+([^\s|&;]+)", command,
            re.IGNORECASE):
        service = match.group(1).strip("\"'").lower()
        if service not in ("open-agc", "open-agc.service", "uvicorn", "uvicorn.service"):
            return True
    return False


def _system_write(command: str) -> bool:
    redirect = re.search(
        r">>?\s*[\"']?\s*(/(?:etc|usr|lib64|lib|boot)(?:/|\b)|/var/lib/dpkg(?:/|\b))",
        command, re.IGNORECASE)
    if redirect:
        return True
    # cp/mv 等只检查目标，读取或备份系统配置到用户目录不弹窗。
    for segment in re.split(r"[|&;\n]+", command):
        try:
            words = [word.strip("\"'") for word in shlex.split(segment, posix=True)]
        except ValueError:
            words = segment.strip().split()
        if not words:
            continue
        try:
            command_index = next(i for i, word in enumerate(words)
                                 if word.lower() in {"cp", "mv", "tee", "rsync", "install"})
        except StopIteration:
            continue
        name = words[command_index].lower()
        args = words[command_index + 1:]
        if not args:
            continue
        targets = args if name == "tee" else args[-1:]
        for target in targets:
            if not target.startswith("-") and _is_system_path(target):
                return True
    return False


def check_system_command(cmd: str) -> dict:
    """返回 allow/approve/deny；approve 类别一律只能逐条授权。"""
    command = cmd or ""
    if _denied_command(command):
        return _result("deny", reason=DENY_REASON)
    if _package_remove(command):
        return _result("approve", "pkg_remove", PKG_REMOVE_REASON,
                       _package_preview(command))
    if _service_change(command):
        return _result("approve", "system_service", "停止、禁用或屏蔽系统服务可能影响系统正常运行")
    if _system_write(command):
        return _result("approve", "system_write", "写入系统目录可能覆盖关键系统库或配置")
    if re.search(r"(?:^|[|&;\s])pkexec(?:\s|$)", command, re.IGNORECASE):
        return _result("approve", "pkexec", "pkexec 将以管理员权限执行命令")
    return _result("allow")


def _literal(value: str) -> Optional[str]:
    try:
        parsed = ast.literal_eval(value)
        return parsed if isinstance(parsed, str) else None
    except (ValueError, SyntaxError):
        return None


def _extract_shell_commands(code: str) -> list:
    commands = []
    for match in re.finditer(r"\bos\.system\s*\(\s*((?:'[^']*')|(?:\"[^\"]*\"))", code):
        value = _literal(match.group(1))
        if value:
            commands.append(value)
    for match in re.finditer(r"\bsubprocess\.\w+\s*\(\s*(\[[^\]]*\]|(?:'[^']*')|(?:\"[^\"]*\"))", code):
        raw = match.group(1)
        if raw.startswith("["):
            parts = []
            for item in re.finditer(r"'[^']*'|\"[^\"]*\"", raw):
                value = _literal(item.group(0))
                if value is not None:
                    parts.append(value)
            if parts:
                commands.append(" ".join(parts))
        else:
            value = _literal(raw)
            if value:
                commands.append(value)
    return commands


def check_python_code(code: str) -> dict:
    """轻量扫描常见字面量调用；动态拼接无法提取时不强行拦截。"""
    source = code or ""
    shell_results = [check_system_command(command)
                     for command in _extract_shell_commands(source)]
    if any(result["action"] == "deny" for result in shell_results):
        return next(result for result in shell_results if result["action"] == "deny")
    if re.search(r"\bshutil\.rmtree\s*\(\s*[\"'](/(?:etc|usr|lib64|lib|boot|var/lib/dpkg)?(?:/[^'\"]*)?)[\"']", source):
        return _result("deny", reason=DENY_REASON)
    if re.search(r"\bos\.kill\s*\(", source) and re.search(
            r"\b(?:xorg|lightdm|dde(?:-session)?)\b", source, re.IGNORECASE):
        return _result("deny", reason=DENY_REASON)
    if any(result["action"] == "approve" for result in shell_results):
        return next(result for result in shell_results if result["action"] == "approve")
    for match in re.finditer(
            r"\bopen\s*\(\s*[\"']([^'\"]+)[\"']\s*,\s*[\"']([^'\"]+)[\"']", source):
        if _is_system_path(match.group(1)) and any(flag in match.group(2) for flag in "wax+"):
            return _result("approve", "system_write", "Python 代码将写入系统目录")
    for match in re.finditer(
            r"\bshutil\.(?:copy(?:2|file|tree)?|move)\s*\(\s*[\"'][^'\"]+[\"']\s*,\s*[\"']([^'\"]+)[\"']",
            source):
        if _is_system_path(match.group(1)):
            return _result("approve", "system_write", "Python 代码将写入系统目录")
    return _result("allow")
