# -*- coding: utf-8 -*-
"""品牌人格（soul.md）播种/升级的回归测试。

实证：zxs 版 agent 自称「中新社智能体（熊猫）」——熊猫是开源版默认人格
泄漏。品牌线播种/升级品牌人格（小新 + 品牌显示名），通用版不变。
"""
import agent.agent as ag


def test_generic_default_unchanged(monkeypatch):
    """无 brand.json：仍是熊猫通用人格。"""
    import core.paths as paths
    monkeypatch.setattr(paths, "_BRAND_CACHE", {})
    assert "熊猫" in ag._default_soul_md()


def test_brand_default_persona(monkeypatch, tmp_path):
    """有 brand.json：品牌人格（小新 + 中新社智能体），无熊猫/Open-AGC。"""
    import core.paths as paths
    import json
    (tmp_path / "build_data").mkdir()
    (tmp_path / "build_data" / "brand.json").write_text(json.dumps(
        {"app_name": "ZXS", "display_name": "中新社智能体"}), encoding="utf-8")
    monkeypatch.setattr(paths, "_BRAND_CACHE", None)
    monkeypatch.setattr(paths, "get_base_dir", lambda: str(tmp_path))
    import api.config as cfgmod
    monkeypatch.setattr(cfgmod, "load_config", lambda: {
        "ui_theme": {"assistant_name": "小新"}})
    # get_brand 在函数内从 core.paths import——打的是路径模块的缓存与目录
    soul = ag._default_soul_md()
    assert "小新" in soul
    assert "中新社智能体" in soul
    assert "熊猫" not in soul
    assert "Open-AGC" not in soul
