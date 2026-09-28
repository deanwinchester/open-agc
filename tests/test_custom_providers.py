# -*- coding: utf-8 -*-
"""自定义厂商（custom_providers）整体替换时掩码 key 还原的回归测试。

背景：GET /api/settings 回传的 api_key 是 sk-...xxx 掩码；POST 为整体替换
语义，若把掩码直接落库会把真 key 写坏。修复：同名厂商的掩码/空 key 一律
还原为已存 key。
"""
import asyncio

import api.routes.routes_settings as rs


def _patch_env(monkeypatch, tmp_path, stored_providers):
    saved = {}
    monkeypatch.setattr(rs, "load_config", lambda: {
        "custom_providers": stored_providers,
        "api_keys": {},
    })
    monkeypatch.setattr(rs, "save_config", lambda cfg: saved.update(cfg))
    monkeypatch.setattr(rs, "get_data_path", lambda name: str(tmp_path / name))
    return saved


def _post(providers):
    return asyncio.run(rs.update_settings(rs.ConfigUpdate(custom_providers=providers)))


def test_masked_key_restored_from_stored(tmp_path, monkeypatch):
    stored = [{"name": "acme", "base_url": "https://a.com/v1",
               "api_key": "sk-real-key-123456", "models": ["m1"]}]
    saved = _patch_env(monkeypatch, tmp_path, stored)
    # 前端整体替换：编辑 base_url，api_key 原样回传掩码
    resp = _post([{"name": "acme", "base_url": "https://b.com/v1",
                   "api_key": "sk-...456", "models": ["m1", "m2"]}])
    assert resp["status"] == "success"
    cp = saved["custom_providers"][0]
    assert cp["api_key"] == "sk-real-key-123456"   # 掩码还原为真 key
    assert cp["base_url"] == "https://b.com/v1"     # 其他字段正常更新
    assert cp["models"] == ["m1", "m2"]


def test_empty_key_keeps_stored(tmp_path, monkeypatch):
    stored = [{"name": "acme", "base_url": "https://a.com/v1",
               "api_key": "sk-real-key-123456", "models": []}]
    saved = _patch_env(monkeypatch, tmp_path, stored)
    _post([{"name": "acme", "base_url": "https://a.com/v1", "api_key": "", "models": []}])
    assert saved["custom_providers"][0]["api_key"] == "sk-real-key-123456"


def test_new_provider_real_key_kept(tmp_path, monkeypatch):
    saved = _patch_env(monkeypatch, tmp_path, [])
    _post([{"name": "newco", "base_url": "https://n.com/v1",
            "api_key": "sk-brand-new-key", "models": ["x"]}])
    assert saved["custom_providers"][0]["api_key"] == "sk-brand-new-key"
