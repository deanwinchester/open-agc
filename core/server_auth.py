# -*- coding: utf-8 -*-
"""中央服务器账号登录态（zxs 产品线）。

配置了 ``config.json`` 的 ``server_url`` 时启用：客户端所有访问需先通过
中央服务器（中新社智能体服务平台）账号登录，拿到的 JWT 同时作为内网大模型
网关（<server_url>/v1）的调用凭据——服务端按账号统计 token 用量、限额。

token 策略（与服务端约定）：有效期 180 天，90 天不活跃失效（服务端按
auth_token.last_seen_at 判定），临期（<30 天）调用方应主动 refresh 换新。

本模块只做客户端侧的存储与调用：token 存 ``data/server_auth.json``
（0600），不落 config.json（避免被设置页整体替换/掩码逻辑误伤）。
"""
import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

from core.paths import get_data_path

AUTH_FILE = get_data_path("server_auth.json")

# 临期阈值：剩余有效期不足 30 天时主动换新（服务端 token 总长 180 天）
REFRESH_THRESHOLD_SECONDS = 30 * 24 * 3600
_HTTP_TIMEOUT = 10

_lock = threading.Lock()
_cache = None          # dict | None：文件内容缓存
_cache_mtime = 0.0     # 缓存对应的文件 mtime


def server_url() -> str:
    """中央服务器地址（config.json 的 server_url）；未配置返回 ""。"""
    try:
        from api.config import load_config
        return str((load_config() or {}).get("server_url") or "").strip().rstrip("/")
    except Exception:
        return ""


def enabled() -> bool:
    """是否启用中央服务器账号体系（zxs 品牌构建预置 server_url）。"""
    return bool(server_url())


# ── token 文件读写 ────────────────────────────────────────────────────────

def _load() -> Optional[dict]:
    global _cache, _cache_mtime
    with _lock:
        try:
            mtime = os.path.getmtime(AUTH_FILE)
        except OSError:
            _cache, _cache_mtime = None, 0.0
            return None
        if _cache is not None and mtime == _cache_mtime:
            return _cache
        try:
            with open(AUTH_FILE, "r", encoding="utf-8") as f:
                _cache = json.load(f)
            _cache_mtime = mtime
        except Exception:
            _cache = None
        return _cache


def _save(data: Optional[dict]) -> None:
    global _cache, _cache_mtime
    with _lock:
        _cache, _cache_mtime = data, 0.0
        if data is None:
            try:
                os.remove(AUTH_FILE)
            except OSError:
                pass
            return
        os.makedirs(os.path.dirname(AUTH_FILE), exist_ok=True)
        tmp = AUTH_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, AUTH_FILE)
        try:
            os.chmod(AUTH_FILE, 0o600)
        except OSError:
            pass
        try:
            _cache_mtime = os.path.getmtime(AUTH_FILE)
        except OSError:
            pass


def _decode_exp(token: str) -> int:
    """从自研 JWT（base64url(payload).sig）解析 exp；失败返回 0。"""
    try:
        payload_b64 = token.split(".", 1)[0]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("ascii")))
        return int(payload.get("exp") or 0)
    except Exception:
        return 0


# ── 中央服务器 HTTP 调用 ──────────────────────────────────────────────────

class ServerAuthError(Exception):
    """登录/刷新失败的友好错误（message 可直接上屏）。"""


def _read_error(e: urllib.error.HTTPError) -> str:
    try:
        return json.loads(e.read().decode("utf-8")).get("error", "")
    except Exception:
        return ""


def _post(path: str, body: Optional[dict] = None, token: Optional[str] = None) -> dict:
    url = server_url() + path
    data = json.dumps(body or {}).encode("utf-8") if body is not None else b""
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = _read_error(e)
        if e.code == 401:
            raise ServerAuthError(detail or "用户名或密码错误")
        raise ServerAuthError(detail or f"服务器返回 HTTP {e.code}")
    except Exception:
        raise ServerAuthError("无法连接中新社智能体服务平台，请检查网络后重试")


def _get(path: str, token: str) -> dict:
    req = urllib.request.Request(server_url() + path)
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ServerAuthError(_read_error(e) or f"服务器返回 HTTP {e.code}")
    except Exception:
        raise ServerAuthError("无法连接中新社智能体服务平台，请检查网络后重试")


def _require_token() -> str:
    tok = get_token()
    if not tok:
        raise ServerAuthError("尚未登录")
    return tok


# ── 对外接口 ─────────────────────────────────────────────────────────────

def status() -> dict:
    """登录态快照：{enabled, logged_in, username, display_name}。"""
    info = _load()
    logged_in = bool(
        info and info.get("token") and int(info.get("exp") or 0) > time.time()
    )
    return {
        "enabled": enabled(),
        "logged_in": logged_in,
        "username": (info or {}).get("username") or "",
        "display_name": (info or {}).get("display_name") or "",
    }


def is_logged_in() -> bool:
    """仅看本地 token 有效性（不依赖配置读取，供中间件高频调用）。"""
    try:
        info = _load()
        return bool(info and info.get("token")
                    and int(info.get("exp") or 0) > time.time())
    except Exception:
        return False


def get_token() -> Optional[str]:
    """当前有效 token（临期自动尝试刷新）；未登录/已失效返回 None。"""
    info = _load()
    if not info or not info.get("token"):
        return None
    exp = int(info.get("exp") or 0)
    now = time.time()
    if exp <= now:
        return None
    if exp - now < REFRESH_THRESHOLD_SECONDS:
        refresh_if_needed()
        info = _load() or info
    return info.get("token")


def previous_token() -> Optional[str]:
    """滚动刷新前的上一代 token（宽限期内仍可被 LAN 客户端使用）。"""
    info = _load()
    return (info or {}).get("prev_token") or None


def login(username: str, password: str) -> dict:
    """账号密码登录：成功落 token 并返回用户信息；失败抛 ServerAuthError。"""
    if not enabled():
        raise ServerAuthError("当前实例未配置中央服务器（server_url）")
    resp = _post("/api/auth/user-login",
                 {"username": username.strip(), "password": password})
    token = resp.get("token") or ""
    if not token:
        raise ServerAuthError("服务器未返回登录令牌")
    info = {
        "token": token,
        "username": resp.get("username") or username.strip(),
        "display_name": resp.get("display_name") or resp.get("username") or "",
        "exp": _decode_exp(token),
    }
    _save(info)
    return {k: info[k] for k in ("username", "display_name")}


def logout() -> None:
    """登出：尽力通知服务端吊销 token，随后清除本地登录态（幂等）。"""
    info = _load()
    if info and info.get("token") and enabled():
        try:
            _post("/api/auth/logout", token=info["token"])
        except Exception:
            pass
    _save(None)


def refresh_if_needed() -> bool:
    """临期（剩余 <30 天）时向服务端换新 token。返回是否发生了刷新。"""
    info = _load()
    if not info or not info.get("token") or not enabled():
        return False
    exp = int(info.get("exp") or 0)
    now = time.time()
    if exp <= now or exp - now >= REFRESH_THRESHOLD_SECONDS:
        return False
    try:
        resp = _post("/api/auth/refresh", token=info["token"])
    except Exception:
        return False
    new_token = resp.get("token") or ""
    if not new_token:
        return False
    # 滚动刷新宽限：旧 token 保留一代——LAN 客户端在刷新瞬间持有的旧 token
    # 不得立刻判失效（生产实证：刷新后旧 token 请求被 401 打掉）
    info["prev_token"] = info["token"]
    info["token"] = new_token
    info["exp"] = _decode_exp(new_token)
    _save(info)
    return True


# ── 用户自助（个人资料 / 改密码）─────────────────────────────────────────

def fetch_profile() -> dict:
    """拉取本人资料（display_name/phone/department/quota/used，只读展示）。"""
    return _get("/api/user/me", _require_token())


def change_password(old_password: str, new_password: str) -> None:
    """修改本人密码（服务端校验旧密码，并吊销其它会话 token）。"""
    if len(new_password or "") < 6:
        raise ServerAuthError("新密码至少 6 位")
    _post("/api/user/password",
          {"old_password": old_password, "new_password": new_password},
          token=_require_token())
