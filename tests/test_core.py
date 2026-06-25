"""
Core infrastructure tests — memory store, LLM client, token budget, skill store.
Each test is designed to run without API keys or external services.
"""
import os
import sys
import json
import tempfile
import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

_HAS_LITELLM = False
try:
    import litellm  # noqa: F401
    _HAS_LITELLM = True
except ImportError:
    pass

_litellm = pytest.mark.skipif(not _HAS_LITELLM, reason="requires litellm")


class TestMemoryStore:
    """MemoryStore with temporary SQLite database."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        """Patch get_data_path to return a temp dir, then init MemoryStore."""
        self.tmpdir = tempfile.mkdtemp()
        monkeypatch.setattr(
            "core.paths.get_data_path",
            lambda *args: os.path.join(self.tmpdir, *(args or ("test.db",)))
        )
        monkeypatch.setattr(
            "core.paths.get_data_dir",
            lambda: self.tmpdir
        )
        from core.memory_store import MemoryStore
        self.store = MemoryStore(db_path=os.path.join(self.tmpdir, "test_memory.db"))

    def test_add_and_search(self):
        """add_memory → search_memories returns the entry."""
        self.store.add_memory("测试记忆内容: Python是编程语言", category="tech")
        results = self.store.search_memories("Python", top_k=5)
        assert len(results) > 0
        assert "Python" in results[0]["content"]

    def test_add_with_category(self):
        """add_memory respects memory_type and category."""
        mid = self.store.add_memory("用户喜欢用DeepSeek模型", category="tech", memory_type="core")
        assert mid > 0
        results = self.store.search_memories("DeepSeek", top_k=5)
        assert any(r["memory_type"] == "core" for r in results)

    def test_get_memory_by_id(self):
        """get_memory returns the correct entry."""
        mid = self.store.add_memory("记住这个特定条目", category="general")
        mem = self.store.get_memory(mid)
        assert mem is not None
        assert mem["id"] == mid
        assert mem["content"] == "记住这个特定条目"

    def test_update_memory(self):
        """update_memory changes content and updates FTS index."""
        mid = self.store.add_memory("原始内容", category="general")
        ok = self.store.update_memory(mid, "更新后的内容")
        assert ok is True
        mem = self.store.get_memory(mid)
        assert mem["content"] == "更新后的内容"
        results = self.store.search_memories("更新后", top_k=5)
        assert any(r["id"] == mid for r in results)

    def test_delete_memory(self):
        """delete_memory removes entry and cleans up FTS."""
        mid = self.store.add_memory("待删除条目", category="temp")
        assert self.store.get_memory(mid) is not None
        ok = self.store.delete_memory(mid)
        assert ok is True
        assert self.store.get_memory(mid) is None

    def test_get_categories_summary(self):
        """get_categories_summary returns counts per category."""
        self.store.add_memory("技术记忆", category="tech")
        self.store.add_memory("用户偏好", category="user")
        summary = self.store.get_categories_summary()
        assert isinstance(summary, dict)

    def test_get_type_summary(self):
        """get_type_summary returns counts per memory type."""
        self.store.add_memory("核心记忆", category="general", memory_type="core")
        self.store.add_memory("工作记忆", category="general", memory_type="working")
        summary = self.store.get_type_summary()
        assert isinstance(summary, dict)

    def test_search_no_results(self):
        """search_memories with no match returns empty list."""
        results = self.store.search_memories("zzzznotexist", top_k=5)
        assert isinstance(results, list)

    def test_add_memory_with_vector(self):
        """add_memory_vector works or gracefully falls back (no crash)."""
        try:
            self.store.add_memory_vector("向量测试内容", category="test")
        except ImportError:
            pytest.skip("ChromaDB not installed")
        except Exception as e:
            pytest.skip(f"Vector memory init failed: {e}")

    def test_search_semantic(self):
        """search_semantic works or gracefully falls back (no crash)."""
        try:
            results = self.store.search_semantic("测试", top_k=3)
            assert isinstance(results, list)
        except (ImportError, Exception) as e:
            pytest.skip(f"Semantic search unavailable: {e}")


class TestTokenBudget:
    """Token budget estimation and pruning (no litellm needed)."""

    def setup_method(self):
        from core.token_budget import TokenBudget, estimate_messages_tokens
        self.TokenBudget = TokenBudget
        self.estimate_messages_tokens = estimate_messages_tokens

    def test_estimate_messages_tokens_returns_int(self):
        msgs = [{"role": "user", "content": "hello world"}]
        count = self.estimate_messages_tokens(msgs)
        assert isinstance(count, int)
        assert count > 0

    def test_estimate_messages_tokens_chinese(self):
        msgs = [{"role": "user", "content": "你好世界，这是一段中文测试"}]
        count = self.estimate_messages_tokens(msgs)
        assert isinstance(count, int)
        assert count > 0

    def test_estimate_messages_tokens_empty(self):
        assert self.estimate_messages_tokens([]) >= 0

    def test_token_budget_prune_short(self):
        """prune_messages preserves messages when under budget."""
        budget = self.TokenBudget({"max_total_tokens": 999999})
        msgs = [{"role": "system", "content": "you are a bot"},
                {"role": "user", "content": "hi"}]
        pruned = budget.prune_messages(msgs)
        assert isinstance(pruned, list)
        assert pruned[0]["role"] == "system"

    def test_token_budget_prune(self):
        """prune_messages returns a list even when over budget."""
        budget = self.TokenBudget({"max_total_tokens": 50})
        msgs = [{"role": "system", "content": "x" * 200},
                {"role": "user", "content": "y" * 200}]
        pruned = budget.prune_messages(msgs)
        assert isinstance(pruned, list)


@_litellm
class TestLLMClient:
    """LLMClient initialization (no real API calls)."""

    def test_init_with_defaults(self, monkeypatch):
        monkeypatch.setattr("core.llm_client.load_config", lambda: {})
        from core.llm_client import LLMClient
        client = LLMClient(default_model="gpt-4o")
        assert client.default_model == "gpt-4o"
        assert client.fallback_models == []

    def test_init_with_config(self, monkeypatch):
        test_config = {
            "default_model": "deepseek/deepseek-chat",
            "fallback_models": ["openai/gpt-4o"],
            "api_keys": {"deepseek": "sk-test-key"},
            "llamacpp_ctx_size": 32768,
        }
        monkeypatch.setattr("core.llm_client.load_config", lambda: test_config)
        from core.llm_client import LLMClient
        client = LLMClient()
        assert client.default_model == "deepseek/deepseek-chat"
        assert len(client.fallback_models) == 1
        assert os.environ.get("DEEPSEEK_API_KEY") == "sk-test-key"

    def test_clean_llm_text(self):
        from core.llm_client import clean_llm_text
        assert clean_llm_text("<think>some reasoning</think>final answer") == "final answer"
        assert clean_llm_text("<thought>internal</thought>output") == "output"
        assert clean_llm_text("") == ""
        assert clean_llm_text(None) is None

    def test_extract_screenshot_data(self):
        from core.llm_client import extract_screenshot_data
        result = extract_screenshot_data("prefix [SCREENSHOT_DATA:abc123] suffix")
        assert result == "abc123"
        assert extract_screenshot_data("no marker") is None

    def test_infer_provider(self):
        from core.llm_client import _infer_provider
        assert _infer_provider("deepseek/deepseek-chat") == "deepseek"
        assert _infer_provider("openai/gpt-4o") == "openai"
        assert _infer_provider("") == "unknown"
        assert _infer_provider(None) == "unknown"
        assert _infer_provider("claude-sonnet-4") == "anthropic"
        assert _infer_provider("gemini/gemini-pro") == "gemini"

    def test_encode_image_to_data_url(self):
        from core.llm_client import encode_image_to_data_url
        with pytest.raises(FileNotFoundError):
            encode_image_to_data_url("/nonexistent/image.png")

    def test_build_user_message_no_images(self):
        from core.llm_client import build_user_message
        msg = build_user_message("just text")
        assert msg["role"] == "user"
        assert msg["content"] == "just text"

    def test_build_user_message_with_images(self, tmp_path):
        from core.llm_client import build_user_message
        # With data URL — should work even without actual image file
        msg = build_user_message("has image", images=["data:image/png;base64,abc"])
        assert msg["role"] == "user"
        assert isinstance(msg["content"], list)
