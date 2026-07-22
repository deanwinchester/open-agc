# -*- coding: utf-8 -*-
"""Tests for tools/fetch_url.py — SSRF guard matrix, truncation, raw mode, errors.

All network access is mocked: DNS resolution is patched to a public IP via an
autouse fixture, and requests.Session.get is replaced with FakeResp handlers.
"""
import json
import os
import sys
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

pytest.importorskip("bs4")

import tools.fetch_url as fu  # noqa: E402
from tools.fetch_url import (  # noqa: E402
    FetchURLTool, FetchURLError, fetch_url_text, validate_public_url,
)

# Real resolver, captured before the autouse fixture patches it — used by the
# numeric-IP-form regression tests which must exercise the real DNS path.
_REAL_GETADDRINFO = fu.socket.getaddrinfo


# ------------------------------------------------------------------ helpers

class FakeResp:
    def __init__(self, body=b"", status=200, headers=None, url="http://example.com/"):
        self.status_code = status
        self.headers = headers if headers is not None else {
            "Content-Type": "text/html; charset=utf-8"}
        self.url = url
        self._body = body
        self.encoding = "utf-8"

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        pass


def patch_get(monkeypatch, handler):
    """Route requests.Session.get to handler(url) -> FakeResp (or raise)."""
    def fake_get(self, url, **kwargs):
        return handler(url)
    monkeypatch.setattr(fu.requests.Session, "get", fake_get)


def _html(text):
    return f"<html><head><title>T</title></head><body><p>{text}</p></body></html>"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Hermetic DNS: every domain name resolves to a public IP."""
    monkeypatch.setattr(
        fu.socket, "getaddrinfo",
        lambda host, port, **kw: [(2, 1, 6, "", ("93.184.216.34", port))])


# ------------------------------------------------------------------ SSRF matrix

SSRF_LITERAL_URLS = [
    "http://127.0.0.1/",
    "http://127.0.0.1:8080/admin",
    "http://10.0.0.1/",
    "http://10.255.0.9/internal",
    "http://172.16.0.1/",
    "http://172.31.255.254/",
    "http://192.168.0.1/",
    "http://192.168.100.200:8000/",
    "http://169.254.169.254/latest/meta-data",  # cloud metadata endpoint
    "http://0.0.0.0/",
    "http://[::1]/",
    "http://[::1]:11434/api",
    "http://[fc00::1]/",
    "http://[fd12::8]/",
    "http://[fe80::1]/",
    "http://[::ffff:127.0.0.1]/",  # IPv4-mapped IPv6 loopback
    "http://localhost/",
    "http://localhost:3000/api",
    "http://foo.localhost/",
    "http://LOCALHOST./",      # case + trailing dot
    "http://127.0.0.1./",      # trailing dot
]


@pytest.mark.parametrize("url", SSRF_LITERAL_URLS)
def test_ssrf_literal_rejected(url):
    with pytest.raises(FetchURLError):
        validate_public_url(url)


SSRF_NUMERIC_FORM_URLS = [
    "http://2130706433/",   # 127.0.0.1 as a single decimal integer
    "http://0x7f.0.0.1/",   # hex octet
    "http://0177.0.0.1/",   # octal octet
    "http://127.1/",        # short form
    "http://0/",            # 0.0.0.0
]


@pytest.mark.parametrize("url", SSRF_NUMERIC_FORM_URLS)
def test_ssrf_numeric_ip_forms_rejected(monkeypatch, url):
    """Non-canonical IP spellings must never be fetchable.

    ipaddress rejects these spellings, so they fall through to the DNS check
    with the REAL resolver: platforms whose resolver parses inet_aton forms
    resolve them to loopback/unspecified and the SSRF check fires; platforms
    that don't parse them fail DNS resolution — either way FetchURLError.
    """
    monkeypatch.setattr(fu.socket, "getaddrinfo", _REAL_GETADDRINFO)
    with pytest.raises(FetchURLError):
        validate_public_url(url)


@pytest.mark.parametrize("url", [
    "ftp://example.com/x",
    "file:///etc/passwd",
    "http://",
    "not-a-url",
    "",
])
def test_non_http_or_hostless_rejected(url):
    with pytest.raises(FetchURLError):
        validate_public_url(url)


def test_public_literal_ip_allowed():
    assert validate_public_url("http://8.8.8.8/") == "http://8.8.8.8/"
    # 172.32.x.x is just outside the 172.16.0.0/12 private range
    assert validate_public_url("http://172.32.0.1/") == "http://172.32.0.1/"


def test_public_domain_allowed():
    assert validate_public_url("https://example.com/") == "https://example.com/"


def test_domain_resolving_private_rejected(monkeypatch):
    monkeypatch.setattr(
        fu.socket, "getaddrinfo",
        lambda host, port, **kw: [(2, 1, 6, "", ("192.168.1.10", port))])
    with pytest.raises(FetchURLError, match="SSRF"):
        validate_public_url("http://internal.corp/")


def test_domain_dns_failure_rejected(monkeypatch):
    def boom(host, port, **kw):
        raise fu.socket.gaierror("name or service not known")
    monkeypatch.setattr(fu.socket, "getaddrinfo", boom)
    with pytest.raises(FetchURLError, match="DNS"):
        validate_public_url("http://nonexistent.invalid/")


def test_redirect_to_internal_blocked(monkeypatch):
    def handler(url):
        return FakeResp(status=302,
                        headers={"Location": "http://127.0.0.1:8501/"}, url=url)
    patch_get(monkeypatch, handler)
    out = fetch_url_text("http://example.com/")
    assert out.startswith("Error")
    assert "SSRF" in out


def test_tool_blocks_ssrf_before_any_request(monkeypatch):
    called = []

    def fake_get(self, url, **kwargs):
        called.append(url)
        return FakeResp(url=url)
    monkeypatch.setattr(fu.requests.Session, "get", fake_get)
    out = FetchURLTool().execute(url="http://192.168.1.1/admin")
    assert out.startswith("Error") and "SSRF" in out
    assert called == []  # request never fired


def test_web_search_fetch_page_content_guarded():
    """web_search.fetch_page_content now delegates to the hardened fetcher."""
    from tools.web_search import fetch_page_content
    out = fetch_page_content("http://127.0.0.1/")
    assert out.startswith("Error") and "SSRF" in out


# ------------------------------------------------------------------ happy path

def test_redirect_chain_followed(monkeypatch):
    def handler(url):
        if url == "http://example.com/a":
            return FakeResp(status=301,
                            headers={"Location": "http://example.org/b"}, url=url)
        return FakeResp(body=_html("最终页面内容" * 50).encode("utf-8"), url=url)
    patch_get(monkeypatch, handler)
    out = fetch_url_text("http://example.com/a")
    assert "最终页面内容" in out
    assert not out.startswith("Error")


def test_text_mode_strips_tags(monkeypatch):
    patch_get(monkeypatch, lambda url: FakeResp(body=_html("正文内容" * 100).encode("utf-8"), url=url))
    out = FetchURLTool().execute(url="http://example.com/")
    assert "正文内容" in out
    assert "<p>" not in out and "<html>" not in out


def test_spa_next_data_json_extracted(monkeypatch):
    """SPA shell: no DOM text, content only in __NEXT_DATA__ embedded JSON."""
    payload = json.dumps(
        {"props": {"description": "这是一个纯 SPA 页面，正文只存在于内嵌 JSON 数据中，"
                                  "DOM 里没有可见文本，需要从内嵌数据里提取。"}},
        ensure_ascii=False)
    html = ('<html><head><title>SPA页</title></head><body><div id="app"></div>'
            f'<script id="__NEXT_DATA__" type="application/json">{payload}</script>'
            '</body></html>')
    patch_get(monkeypatch, lambda url: FakeResp(body=html.encode("utf-8"), url=url))
    out = fetch_url_text("http://spa.example/")
    assert not out.startswith("Error")
    assert "内嵌 JSON 数据" in out


def test_spa_initial_state_extracted(monkeypatch):
    """SPA shell: content in window.__INITIAL_STATE__ assignment."""
    html = ('<html><head><title>SPA页</title></head><body><div id="root"></div>'
            '<script>window.__INITIAL_STATE__ = {"body": '
            '"这段正文存放在 INITIAL_STATE 全局变量里，页面本身由脚本渲染，'
            '静态标签中找不到任何文字。"};</script>'
            '</body></html>')
    patch_get(monkeypatch, lambda url: FakeResp(body=html.encode("utf-8"), url=url))
    out = fetch_url_text("http://spa.example/")
    assert not out.startswith("Error")
    assert "INITIAL_STATE" in out


# ------------------------------------------------------------------ truncation

def test_max_chars_truncation(monkeypatch):
    body = _html("很长的正文内容" * 1000).encode("utf-8")  # ~7000 chars of text
    patch_get(monkeypatch, lambda url: FakeResp(body=body, url=url))
    out = fetch_url_text("http://example.com/", max_chars=1000)
    assert "output truncated" in out
    assert len(out) < 1200


def test_default_max_chars_is_8000(monkeypatch):
    body = _html("字" * 20000).encode("utf-8")
    patch_get(monkeypatch, lambda url: FakeResp(body=body, url=url))
    out = FetchURLTool().execute(url="http://example.com/")  # no max_chars
    assert "output truncated" in out
    assert 8000 < len(out) < 8300


def test_response_size_cap_2mb(monkeypatch):
    big = _html("x" * (3 * 1024 * 1024)).encode("utf-8")  # >2MB body
    patch_get(monkeypatch, lambda url: FakeResp(body=big, url=url))
    out = FetchURLTool().execute(url="http://example.com/", max_chars=8000)
    assert "response exceeded 2MB" in out


# ------------------------------------------------------------------ raw mode

def test_raw_mode_returns_html(monkeypatch):
    body = _html("正文" * 300).encode("utf-8")
    patch_get(monkeypatch, lambda url: FakeResp(body=body, url=url))
    out = FetchURLTool().execute(url="http://example.com/", raw=True)
    assert "<html>" in out and "<p>" in out


def test_raw_mode_respects_max_chars(monkeypatch):
    body = _html("字" * 20000).encode("utf-8")
    patch_get(monkeypatch, lambda url: FakeResp(body=body, url=url))
    out = FetchURLTool().execute(url="http://example.com/", raw=True, max_chars=500)
    assert "output truncated" in out
    assert len(out) < 700


# ------------------------------------------------------------------ errors

def test_timeout_handled(monkeypatch):
    def fake_get(self, url, **kwargs):
        raise fu.requests.exceptions.Timeout()
    monkeypatch.setattr(fu.requests.Session, "get", fake_get)
    out = fetch_url_text("http://example.com/")
    assert out.startswith("Error") and "超时" in out


def test_connection_error_handled(monkeypatch):
    def fake_get(self, url, **kwargs):
        raise fu.requests.exceptions.ConnectionError("refused")
    monkeypatch.setattr(fu.requests.Session, "get", fake_get)
    out = fetch_url_text("http://example.com/")
    assert out.startswith("Error") and "ConnectionError" in out


def test_non_200_status(monkeypatch):
    patch_get(monkeypatch, lambda url: FakeResp(status=404, url=url))
    out = fetch_url_text("http://example.com/")
    assert out.startswith("Error") and "404" in out


def test_non_html_content_type(monkeypatch):
    patch_get(monkeypatch, lambda url: FakeResp(
        body=b"%PDF-1.4 fake", headers={"Content-Type": "application/pdf"}, url=url))
    out = fetch_url_text("http://example.com/")
    assert out.startswith("Error") and "非 HTML" in out


def test_execute_missing_url():
    out = FetchURLTool().execute(url="")
    assert out.startswith("Error")


# ------------------------------------------------------------------ schema

def test_schema_structure():
    schema = FetchURLTool().get_openai_schema()
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == "fetch_url"
    assert fn["description"]
    params = fn["parameters"]
    assert params["type"] == "object"
    assert set(params["properties"]) == {"url", "max_chars", "raw"}
    assert params["required"] == ["url"]
