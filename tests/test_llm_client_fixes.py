"""
Tests for 阶段4 Task4 fixes:
  1. _infer_provider ordering (llamacpp/sglang → local)
  2. _sanitize_for_llamacpp preserves ALL system messages
  3. Context-compaction retry does not mutate the caller's list and
     rebuilds kwargs via _build_model_kwargs
  4. model_call_logs DDL gains cached_tokens + init-once flag
  5. cleanup_model_logs min_cost<=0 works, freed_bytes accumulates,
     and stale-KG cleanup targets task_trajectories

All tests run without API keys or external services.
"""
import os
import sys
import json
import sqlite3
import types
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


@_litellm
class TestInferProvider:
    """Provider inference must classify local serving stacks before the
    upstream provider keywords their model ids may contain."""

    @pytest.mark.parametrize("model,expected", [
        # Local stacks first (regression: used to hit "llama"/"qwen" first)
        ("llamacpp/qwen", "local"),
        ("llamacpp/qwen2.5-7b-instruct", "local"),
        ("llamacpp/deepseek-r1-distill", "local"),
        ("llamacpp/llama-3-8b", "local"),
        ("sglang/llama-3-8b", "local"),
        ("sglang/qwen3-32b", "local"),
        # Hosted providers
        ("deepseek/deepseek-chat", "deepseek"),
        ("deepseek-chat", "deepseek"),
        ("deepseek/deepseek-reasoner", "deepseek"),
        ("kimi_code/kimi-for-coding", "kimi_code"),
        ("kimi_code/claude-sonnet-4-5", "kimi_code"),
        ("gpt-4o", "openai"),
        ("openai/gpt-4o-mini", "openai"),
        ("claude-3-5-sonnet-20241022", "anthropic"),
        ("anthropic/claude-opus-4", "anthropic"),
        ("gemini/gemini-2.0-flash", "gemini"),
        ("gemini-2.5-pro", "gemini"),
        ("moonshot/kimi-k2", "kimi"),
        ("kimi-k2-0711-preview", "kimi"),
        ("glm-4.5", "glm"),
        ("zhipu/glm-4-air", "glm"),
        ("qwen/qwen3-235b", "qwen"),
        ("qwen2.5-72b-instruct", "qwen"),
        ("meta-llama/llama-3.1-8b", "llama"),
        ("llama-3.1-70b", "llama"),
        # Fallbacks
        ("openrouter/mistral", "openrouter"),
        ("mistral-large-latest", "unknown"),
        ("", "unknown"),
        (None, "unknown"),
    ])
    def test_infer_provider(self, model, expected):
        from core.llm_client import _infer_provider
        assert _infer_provider(model) == expected


@_litellm
class TestSanitizeForLlamacpp:
    """All system messages must survive sanitization, in order."""

    def _client(self):
        from core.llm_client import LLMClient
        # _sanitize_for_llamacpp uses no instance state; skip __init__ side effects
        return LLMClient.__new__(LLMClient)

    def test_multiple_system_messages_all_preserved_in_order(self):
        client = self._client()
        messages = [
            {"role": "system", "content": "SYS-1 主提示词"},
            {"role": "system", "content": "SYS-2 工具说明"},
            {"role": "system", "content": "SYS-3 输出规范"},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！有什么可以帮你？"},
            {"role": "user", "content": "第二轮问题"},
        ]
        result = client._sanitize_for_llamacpp(messages)

        # No system-role messages remain
        assert all(m.get("role") != "system" for m in result)

        # All three system contents merged into the first user message, in order
        first_user = next(m for m in result if m.get("role") == "user")
        content = first_user["content"]
        i1 = content.index("SYS-1 主提示词")
        i2 = content.index("SYS-2 工具说明")
        i3 = content.index("SYS-3 输出规范")
        assert i1 < i2 < i3
        assert "你好" in content

        # Later turns untouched
        assert any(m.get("content") == "第二轮问题" for m in result)
        assert any(m.get("content") == "你好！有什么可以帮你？" for m in result)

    def test_single_system_message_still_merged(self):
        client = self._client()
        messages = [
            {"role": "system", "content": "ONLY-SYS"},
            {"role": "user", "content": "hi"},
        ]
        result = client._sanitize_for_llamacpp(messages)
        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert "ONLY-SYS" in result[0]["content"]
        assert "hi" in result[0]["content"]

    def test_no_system_message_passthrough(self):
        client = self._client()
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        result = client._sanitize_for_llamacpp(messages)
        assert result == messages

    def test_tool_call_round_trip_preserved(self):
        client = self._client()
        messages = [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "t1", "content": "result"},
            {"role": "assistant", "content": "done"},
        ]
        result = client._sanitize_for_llamacpp(messages)
        roles = [m.get("role") for m in result]
        assert "tool" in roles  # tool result kept with its assistant tool_calls


@_litellm
class TestContextCompactionRetry:
    """ContextWindowExceeded retry must not mutate the caller's list and must
    rebuild kwargs (so llamacpp sanitization applies on the retry path)."""

    def test_caller_list_not_mutated_and_kwargs_rebuilt(self, monkeypatch):
        import core.llm_client as llm_mod
        from core.llm_client import LLMClient
        from litellm.exceptions import ContextWindowExceededError

        monkeypatch.setattr(llm_mod, "load_config", lambda: {})
        monkeypatch.setattr(llm_mod, "_log_model_call", lambda *a, **k: None)
        client = LLMClient()

        # Deterministic truncation: keep first + last message only
        def fake_truncate(messages, max_tokens=4096):
            return [messages[0], messages[-1]]
        monkeypatch.setattr(client, "_truncate_for_context", fake_truncate)

        calls = []

        def fake_completion(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise ContextWindowExceededError(
                    "context window exceeded",
                    model="llamacpp/qwen", llm_provider="openai")
            usage = types.SimpleNamespace(
                prompt_tokens=10, completion_tokens=5,
                prompt_tokens_details=None, completion_tokens_details=None)
            message = types.SimpleNamespace(content="ok", tool_calls=None)
            return types.SimpleNamespace(
                usage=usage, choices=[types.SimpleNamespace(message=message)])

        monkeypatch.setattr(llm_mod.litellm, "completion", fake_completion)

        messages = [
            {"role": "system", "content": "SYS-PROMPT"},
            {"role": "user", "content": "U1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "U2"},
        ]
        snapshot = [dict(m) for m in messages]

        response, used_model = client.chat(messages, model="llamacpp/qwen")

        assert used_model == "llamacpp/qwen"
        assert len(calls) == 2

        # 1) The caller's list is untouched by the compaction retry
        assert messages == snapshot
        assert len(messages) == 4

        # 2) The retry went through _build_model_kwargs again:
        #    model rewritten to openai/... and messages sanitized for llamacpp
        retry_kwargs = calls[1]
        assert retry_kwargs["model"] == "openai/qwen"
        sent = retry_kwargs["messages"]
        assert all(m.get("role") != "system" for m in sent)

        # 3) Truncated content is what actually got sent (U2 kept, U1 dropped)
        sent_text = json.dumps(sent, ensure_ascii=False)
        assert "U2" in sent_text
        assert "U1" not in sent_text
        # System prompt content survived via the merge
        assert "SYS-PROMPT" in sent_text


@_litellm
class TestModelLogsTableInit:
    """cached_tokens column + init-once flag for _init_model_logs_table."""

    def test_cached_tokens_column_added_and_init_once(self, monkeypatch, tmp_path):
        import core.llm_client as llm_mod

        db_path = str(tmp_path / "chat_history.db")
        # Pre-create the table with the OLD schema (no cached_tokens)
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE model_call_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                provider TEXT NOT NULL,
                model TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

        # Route the per-write connections (阶段4 Task5: db_connect + closing)
        # at the temp DB and reset the process-level init-once flag.
        monkeypatch.setattr(llm_mod, "db_connect",
                            lambda: sqlite3.connect(db_path))
        monkeypatch.setattr(llm_mod, "_model_logs_table_ready", False)

        # First call: runs DDL + idempotent ALTER on the old-schema DB
        llm_mod._init_model_logs_table()
        assert llm_mod._model_logs_table_ready is True

        check = sqlite3.connect(db_path)
        cols = {row[1] for row in check.execute("PRAGMA table_info(model_call_logs)")}
        check.close()
        assert "cached_tokens" in cols

        # Calling again is a no-op (flag short-circuits before touching the DB)
        llm_mod._init_model_logs_table()

        # Prove the second call never opens a connection
        def _boom():
            raise AssertionError("db_connect must not be called after init")
        monkeypatch.setattr(llm_mod, "db_connect", _boom)
        llm_mod._init_model_logs_table()


class TestCleanupModelLogs:
    """cleanup_model_logs with a temporary database."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, tmp_path):
        self.tmp_path = tmp_path
        self.db_path = str(tmp_path / "chat_history.db")
        monkeypatch.setattr("core.db_maintenance.get_data_path",
                            lambda name: self.db_path)

        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE model_call_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                session_id INTEGER,
                task_id INTEGER,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                request_data TEXT,
                response_data TEXT,
                cache_hit TEXT DEFAULT 'unknown',
                latency_ms INTEGER DEFAULT 0,
                cost_estimate REAL DEFAULT 0.0,
                cached_tokens INTEGER DEFAULT 0
            )
        """)

        # Two old rows with real on-disk req/resp files
        self.old_files = []
        for i in range(2):
            req = tmp_path / f"old_{i}_req.json"
            resp = tmp_path / f"old_{i}_resp.json"
            req.write_text('{"req": true}', encoding="utf-8")
            resp.write_text('{"resp": "' + "x" * 100 + '"}', encoding="utf-8")
            self.old_files += [str(req), str(resp)]
            conn.execute(
                """INSERT INTO model_call_logs
                   (timestamp, provider, model, response_data, cost_estimate)
                   VALUES (?, ?, ?, ?, ?)""",
                ("2020-01-01 00:00:00", "deepseek", "deepseek-chat",
                 f"{req}|{resp}", 0.001)
            )

        # One fresh row that must survive
        conn.execute(
            """INSERT INTO model_call_logs
               (timestamp, provider, model, response_data, cost_estimate)
               VALUES (datetime('now'), 'openai', 'gpt-4o', 'req|resp', 0.001)"""
        )
        conn.commit()
        conn.close()

    def _remaining_rows(self):
        conn = sqlite3.connect(self.db_path)
        n = conn.execute("SELECT COUNT(*) FROM model_call_logs").fetchone()[0]
        conn.close()
        return n

    def test_default_min_cost_deletes_old_rows(self):
        """Regression: min_cost=0.0 used to produce `cost_estimate < 0.0`
        (never true), so nothing was ever deleted."""
        from core.db_maintenance import cleanup_model_logs
        result = cleanup_model_logs(days=30)  # default min_cost=0.0

        assert result["deleted_rows"] == 2
        assert result["deleted_files"] == 4
        assert result["freed_bytes"] > 0
        assert self._remaining_rows() == 1
        for f in self.old_files:
            assert not os.path.exists(f)

    def test_freed_bytes_matches_file_sizes(self):
        from core.db_maintenance import cleanup_model_logs
        expected = sum(os.path.getsize(f) for f in self.old_files)
        result = cleanup_model_logs(days=30)
        assert result["freed_bytes"] == expected

    def test_min_cost_filter_still_works_when_positive(self):
        """min_cost > 0 keeps the cost predicate: only cheap rows are deleted."""
        from core.db_maintenance import cleanup_model_logs
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT INTO model_call_logs
               (timestamp, provider, model, response_data, cost_estimate)
               VALUES ('2020-01-01 00:00:00', 'openai', 'gpt-4o', '', 100.0)"""
        )
        conn.commit()
        conn.close()

        result = cleanup_model_logs(days=30, min_cost=1.0)
        # Only the two cheap old rows (0.001 < 1.0), not the expensive one
        assert result["deleted_rows"] == 2
        assert self._remaining_rows() == 2

    def test_dry_run_deletes_nothing(self):
        from core.db_maintenance import cleanup_model_logs
        result = cleanup_model_logs(days=30, dry_run=True)
        assert result["deleted_rows"] == 2
        assert self._remaining_rows() == 3
        for f in self.old_files:
            assert os.path.exists(f)


class TestCleanupStaleKgData:
    """The reflections cleanup must target the real task_trajectories table."""

    def test_deletes_from_task_trajectories(self, monkeypatch, tmp_path):
        db_path = str(tmp_path / "agent.db")
        monkeypatch.setattr("core.db_maintenance.get_data_path",
                            lambda name: db_path)

        conn = sqlite3.connect(db_path)
        # Mirror core/reflection.py schema
        conn.execute("""
            CREATE TABLE task_trajectories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_input TEXT NOT NULL,
                tool_sequence TEXT NOT NULL,
                success INTEGER NOT NULL DEFAULT 0,
                reflection_id INTEGER DEFAULT NULL,
                duration_seconds REAL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        # reflection.py stores created_at as datetime.now().isoformat()
        conn.execute(
            "INSERT INTO task_trajectories (task_input, tool_sequence, success, created_at) "
            "VALUES ('old task', '[]', 1, '2020-01-01T00:00:00')")
        conn.execute(
            "INSERT INTO task_trajectories (task_input, tool_sequence, success, created_at) "
            "VALUES ('new task', '[]', 1, datetime('now'))")
        conn.commit()
        conn.close()

        from core.db_maintenance import cleanup_stale_kg_data
        result = cleanup_stale_kg_data(days=90)

        assert result["deleted_reflections"] == 1
        conn = sqlite3.connect(db_path)
        n = conn.execute("SELECT COUNT(*) FROM task_trajectories").fetchone()[0]
        conn.close()


@_litellm
class TestCacheDetection:
    """Cache hit detection must recognise provider-specific usage fields."""

    def _response(self, usage):
        class _R:
            pass
        r = _R()
        r.usage = usage
        return r

    def test_anthropic_cache_read_input_tokens(self):
        from core.llm_client import _detect_cache_hit, _detect_cached_tokens
        u = type("U", (), {"prompt_tokens_details": None,
                           "completion_tokens_details": None,
                           "cache_read_input_tokens": 12,
                           "prompt_cache_hit_tokens": 0})()
        assert _detect_cache_hit(self._response(u)) == "hit"
        assert _detect_cached_tokens(self._response(u)) == 12

    def test_openai_prompt_tokens_details_cached(self):
        from core.llm_client import _detect_cache_hit, _detect_cached_tokens
        details = type("D", (), {"cached_tokens": 7})()
        u = type("U", (), {"prompt_tokens_details": details,
                           "completion_tokens_details": None,
                           "cache_read_input_tokens": 0,
                           "prompt_cache_hit_tokens": 0})()
        assert _detect_cache_hit(self._response(u)) == "hit"
        assert _detect_cached_tokens(self._response(u)) == 7

    def test_deepseek_prompt_cache_hit_tokens(self):
        from core.llm_client import _detect_cache_hit, _detect_cached_tokens
        u = type("U", (), {"prompt_tokens_details": None,
                           "completion_tokens_details": None,
                           "cache_read_input_tokens": 0,
                           "prompt_cache_hit_tokens": 9})()
        assert _detect_cache_hit(self._response(u)) == "hit"
        assert _detect_cached_tokens(self._response(u)) == 9

    def test_no_cache_indicators_returns_miss(self):
        from core.llm_client import _detect_cache_hit, _detect_cached_tokens
        u = type("U", (), {"prompt_tokens_details": None,
                           "completion_tokens_details": None,
                           "cache_read_input_tokens": 0,
                           "prompt_cache_hit_tokens": 0})()
        assert _detect_cache_hit(self._response(u)) == "miss"
        assert _detect_cached_tokens(self._response(u)) == 0


@_litellm
class TestAnthropicPromptCacheMarker:
    """Anthropic-style endpoints need an explicit cache_control breakpoint."""

    def _client(self):
        from core.llm_client import LLMClient
        c = LLMClient.__new__(LLMClient)
        c.kimi_code_api_key = "sk-test"
        c.kimi_code_api_base = "https://test/v1"
        c.xiaomi_api_key = ""
        c.xiaomi_api_base = ""
        c.llamacpp_api_base = ""
        c.llamacpp_ctx_size = 32768
        c._custom_providers = {}
        return c

    def test_first_system_message_gets_cache_control(self):
        from core.llm_client import LLMClient
        messages = [
            {"role": "system", "content": "STATIC PROMPT"},
            {"role": "user", "content": "hi"},
        ]
        marked = LLMClient._mark_anthropic_prompt_cache(messages)
        assert marked[0]["role"] == "system"
        content = marked[0]["content"]
        assert isinstance(content, list)
        assert content[0]["text"] == "STATIC PROMPT"
        assert content[0]["cache_control"] == {"type": "ephemeral"}
        # The last message is also marked as a breakpoint so the next turn
        # reuses the whole conversation prefix, not just the system prompt.
        assert isinstance(marked[1]["content"], list)
        assert marked[1]["content"][0]["cache_control"] == {"type": "ephemeral"}

    def test_first_system_and_last_message_marked(self):
        from core.llm_client import LLMClient
        messages = [
            {"role": "system", "content": "FIRST"},
            {"role": "user", "content": "u"},
            {"role": "system", "content": "SECOND"},
        ]
        marked = LLMClient._mark_anthropic_prompt_cache(messages)
        assert isinstance(marked[0]["content"], list)
        assert marked[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
        # The middle message is untouched
        assert marked[1]["content"] == "u"
        # The last message becomes a breakpoint so the whole prefix is reused
        assert isinstance(marked[2]["content"], list)
        assert marked[2]["content"][0]["cache_control"] == {"type": "ephemeral"}

    def test_kimi_code_build_kwargs_marks_system(self):
        client = self._client()
        kwargs = client._build_model_kwargs(
            "kimi_code/k3",
            [{"role": "system", "content": "STATIC"}, {"role": "user", "content": "hi"}],
        )
        assert kwargs["model"] == "anthropic/k3"
        content = kwargs["messages"][0]["content"]
        assert isinstance(content, list)
        assert content[0]["cache_control"] == {"type": "ephemeral"}


@_litellm
class TestStreamCollectLogging:
    """Streaming calls must log usage from the aggregated response, not just
    the provider's optional final usage chunk."""

    def _client(self):
        from core.llm_client import LLMClient
        c = LLMClient.__new__(LLMClient)
        c.default_model = "kimi_code/k3"
        c.fallback_models = []
        c.kimi_code_api_key = "sk-test"
        c.kimi_code_api_base = "https://test/v1"
        c.xiaomi_api_key = ""
        c.xiaomi_api_base = ""
        c.llamacpp_api_base = ""
        c.llamacpp_ctx_size = 32768
        c._custom_providers = {}
        c._log_session_id = None
        c._log_task_id = None
        return c

    def test_logs_usage_from_stream_chunk_builder(self, monkeypatch):
        from core.llm_client import _log_model_call, litellm

        logged = []
        monkeypatch.setattr("core.llm_client._log_model_call", lambda **kw: logged.append(kw))

        class FakeDelta:
            content = "hello"

        class FakeChoice:
            delta = FakeDelta()

        class FakeChunk:
            choices = [FakeChoice()]
            model = "anthropic/k3"

        def fake_completion(**kwargs):
            yield FakeChunk()

        class FakeMessage:
            content = "hello"
            tool_calls = None

        class FakeUsage:
            prompt_tokens = 42
            completion_tokens = 5

        class FakeResponse:
            choices = [type("C", (), {"message": FakeMessage()})()]
            usage = FakeUsage()

        monkeypatch.setattr(litellm, "completion", fake_completion)
        monkeypatch.setattr(litellm, "stream_chunk_builder", lambda chunks, **kw: FakeResponse())

        client = self._client()
        resp, model = client.chat_stream_collect(messages=[{"role": "user", "content": "hi"}])

        assert model == "kimi_code/k3"
        assert len(logged) == 1
        entry = logged[0]
        assert entry["provider"] == "kimi_code"
        assert entry["model"] == "kimi_code/k3"
        assert entry["prompt_tokens"] == 42
        assert entry["completion_tokens"] == 5
        assert entry["total_tokens"] == 47
        assert entry["cache_hit"] == "miss"
