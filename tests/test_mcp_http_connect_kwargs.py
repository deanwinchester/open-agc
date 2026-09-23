# -*- coding: utf-8 -*-
"""_http_connect_kwargs 的签名分派测试（mcp SDK 新旧签名兼容）。"""
from tools.mcp_tool import _http_connect_kwargs


def _old_sig(url, headers=None, timeout=60):
    pass


def _new_sig(url, *, http_client=None, terminate_on_close=True):
    pass


def test_headers_passed_through_for_old_signature():
    kw = _http_connect_kwargs({"Authorization": "Bearer k"}, _old_sig)
    assert kw == {"headers": {"Authorization": "Bearer k"}}


def test_httpx_client_built_for_new_signature():
    kw = _http_connect_kwargs({"Authorization": "Bearer k"}, _new_sig)
    assert "http_client" in kw
    assert kw["http_client"].headers["authorization"] == "Bearer k"


def test_no_headers_returns_empty():
    assert _http_connect_kwargs(None, _old_sig) == {}
    assert _http_connect_kwargs({}, _new_sig) == {}
