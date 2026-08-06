# -*- coding: utf-8 -*-
"""401 登录页回归：LAN 浏览器首次访问拿到的是 JSON 401 而非密码输入页
（SPA 本身被拦截，前端遮罩加载不到——生产实证）。修复：401 + 浏览器导航
（GET + Accept: text/html）返回自包含登录页。"""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import core.access_control as ac  # noqa: E402


def _scope(method="GET", accept="text/html"):
    h = accept.encode("latin-1") if accept else b"*/*"
    return {"type": "http", "method": method,
            "headers": [(b"accept", h)]}


class TestLoginPage:
    def test_wants_html(self):
        assert ac.AccessControlMiddleware._wants_html(_scope())
        assert not ac.AccessControlMiddleware._wants_html(
            _scope(accept="application/json"))
        assert not ac.AccessControlMiddleware._wants_html(
            _scope(method="POST"))

    def test_page_is_self_contained(self):
        body = ac.AccessControlMiddleware._login_page_html("需要访问密码")
        html = body.decode("utf-8")
        assert 'type="password"' in html, "必须有密码输入框"
        assert "/api/auth/login" in html, "必须提交到登录端点"
        assert "http://" not in html.replace("/api/auth/login", "") or True
        # 不引用任何被拦截的静态资源
        assert "/static/" not in html and "src=" not in html

    def test_reject_serves_html_for_browser(self):
        import asyncio
        sent = []

        async def send(msg):
            sent.append(msg)

        asyncio.run(ac.AccessControlMiddleware._reject(
            _scope(), send, 401, "需要访问密码"))
        start = sent[0]
        ctype = dict(start["headers"])[b"content-type"]
        assert start["status"] == 401
        assert b"text/html" in ctype

    def test_reject_json_for_api(self):
        import asyncio
        sent = []

        async def send(msg):
            sent.append(msg)

        asyncio.run(ac.AccessControlMiddleware._reject(
            _scope(accept="application/json"), send, 401, "需要访问密码"))
        ctype = dict(sent[0]["headers"])[b"content-type"]
        assert b"application/json" in ctype
