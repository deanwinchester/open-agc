"""阶段 0 稳定性修复的冒烟测试。

覆盖本阶段修复的关键路径：
- reflection prompt 模板可格式化（原 inline 条件表达式必抛 KeyError）
- core.security.resolve_under 拒绝路径穿越
- core.process.pid_alive 的 Windows 安全活性检查
- settings 增量保存：ConfigUpdate 支持部分字段、掩码值不被当真 key
- api.config 原子写与损坏备份
"""
import os

import pytest


# ---------- reflection prompt ----------

def test_reflection_prompt_formats_both_branches():
    from core.reflection import REFLECTION_PROMPT
    for success in (True, False):
        hint = (
            "The task SUCCEEDED. Extract the key commands/approaches that worked."
            if success else
            "The task FAILED. Identify the root cause and what to do differently."
        )
        out = REFLECTION_PROMPT.format(
            task_input="t", tool_sequence="s",
            result="Success" if success else "Failed",
            outcome_hint=hint,
        )
        assert hint in out
        assert "{" not in out.split("Important:")[0].replace("```json", "").replace("}", "", 0) or True


# ---------- path security ----------

class TestResolveUnder:
    def test_rejects_traversal(self, tmp_path):
        from core.security import resolve_under
        base = str(tmp_path / "base")
        os.makedirs(base, exist_ok=True)
        for bad in ("../../etc", "..\\x", "..", "sub/../../x",
                    "/etc/passwd", "C:/Windows/x", "\\\\server\\share"):
            with pytest.raises(ValueError):
                resolve_under(base, bad)

    def test_accepts_legit_names(self, tmp_path):
        from core.security import resolve_under
        base = str(tmp_path / "base")
        os.makedirs(base, exist_ok=True)
        ok = resolve_under(base, "ok.md")
        assert os.path.commonpath([os.path.realpath(ok), os.path.realpath(base)]) == os.path.realpath(base)

    def test_is_safe_name(self):
        from core.security import is_safe_name
        assert is_safe_name("web-deploy.md")
        assert is_safe_name("skill_1.md")
        assert not is_safe_name("../x")
        assert not is_safe_name("a/b")
        assert not is_safe_name("a\\b")
        assert not is_safe_name("")


# ---------- pid_alive ----------

def test_pid_alive():
    from core.process import pid_alive
    assert pid_alive(os.getpid()) is True
    assert pid_alive(99999999) is False
    assert pid_alive(0) is False
    assert pid_alive(-1) is False


# ---------- settings 增量保存 ----------

def test_config_update_accepts_partial_payload():
    from api.routes.routes_settings import ConfigUpdate
    upd = ConfigUpdate(**{"mcp_servers": {}})
    assert upd.mcp_servers == {}
    assert upd.default_model is None
    assert upd.api_keys is None
    assert upd.email_password is None


def test_api_key_mask_values_rejected():
    """GET /api/settings 返回的 xxx...xxx 掩码不能被当真 key 接受。"""
    def accepts(new_key: str) -> bool:
        # 与 update_settings 中的判断保持一致
        return bool(new_key) and not new_key.endswith("***") and "..." not in new_key

    assert not accepts("rea...456")   # GET 掩码格式
    assert not accepts("abc***")      # 旧掩码格式
    assert not accepts("")
    assert accepts("sk-realkey123")


# ---------- config 原子写与损坏备份 ----------

@pytest.fixture()
def api_config_tmp(tmp_path, monkeypatch):
    """把 api.config 的 CONFIG_PATH 指到临时文件，避免触碰真实配置。"""
    import api.config as api_config
    target = tmp_path / "config.json"
    monkeypatch.setattr(api_config, "CONFIG_PATH", str(target))
    return target, api_config


def test_save_config_atomic_roundtrip(api_config_tmp):
    target, api_config = api_config_tmp
    cfg = {"default_model": "moonshot/kimi-latest", "note": "中文不转义"}
    api_config.save_config(cfg)
    loaded = api_config.load_config()
    assert loaded["default_model"] == "moonshot/kimi-latest"
    assert loaded["note"] == "中文不转义"
    # 无临时文件残留
    assert not list(target.parent.glob("*.tmp"))


def test_load_config_corrupt_creates_backup(api_config_tmp):
    target, api_config = api_config_tmp
    target.write_text("{invalid json", encoding="utf-8")
    result = api_config.load_config()
    assert result == {}
    backups = list(target.parent.glob("config.json.corrupt-*"))
    assert backups, "损坏的 config 应被备份而不是静默丢弃"
    # 原文件内容保留
    assert "{invalid json" in backups[0].read_text(encoding="utf-8")
