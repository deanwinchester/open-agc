# -*- coding: utf-8 -*-
"""Verify incremental semantics of POST /api/settings (update_settings).

Run from project root:  venv/Scripts/python.exe scratch/verify_incremental_settings.py
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.getcwd())

import api.routes.routes_settings as rs

# ---------- 1) ConfigUpdate accepts partial payload ----------
cu = rs.ConfigUpdate(**{"mcp_servers": {}})
assert cu.mcp_servers == {}
others = cu.model_dump(exclude={"mcp_servers"})
assert all(v is None for v in others.values()), \
    {k: v for k, v in others.items() if v is not None}
print("1) ConfigUpdate({'mcp_servers': {}}) OK — all other fields are None")

# ---------- shared fixtures ----------
tmp = tempfile.mkdtemp()
env_file = os.path.join(tmp, ".env")
config_path = os.path.join(tmp, "config.json")

original_config = {
    "api_keys": {"deepseek": "real-deepseek-key-123456"},
    "default_model": "deepseek/deepseek-chat",
    "fallback_models": ["a", "b"],
    "disabled_skills": ["s1"],
    "sandbox_mode": True,
    "sandbox_dir": "D:/sandbox",
    "llamacpp_ctx_size": 8192,
    "browser_headless": True,
    "http_proxy": "http://127.0.0.1:7890",
    "heartbeat_enabled": True,
    "heartbeat_interval": 120,
    "email_listener_enabled": True,
    "email_account": "me@example.com",
    "email_password": "realpassword",
    "email_imap_server": "imap.example.com",
    "email_smtp_server": "smtp.example.com",
    "owner_email": "owner@example.com",
    "mcp_servers": {"old": {"command": "x"}},
    "searxng_url": "http://old:8888",
    "searxng_port": 9999,
    "max_correction_attempts": 7,
    "cold_cache_ttl": 111,
    "max_resume_count": 3,
    "context_budget": {"max_total_tokens": 64000},
}


def reset():
    rs.load_config = lambda: json.loads(json.dumps(original_config, ensure_ascii=False))
    rs.CONFIG_PATH = config_path
    rs.get_data_path = lambda name: env_file
    rs.set_key = lambda *a, **k: None
    rs.load_dotenv = lambda *a, **k: None


def saved():
    return json.load(open(config_path, encoding="utf-8"))


# ---------- 2) MCP-only save (with session_id) touches nothing else ----------
reset()
new_mcp = {"filesystem": {"command": "npx", "args": ["-y", "mcp-fs"]}}
res = asyncio.run(rs.update_settings(rs.ConfigUpdate(**{"mcp_servers": new_mcp, "session_id": 1})))
assert res["status"] == "success"
cfg = saved()
assert cfg["mcp_servers"] == new_mcp
for k in ("default_model", "email_account", "email_password", "email_listener_enabled",
          "email_imap_server", "email_smtp_server", "owner_email", "fallback_models",
          "disabled_skills", "sandbox_mode", "sandbox_dir", "searxng_url", "searxng_port",
          "context_budget", "api_keys"):
    assert cfg[k] == original_config[k], f"{k} changed: {cfg.get(k)!r} != {original_config[k]!r}"
print("2) MCP-only save: mcp_servers updated; default_model/email_*/api_keys/others untouched")
print("   (session_id present but email fields absent -> per-session email UPDATE skipped)")

# ---------- 3) masked api keys are rejected ----------
reset()
res = asyncio.run(rs.update_settings(rs.ConfigUpdate(**{
    "api_keys": {"deepseek": "rea...456", "kimi": "abc***"},
})))
cfg = saved()
assert cfg["api_keys"]["deepseek"] == "real-deepseek-key-123456", cfg["api_keys"]
assert "kimi" not in cfg["api_keys"], cfg["api_keys"]
print("3) Masked values ('rea...456' and 'abc***') rejected; stored real key kept")

# ---------- 4) real values still update; '***' email sentinel preserved ----------
reset()
res = asyncio.run(rs.update_settings(rs.ConfigUpdate(**{
    "api_keys": {"deepseek": "brand-new-real-key-999"},
    "default_model": "kimi/k2",
    "email_password": "***",
})))
cfg = saved()
assert cfg["api_keys"]["deepseek"] == "brand-new-real-key-999"
assert cfg["default_model"] == "kimi/k2"
assert cfg["email_password"] == "realpassword"   # '***' sentinel keeps existing
assert cfg["email_account"] == "me@example.com"  # not provided -> untouched
print("4) Real key + default_model updated; email_password '***' sentinel preserved")

# ---------- 5) ensure_ascii=False: Chinese written unescaped ----------
reset()
rs.load_config = lambda: {"default_model": "m", "note": "中文注释"}
res = asyncio.run(rs.update_settings(rs.ConfigUpdate(**{"default_model": "m2"})))
raw = open(config_path, encoding="utf-8").read()
assert "中文注释" in raw and "\\u4e2d" not in raw
print("5) ensure_ascii=False: Chinese written unescaped")

print("ALL CHECKS PASSED")
