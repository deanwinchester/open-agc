# -*- coding: utf-8 -*-
"""Tests for _resolve_stream_enabled (kimi_code 默认非流式以保留缓存显示)。"""
import json

from agent.agent import _resolve_stream_enabled


def _write_cfg(tmp_path, **kw):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(kw, ensure_ascii=False), encoding="utf-8")
    return str(p)


class TestResolveStreamEnabled:
    def test_default_true_for_normal_models(self, tmp_path):
        cfg = _write_cfg(tmp_path)
        assert _resolve_stream_enabled(cfg, "deepseek/deepseek-chat") is True
        assert _resolve_stream_enabled(cfg, "gpt-4o") is True

    def test_kimi_code_defaults_to_false(self, tmp_path):
        cfg = _write_cfg(tmp_path)
        assert _resolve_stream_enabled(cfg, "kimi_code/k3") is False
        assert _resolve_stream_enabled(cfg, "kimi_code/kimi-for-coding") is False

    def test_kimi_code_stream_enabled_override(self, tmp_path):
        cfg = _write_cfg(tmp_path, kimi_code_stream_enabled=True)
        assert _resolve_stream_enabled(cfg, "kimi_code/k3") is True

    def test_global_llm_stream_disabled(self, tmp_path):
        cfg = _write_cfg(tmp_path, llm_stream_enabled=False)
        assert _resolve_stream_enabled(cfg, "deepseek/deepseek-chat") is False
        # kimi_code 仍按自己的开关（默认 false）
        assert _resolve_stream_enabled(cfg, "kimi_code/k3") is False

    def test_missing_config_defaults_true(self, tmp_path):
        assert _resolve_stream_enabled(str(tmp_path / "nope.json"), "gpt-4o") is True
