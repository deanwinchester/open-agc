# -*- coding: utf-8 -*-
"""访问控制层：按客户端 IP 三层分类（本机 / 局域网 / 公网）的 ASGI 中间件。

策略（config.json 的 ``access_password`` 字段控制）：
- 本机（127.0.0.0/8、::1，含 v4-mapped IPv6）一律放行，免密码；
- 局域网（10/8、172.16/12、192.168/16、fc00::/7、fe80::/10）需凭密码访问：
  Cookie ``open_agc_token``（HMAC 签名令牌，7 天有效）或
  ``Authorization: Bearer <密码原文|有效令牌>``；
- 公网（其余一切 IP，含公网 IPv6）一律 403；
- ``access_password`` 为空（未配置）= 禁止一切非本机访问（最安全默认）。

不信任 X-Forwarded-For：应用层自身不解析 XFF，且服务器已禁用
uvicorn proxy headers（launcher.py / gui_app.py 均显式
``proxy_headers=False``），因此 ``scope["client"]`` 即真实 TCP 对端地址。
"""
import hashlib
import hmac
import ipaddress
import json
import time
from typing import Callable, Optional

# ── IP 分类 ──────────────────────────────────────────────────────────────

_LAN_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "fc00::/7",   # IPv6 ULA
        "fe80::/10",  # IPv6 链路本地
    )
)


def classify_ip(ip: str) -> str:
    """把客户端 IP 归类为 "local" | "lan" | "public"。

    无法解析的地址（含空串）按 "public" 处理（安全默认）。
    v4-mapped IPv6（如 ::ffff:127.0.0.1）先还原成 v4 再归类。
    """
    try:
        # 去掉 IPv6 scope id（fe80::1%eth0），避免影响网段比较
        addr = ipaddress.ip_address(str(ip).split("%", 1)[0].strip())
    except ValueError:
        return "public"
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if addr.is_loopback:
        return "local"
    for net in _LAN_NETWORKS:
        if addr in net:
            return "lan"
    return "public"


# ── 令牌（HMAC 签名，格式 {expiry_ts}.{sig}）──────────────────────────────

TOKEN_COOKIE_NAME = "open_agc_token"
TOKEN_TTL_SECONDS = 7 * 24 * 3600  # 7 天
_TOKEN_SALT = "open-agc-access-token/v1"


def _token_key(password: str) -> bytes:
    return (_TOKEN_SALT + ":" + password).encode("utf-8")


def issue_token(password: str, now: Optional[float] = None,
                ttl: int = TOKEN_TTL_SECONDS) -> str:
    """签发令牌：HMAC(secret=固定盐+密码) 对 expiry_ts 签名。"""
    expiry = int((time.time() if now is None else now) + ttl)
    sig = hmac.new(_token_key(password), str(expiry).encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return f"{expiry}.{sig}"


def verify_token(token: str, password: str,
                 now: Optional[float] = None) -> bool:
    """校验令牌：先查过期，再验签（恒定时间比较）。"""
    if not token or not password:
        return False
    expiry_str, sep, sig = token.rpartition(".")
    if not sep:
        return False
    try:
        expiry = int(expiry_str)
    except ValueError:
        return False
    if expiry < (time.time() if now is None else now):
        return False
    expected = hmac.new(_token_key(password), expiry_str.encode("utf-8"),
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def check_credential(credential: str, password: str) -> bool:
    """Bearer 凭据：密码原文或有效签名令牌均可。"""
    if not credential or not password:
        return False
    if hmac.compare_digest(credential, password):
        return True
    return verify_token(credential, password)


# ── 响应文案 ─────────────────────────────────────────────────────────────

MSG_LOCAL_ONLY = "本实例仅允许本机访问。可在设置页配置「局域网访问密码」以允许局域网设备凭密码访问。"
MSG_PUBLIC_DENIED = "禁止公网访问：本实例不面向公网开放（含公网 IPv6）。"
MSG_AUTH_REQUIRED = "需要访问密码：请登录后访问。"
MSG_WRONG_CREDENTIAL = "访问密码错误或令牌无效。"

# 登录/自检端点：局域网未认证也可到达（端点内部自行校验），但仍受
# 公网 403 与「未配置密码 = 仅本机」两条规则约束。
AUTH_EXEMPT_PATHS = frozenset({"/api/auth/login", "/api/auth/check"})


def _default_config_loader() -> dict:
    """默认配置来源：api.config.load_config（惰性导入，避免 core → api 硬依赖）。"""
    from api.config import load_config
    return load_config()


def get_access_password(config: Optional[dict] = None) -> str:
    """从配置取访问密码；未配置/空串返回 ""（= 仅本机）。"""
    if config is None:
        config = _default_config_loader()
    return str((config or {}).get("access_password") or "").strip()


def _header_value(scope: dict, name: bytes) -> Optional[str]:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _extract_bearer(scope: dict) -> Optional[str]:
    auth = _header_value(scope, b"authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _extract_cookie_token(scope: dict) -> Optional[str]:
    cookie = _header_value(scope, b"cookie")
    if not cookie:
        return None
    for part in cookie.split(";"):
        name, _, value = part.strip().partition("=")
        if name == TOKEN_COOKIE_NAME:
            return value.strip()
    return None


class AccessControlMiddleware:
    """纯 ASGI 中间件（覆盖 HTTP 与 WebSocket），按客户端 IP 执行三层策略。

    config_loader 可注入（测试用），默认读 config.json；密码改动即时生效，
    无需重启。已签发令牌随密码变更自然失效（HMAC 密钥含密码）。
    """

    def __init__(self, app, config_loader: Callable[[], dict] = None):
        self.app = app
        self._config_loader = config_loader or _default_config_loader

    def _password(self) -> str:
        try:
            return get_access_password(self._config_loader())
        except Exception:
            # 配置读取失败时按未配置处理（最安全：非本机全拒）
            return ""

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        layer = classify_ip(client[0] if client else "")

        if layer == "local":
            await self.app(scope, receive, send)
            return

        password = self._password()
        if not password:
            await self._reject(scope, send, 403, MSG_LOCAL_ONLY)
            return
        if layer == "public":
            await self._reject(scope, send, 403, MSG_PUBLIC_DENIED)
            return

        # 局域网：登录/自检端点放行给路由层处理
        if scope.get("path", "") in AUTH_EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        bearer = _extract_bearer(scope)
        if bearer is not None:
            if check_credential(bearer, password):
                await self.app(scope, receive, send)
            else:
                await self._reject(scope, send, 403, MSG_WRONG_CREDENTIAL)
            return

        cookie_token = _extract_cookie_token(scope)
        if cookie_token and verify_token(cookie_token, password):
            await self.app(scope, receive, send)
            return

        # 无凭据或 Cookie 令牌失效：401 让前端弹密码页
        await self._reject(scope, send, 401, MSG_AUTH_REQUIRED)

    @staticmethod
    def _wants_html(scope: dict) -> bool:
        """浏览器导航请求（GET + Accept: text/html）→ 401 应给登录页而非 JSON。"""
        if scope.get("type") != "http" or scope.get("method") != "GET":
            return False
        accept = _header_value(scope, b"accept") or ""
        return "text/html" in accept

    @staticmethod
    def _login_page_html(message: str) -> bytes:
        # 自包含登录页：无需加载任何被拦截的静态资源（生产实证：SPA 本身
        # 被 401 拦截，前端遮罩根本加载不到，浏览器只见 JSON）
        return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Open-AGC 访问验证</title>
<style>
body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#f5f7fa;font-family:system-ui,-apple-system,"Segoe UI",sans-serif}}
.card{{background:#fff;padding:36px 32px;border-radius:14px;box-shadow:0 4px 24px rgba(0,0,0,.08);width:320px;text-align:center}}
.icon{{font-size:40px;margin-bottom:8px}}
h1{{font-size:18px;margin:0 0 6px;color:#303133}}
p{{font-size:13px;color:#909399;margin:0 0 18px}}
input{{width:100%;box-sizing:border-box;padding:10px 12px;font-size:14px;border:1px solid #dcdfe6;border-radius:8px;outline:none}}
input:focus{{border-color:#409eff}}
button{{width:100%;margin-top:12px;padding:10px;font-size:14px;color:#fff;background:#409eff;border:none;border-radius:8px;cursor:pointer}}
button:hover{{background:#66b1ff}}
.err{{color:#f56c6c;font-size:12px;margin-top:10px;min-height:16px}}
</style></head><body>
<div class="card">
<div class="icon">🐼</div>
<h1>Open-AGC</h1>
<p>{message}</p>
<form id="f" onsubmit="return go(event)">
<input id="pw" type="password" placeholder="访问密码" autocomplete="current-password" autofocus>
<button type="submit">进入</button>
</form>
<div class="err" id="e"></div>
</div>
<script>
async function go(ev) {{
  ev.preventDefault();
  const e = document.getElementById('e'); e.textContent = '';
  try {{
    const r = await fetch('/api/auth/login', {{method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{password: document.getElementById('pw').value}})}});
    if (r.ok) {{ location.replace('/'); return; }}
    const d = await r.json().catch(() => ({{}}));
    e.textContent = d.detail || ('验证失败（HTTP ' + r.status + '）');
  }} catch (err) {{ e.textContent = '网络错误：' + err.message; }}
  return false;
}}
</script>
</body></html>""".encode("utf-8")

    @staticmethod
    async def _reject(scope, send, status: int, message: str):
        if scope["type"] == "websocket":
            await send({
                "type": "websocket.close",
                "code": 4401 if status == 401 else 4403,
                "reason": message,
            })
            return
        # 浏览器导航：401 给自包含登录页（SPA 被拦截加载不了自己的遮罩）
        if status == 401 and AccessControlMiddleware._wants_html(scope):
            body = AccessControlMiddleware._login_page_html(message)
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"text/html; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        body = json.dumps({"detail": message}, ensure_ascii=False).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})
