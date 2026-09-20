"""build 默认配置合并测试：旧包升级后，模板里的新部署键必须合并进已有 config。

背景：播种逻辑只补缺文件，已有 config.json 永远拿不到新键（生产实证：
zxs 包的 ui_theme.app_name / update_manifest_url 在老用户机器上不生效）。
"""
import json
import os

import pytest

import api.config as cfgmod


@pytest.fixture()
def merge_env(tmp_path, monkeypatch):
    """CONFIG_PATH 指向临时目录里的「老配置」+ 模板指向临时「新模板」。"""
    cfg_path = tmp_path / "config.json"
    tmpl_path = tmp_path / "template.json"
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(cfg_path))
    monkeypatch.setattr(cfgmod, "get_data_path", lambda name: str(cfg_path))
    monkeypatch.setattr(cfgmod, "_TEMPLATE_CONFIG", str(tmpl_path))
    return cfg_path, tmpl_path


def _write(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


class TestMergeBuildDefaults:
    def test_new_keys_merged(self, merge_env):
        cfg_path, tmpl_path = merge_env
        _write(cfg_path, {"api_keys": {"deepseek": "sk-user"}})
        _write(tmpl_path, {
            "api_keys": {},
            "update_manifest_url": "http://10.0.0.1:8080/release/version.json",
            "ui_theme": {"app_name": "某品牌智能体"},
        })
        cfg = cfgmod.load_config()
        assert cfg["update_manifest_url"] == "http://10.0.0.1:8080/release/version.json"
        assert cfg["ui_theme"]["app_name"] == "某品牌智能体"
        # 用户原有键不受影响
        assert cfg["api_keys"]["deepseek"] == "sk-user"
        # 落盘了
        on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert on_disk["update_manifest_url"]

    def test_user_app_name_preserved(self, merge_env):
        cfg_path, tmpl_path = merge_env
        _write(cfg_path, {"ui_theme": {"app_name": "用户自定义名"}})
        _write(tmpl_path, {"ui_theme": {"app_name": "某品牌智能体"}})
        cfg = cfgmod.load_config()
        assert cfg["ui_theme"]["app_name"] == "用户自定义名"

    def test_manifest_url_follows_template(self, merge_env):
        """升级通道是构建属性：模板有就以模板为准（旧值被覆盖）。"""
        cfg_path, tmpl_path = merge_env
        _write(cfg_path, {"update_manifest_url": "http://old.example/x.json"})
        _write(tmpl_path, {"update_manifest_url": "http://10.0.0.1:8080/release/version.json"})
        cfg = cfgmod.load_config()
        assert cfg["update_manifest_url"] == "http://10.0.0.1:8080/release/version.json"

    def test_grounder_filled_when_missing(self, merge_env):
        """computer_grounder 是构建预置（zxs 线指向内网定位服务），
        用户未配置时从模板填充。"""
        cfg_path, tmpl_path = merge_env
        _write(cfg_path, {"api_keys": {}})
        _write(tmpl_path, {"computer_grounder": {
            "base_url": "http://10.0.0.2:8003/v1",
            "api_key": "", "model": "UI-TARS-1.5-7B", "coord_mode": "abs"}})
        cfg = cfgmod.load_config()
        assert cfg["computer_grounder"]["model"] == "UI-TARS-1.5-7B"

    def test_grounder_user_config_preserved(self, merge_env):
        """用户已通过设置页配置过 grounder 时不被模板覆盖。"""
        cfg_path, tmpl_path = merge_env
        _write(cfg_path, {"computer_grounder": {
            "base_url": "http://my-host:9000/v1", "api_key": "k", "model": "M"}})
        _write(tmpl_path, {"computer_grounder": {
            "base_url": "http://10.0.0.2:8003/v1", "model": "UI-TARS-1.5-7B"}})
        cfg = cfgmod.load_config()
        assert cfg["computer_grounder"]["base_url"] == "http://my-host:9000/v1"

    def test_splash_keys_filled_when_missing(self, merge_env):
        """启动页文案（splash_title/splash_subtitle）随 ui_theme 一并补缺。"""
        cfg_path, tmpl_path = merge_env
        _write(cfg_path, {"ui_theme": {"app_name": "用户自定义名"}})
        _write(tmpl_path, {"ui_theme": {
            "app_name": "某品牌智能体",
            "assistant_name": "小助手",
            "splash_title": "正在启动某品牌智能体",
            "splash_subtitle": "组件加载中……"}})
        cfg = cfgmod.load_config()
        assert cfg["ui_theme"]["app_name"] == "用户自定义名"  # 已设的不覆盖
        assert cfg["ui_theme"]["assistant_name"] == "小助手"
        assert cfg["ui_theme"]["splash_title"] == "正在启动某品牌智能体"
        assert cfg["ui_theme"]["splash_subtitle"] == "组件加载中……"

    def test_no_template_no_change(self, merge_env, monkeypatch):
        cfg_path, _ = merge_env
        monkeypatch.setattr(cfgmod, "_TEMPLATE_CONFIG", str(cfg_path.parent / "nonexistent.json"))
        _write(cfg_path, {"a": 1})
        cfg = cfgmod.load_config()
        assert cfg == {"a": 1}

    def test_non_default_path_skips_merge(self, tmp_path, monkeypatch):
        """CONFIG_PATH 非真实默认路径（测试/沙箱）时不合并。"""
        cfg_path = tmp_path / "config.json"
        tmpl_path = tmp_path / "template.json"
        monkeypatch.setattr(cfgmod, "CONFIG_PATH", str(cfg_path))
        monkeypatch.setattr(cfgmod, "get_data_path", lambda name: "/real/default/config.json")
        monkeypatch.setattr(cfgmod, "_TEMPLATE_CONFIG", str(tmpl_path))
        _write(cfg_path, {"a": 1})
        _write(tmpl_path, {"update_manifest_url": "http://x", "ui_theme": {"app_name": "y"}})
        cfg = cfgmod.load_config()
        assert "update_manifest_url" not in cfg
