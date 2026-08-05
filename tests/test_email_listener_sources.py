# -*- coding: utf-8 -*-
"""邮件监听凭据来源回归：监听器此前只读 sessions 表凭据，而设置页
「邮件监听与助手」保存的是全局 config.json——sessions 行里的旧密码
（生产实证 admin888）遮蔽了新授权码，LOGIN 失败刷屏。修复：全局配置为主，
sessions 行仅补充不同账号（同账号旧行跳过）。"""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from api.background import _email_listener_sources  # noqa: E402

_GOOD = {"email_listener_enabled": True, "email_account": "a@163.com",
         "email_password": "good-auth-code", "email_imap_server": "imap.163.com",
         "email_smtp_server": "smtp.163.com", "owner_email": "me@x.com"}


class TestEmailListenerSources:
    def test_global_config_used(self):
        src = _email_listener_sources(_GOOD, [])
        assert len(src) == 1
        assert src[0]["email_password"] == "good-auth-code"
        assert src[0]["sess_id"] == 1 and src[0]["id"] == 1

    def test_stale_session_same_account_skipped(self):
        """sessions 行与全局同账号（旧密码 admin888）→ 跳过，不遮蔽。"""
        rows = [{"id": 1, "email_account": "a@163.com",
                 "email_password": "admin888", "email_imap_server": "imap.163.com",
                 "email_smtp_server": "smtp.163.com", "owner_email": "me@x.com"}]
        src = _email_listener_sources(_GOOD, rows)
        assert len(src) == 1
        assert src[0]["email_password"] == "good-auth-code"

    def test_session_different_account_kept(self):
        """不同账号的 session 行是合法多邮箱补充，保留。"""
        rows = [{"id": 2, "email_account": "b@qq.com",
                 "email_password": "pw2", "email_imap_server": "imap.qq.com",
                 "email_smtp_server": "smtp.qq.com", "owner_email": ""}]
        src = _email_listener_sources(_GOOD, rows)
        assert len(src) == 2
        assert src[1]["email_account"] == "b@qq.com"

    def test_global_incomplete_falls_back_to_sessions(self):
        rows = [{"id": 1, "email_account": "a@163.com",
                 "email_password": "pw", "email_imap_server": "imap.163.com",
                 "email_smtp_server": "", "owner_email": ""}]
        src = _email_listener_sources(
            {"email_listener_enabled": False}, rows)
        assert len(src) == 1 and src[0]["email_password"] == "pw"

    def test_empty_everywhere(self):
        assert _email_listener_sources({}, []) == []
