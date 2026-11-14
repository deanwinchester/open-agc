"""LLM 错误人话指引表测试（数据驱动：data/llm_error_guides.json 可编辑）。"""
import json

import pytest

from core import llm_error_guides as G


@pytest.fixture()
def tmp_guides(tmp_path, monkeypatch):
    """指引表播种/读取指向临时目录。"""
    monkeypatch.setattr(G, "get_data_path",
                        lambda name: str(tmp_path / name))
    return tmp_path


class TestLLMErrorGuides:
    def test_authentication_maps_to_key_guidance(self, tmp_guides):
        hint = G.render_llm_error_hint("AuthenticationError", "401 Unauthorized")
        assert "API Key" in hint and "/app/settings/models" in hint

    def test_ratelimit(self, tmp_guides):
        hint = G.render_llm_error_hint("RateLimitError", "429 too many")
        assert "限流" in hint

    def test_unknown_falls_back(self, tmp_guides):
        assert G.render_llm_error_hint("WeirdError", "something odd") == G.FALLBACK_HINT

    def test_err_keyword_needs_message_match(self, tmp_guides):
        """err: 前缀的关键字只匹配错误信息，不匹配类型名。"""
        # 类型名里没有 401，信息里有 → 命中 authentication 指引
        hint = G.render_llm_error_hint("APIError", "status code: 401")
        assert "API Key" in hint

    def test_missing_credentials_is_not_internal_server_error(self, tmp_guides):
        hint = G.render_llm_error_hint(
            "InternalServerError",
            "Missing credentials. Please set the MOONSHOT_API_KEY environment variable.",
        )
        assert "API Key" in hint and "/app/settings/models" in hint
        assert "内部错误" not in hint

    def test_subscription_permission_is_not_connection_error(self, tmp_guides):
        hint = G.render_llm_error_hint(
            "APIConnectionError",
            "permission_error: Your current subscription does not have access to Kimi Code. Upgrade your plan.",
        )
        assert "订阅" in hint and "Kimi Code" in hint
        assert "连不上" not in hint

    def test_legacy_guide_file_gets_new_builtin_rules(self, tmp_guides):
        """老安装已有 JSON 时也要获得新增规则，而不是继续沿用错误分类。"""
        (tmp_guides / G._GUIDE_FILE).write_text("[]", encoding="utf-8")
        hint = G.render_llm_error_hint("InternalServerError", "Missing credentials")
        assert "API Key" in hint

    def test_user_customized_guides_win(self, tmp_guides):
        """用户编辑 data/llm_error_guides.json 后按用户文案走。"""
        _seed = tmp_guides / G._GUIDE_FILE
        G._seed_if_missing()
        assert _seed.exists()
        data = json.loads(_seed.read_text(encoding="utf-8"))
        data.insert(0, {"match": "authentication", "hint": "自定义文案"})
        _seed.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        assert G.render_llm_error_hint("AuthenticationError", "") == "自定义文案"
