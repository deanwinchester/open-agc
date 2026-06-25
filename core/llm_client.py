import os
import json
import re
import uuid
import base64
import time
import threading
import sqlite3
import litellm
from litellm.exceptions import ContextWindowExceededError
# Fix for PyInstaller bundling issue with tiktoken
litellm.num_tokens_logging = False
litellm.supports_token_counter = False
from typing import List, Dict, Any, Optional, Tuple

from core.paths import get_data_path

# ── Model call logging ──
_MODEL_LOGS_DB = threading.local()

def _get_model_logs_conn() -> sqlite3.Connection:
    if not hasattr(_MODEL_LOGS_DB, 'conn') or _MODEL_LOGS_DB.conn is None:
        db_path = get_data_path("chat_history.db")
        _MODEL_LOGS_DB.conn = sqlite3.connect(db_path, timeout=5)
        _MODEL_LOGS_DB.conn.execute("PRAGMA journal_mode=WAL")
    return _MODEL_LOGS_DB.conn

def _init_model_logs_table():
    """Create the model_call_logs table if it doesn't exist."""
    try:
        conn = _get_model_logs_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS model_call_logs (
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
                cost_estimate REAL DEFAULT 0.0
            )
        """)
        conn.commit()
    except Exception as _e:
        print(f"[LLMClient] Failed to init model_logs table: {_e}")

def _infer_provider(model_name: str) -> str:
    """Extract provider name from model string."""
    if not model_name:
        return "unknown"
    ml = model_name.lower()
    if "deepseek" in ml: return "deepseek"
    if "gpt" in ml or "openai" in ml: return "openai"
    if "claude" in ml or "anthropic" in ml: return "anthropic"
    if "gemini" in ml or "google" in ml: return "gemini"
    if "kimi" in ml or "moonshot" in ml: return "kimi"
    if "glm" in ml or "zhipu" in ml or "zai" in ml: return "glm"
    if "qwen" in ml or "alibaba" in ml: return "qwen"
    if "llama" in ml or "meta" in ml: return "llama"
    if "llamacpp" in ml: return "local"
    if "sglang" in ml: return "local"
    if "/" in model_name:
        return model_name.split("/")[0]
    return "unknown"

def _detect_cache_hit(response) -> str:
    """Check response usage for cache hit indicators."""
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return "unknown"
        # Anthropic: prompt_tokens_details.cached_tokens > 0
        details = getattr(usage, "prompt_tokens_details", None)
        if details and getattr(details, "cached_tokens", 0) > 0:
            return "hit"
        # OpenAI: completion_tokens_details may indicate cached content
        cd = getattr(usage, "completion_tokens_details", None)
        if cd and getattr(cd, "cached_tokens", 0) > 0:
            return "hit"
    except Exception as _e:
        print(f"[LLMClient] Cache hit detection error: {_e}")
    return "miss"

def _detect_cached_tokens(response) -> int:
    """Extract cached token count from response usage."""
    try:
        usage = getattr(response, "usage", None)
        if not usage:
            return 0
        details = getattr(usage, "prompt_tokens_details", None)
        if details:
            return getattr(details, "cached_tokens", 0) or 0
        cd = getattr(usage, "completion_tokens_details", None)
        if cd:
            return getattr(cd, "cached_tokens", 0) or 0
    except Exception as _e:
        print(f"[LLMClient] Cached tokens detection error: {_e}")
    return 0


# ── Global logging toggle ──
_model_logging_enabled = True

def set_model_logging(enabled: bool):
    global _model_logging_enabled
    _model_logging_enabled = enabled

def is_model_logging_enabled() -> bool:
    return _model_logging_enabled


def _calculate_cost(provider: str, model: str, prompt_tokens: int,
                     completion_tokens: int, cached_tokens: int) -> float:
    """Calculate cost in CNY with provider-specific pricing."""
    ml = model.lower()
    # DeepSeek pricing (¥ per 1M tokens)
    if "deepseek" in ml:
        uncached = prompt_tokens - cached_tokens
        if "chat" in ml:  # Flash
            return (cached_tokens / 1_000_000 * 0.02
                    + uncached / 1_000_000 * 1.0
                    + completion_tokens / 1_000_000 * 2.0)
        else:  # Pro / Reasoner
            return (cached_tokens / 1_000_000 * 0.025
                    + uncached / 1_000_000 * 3.0
                    + completion_tokens / 1_000_000 * 6.0)
    # Default flat rate
    tt = prompt_tokens + completion_tokens
    return (tt / 1000.0) * 0.01


def _log_model_call(provider: str, model: str, prompt_tokens: int,
                     completion_tokens: int, total_tokens: int,
                     request_data: str, response_data: str,
                     cache_hit: str, latency_ms: int,
                     cached_tokens: int = 0,
                     session_id: int = None, task_id: int = None):
    """Insert a model call log entry. Full request/response saved to files."""
    if not _model_logging_enabled:
        return
    try:
        from core.paths import get_data_path
        import os as _ml_os
        import time as _ml_time
        _log_dir = _ml_os.path.join(get_data_path("logs"), "model_calls")
        _ml_os.makedirs(_log_dir, exist_ok=True)

        # Auto-cleanup: delete log files older than 7 days (once per ~100 writes)
        _cleanup_counter = getattr(_log_model_call, '_cleanup_counter', 0) + 1
        _log_model_call._cleanup_counter = _cleanup_counter
        if _cleanup_counter % 100 == 0:
            try:
                _cutoff = _ml_time.time() - 7 * 86400
                for _f in _ml_os.listdir(_log_dir):
                    _fp = _ml_os.path.join(_log_dir, _f)
                    if _ml_os.path.isfile(_fp) and _ml_os.path.getmtime(_fp) < _cutoff:
                        _ml_os.remove(_fp)
            except Exception as _ml_e:
                print(f"[ModelLog] Auto-cleanup error: {_ml_e}")

        _ts = _ml_time.strftime("%Y%m%d_%H%M%S")
        _seq = int(_ml_time.time() * 1000) % 10000
        _base = f"{_ts}_{_seq}_{provider}_{model.replace('/','_')}"
        if len(_base) > 200:
            _base = _base[:200]

        _req_path = _ml_os.path.join(_log_dir, f"{_base}_req.json")
        _resp_path = _ml_os.path.join(_log_dir, f"{_base}_resp.json")

        with open(_req_path, "w", encoding="utf-8") as _f:
            if request_data:
                _f.write(request_data)
        with open(_resp_path, "w", encoding="utf-8") as _f:
            if response_data:
                _f.write(response_data)

        # Store file paths + summary in DB
        _init_model_logs_table()
        conn = _get_model_logs_conn()
        cost = _calculate_cost(provider, model, prompt_tokens, completion_tokens, cached_tokens)
        _summary = (request_data or "")[:500]
        conn.execute(
            """INSERT INTO model_call_logs
               (session_id, task_id, provider, model, prompt_tokens,
                completion_tokens, total_tokens, request_data, response_data,
                cache_hit, latency_ms, cost_estimate, cached_tokens)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (session_id, task_id, provider, model, prompt_tokens,
             completion_tokens, total_tokens, _summary, f"{_req_path}|{_resp_path}",
             cache_hit, latency_ms, cost, cached_tokens)
        )
        conn.commit()
    except Exception as e:
        print(f"[ModelLog] Failed to log: {e}")


# Optional logging or debugging controls for litellm
# litellm.set_verbose = True

# ==========================================
# Multimodal image utilities
# ==========================================

MIME_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
}

SCREENSHOT_MARKER = "[SCREENSHOT_DATA:"


def encode_image_to_data_url(file_path: str) -> str:
    """Read an image file and return a base64 data URL."""
    ext = os.path.splitext(file_path)[1].lower().lstrip(".")
    mime = MIME_TYPES.get(ext, "image/png")
    with open(file_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def build_user_message(text: str, images: list = None) -> dict:
    """Build a user message dict. Uses multimodal content blocks when images are present."""
    if not images:
        return {"role": "user", "content": text}
    content = [{"type": "text", "text": text}]
    for img in images:
        if img.startswith("data:"):
            url = img
        elif os.path.exists(img):
            url = encode_image_to_data_url(img)
        else:
            continue
        content.append({"type": "image_url", "image_url": {"url": url}})
    return {"role": "user", "content": content}


def extract_screenshot_data(text: str) -> Optional[str]:
    """Extract base64 screenshot data from a tool result string. Returns None if not found."""
    if SCREENSHOT_MARKER not in text:
        return None
    try:
        start = text.index(SCREENSHOT_MARKER) + len(SCREENSHOT_MARKER)
        end = text.index("]", start)
        return text[start:end]
    except (ValueError, IndexError):
        return None

def load_config() -> dict:
    from core.paths import get_data_path
    config_path = get_data_path("config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def clean_llm_text(text: str) -> str:
    """Utility to strip thinking tags and JSON artifacts from LLM responses."""
    if not text:
        return text
    
    import re
    
    # 1. Handle JSON hallucinations
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                # Specifically look for keys that hold the actual assistant reply or reasoning
                for key in ["Model", "assistant", "content", "response", "message", "thought", "reasoning", "action"]:
                    if key in data and isinstance(data[key], str):
                        # Skip if it's just a generic "message" action indicator
                        if key == "action" and data[key].lower() == "message":
                            continue
                        text = data[key]
                        break
        except Exception: pass
        
    # 2. Strip reasoning and template tags using regex for better coverage (including multiline)
    patterns = [
        r"<thought>.*?</thought>",
        r"<think>.*?</think>",
        r"<thought>.*", # Unclosed tags
        r"<think>.*",   
        r".*?</thought>",
        r".*?</think>",
        r"<\|im_start\|>", r"<\|im_end\|>", r"<\|endoftext\|>",
        r"assistant\n", r"user\n", r"system\n"
    ]
    
    for pattern in patterns:
        text = re.sub(pattern, "", text, flags=re.DOTALL | re.IGNORECASE)
        
    return text.strip()






class LLMClient:
    """
    A unified wrapper for LLM calls using litellm.
    Supports API models (OpenAI, Anthropic, Gemini, DeepSeek, Kimi, GLM, MiniMax) and local models.
    Includes automatic model failover when the primary model fails.
    """
    def __init__(self, default_model: Optional[str] = None):
        config = load_config()
        self.default_model = default_model or config.get("default_model", "gpt-4o")
        self.fallback_models = config.get("fallback_models", [])

        # Bootstrap: inject API keys from config.json into environment
        # so litellm can find them automatically
        PROVIDER_ENV_MAP = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
            "kimi": "MOONSHOT_API_KEY",
            "glm": "ZAI_API_KEY",
            "minimax": "MINIMAX_API_KEY",
            "llamacpp": "LLAMACPP_API_BASE",
            "huggingface": "HF_TOKEN"
        }
        for provider, env_var in PROVIDER_ENV_MAP.items():
            key = config.get("api_keys", {}).get(provider, "")
            if key and not os.environ.get(env_var):
                os.environ[env_var] = key

        # Set China-specific API base URLs
        if config.get("api_keys", {}).get("kimi"):
            os.environ.setdefault("MOONSHOT_API_BASE", "https://api.moonshot.cn/v1")
        if config.get("api_keys", {}).get("minimax"):
            os.environ.setdefault("MINIMAX_API_BASE", "https://api.minimax.io/v1")

        # llama.cpp API base
        self.llamacpp_api_base = config.get("api_keys", {}).get("llamacpp", "http://localhost:8080/v1")
        os.environ["LLAMACPP_API_BASE"] = self.llamacpp_api_base
        self.llamacpp_ctx_size = config.get("llamacpp_ctx_size", 32768)

        # Ensure local connections bypass proxy
        for var in ["no_proxy", "NO_PROXY"]:
            current = os.environ.get(var, "")
            local_hosts = "localhost,127.0.0.1"
            if not current:
                os.environ[var] = local_hosts
            elif "localhost" not in current or "127.0.0.1" not in current:
                os.environ[var] = f"{current.rstrip(',')},{local_hosts}"

    def _estimate_tokens(self, messages: List[Dict[str, Any]]) -> int:
        """Rough token count estimate. ~3 chars per token for mixed Chinese/English."""
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += len(content) // 3
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        total += len(part.get("text", "")) // 3
        return total

    def _truncate_for_context(self, messages: List[Dict[str, Any]],
                               max_tokens: int = 4096) -> List[Dict[str, Any]]:
        """Truncate message history to fit within the context window.

        Uses ChromaDB embedding for semantic relevance scoring, falls back to
        weighted TF-IDF. Always keeps the last 2 non-system messages.
        """
        if not messages:
            return messages

        system_msgs = [m for m in messages if m.get("role") == "system"]
        other_msgs = [m for m in messages if m.get("role") != "system"]

        sys_tokens = self._estimate_tokens(system_msgs)
        available = max(max_tokens - sys_tokens, max_tokens // 4)

        if len(other_msgs) <= 2:
            return system_msgs + other_msgs

        # Always keep the last 3 messages as reference
        keep_count = min(3, len(other_msgs))
        always_keep = other_msgs[-keep_count:]
        candidates = other_msgs[:-keep_count]

        scored = self._score_by_embedding(always_keep, candidates)
        if scored is None:
            scored = self._score_by_tfidf(always_keep, candidates)

        # Role weighting: user messages more important, tool results less
        _role_weight = {"user": 2.0, "assistant": 1.5, "tool_step": 0.3, "tool": 0.5}
        for i, (score, msg) in enumerate(scored):
            w = _role_weight.get(msg.get("role", ""), 1.0)
            scored[i] = (score * w, msg)

        # Sort by weighted relevance descending
        scored.sort(key=lambda x: -x[0])

        kept = list(always_keep)
        used = self._estimate_tokens(always_keep)
        for score, msg in scored:
            msg_tokens = self._estimate_tokens([msg])
            if used + msg_tokens <= available:
                kept.insert(0, msg)
                used += msg_tokens
            elif used < available and msg.get("role") in ("user", "assistant"):
                content = str(msg.get("content", ""))
                if content:
                    trunc_len = (available - used) * 3
                    kept.insert(0, {**msg, "content": content[:trunc_len] + "..."})
                    used = available
                break

        return system_msgs + kept

    def _score_by_embedding(self, ref_msgs: list, candidates: list):
        """Score candidates by cosine similarity to reference messages using chromadb."""
        try:
            from chromadb.utils import embedding_functions
            import numpy as np
            from core.paths import get_data_dir

            # Store model in data/ for Docker persistence
            model_dir = os.path.join(get_data_dir(), "chroma_embedding")
            os.makedirs(model_dir, exist_ok=True)

            ef = embedding_functions.DefaultEmbeddingFunction(
                download_path=model_dir
            )

            ref_text = " ".join(
                str(m.get("content", ""))[:500] for m in ref_msgs
            )[:2000]
            candidate_texts = [
                str(m.get("content", ""))[:2000] for m in candidates
            ]

            all_texts = [ref_text] + candidate_texts
            embeddings = ef(all_texts)
            ref_emb = np.array(embeddings[0])

            scored = []
            for i, emb in enumerate(embeddings[1:]):
                cand_emb = np.array(emb)
                sim = float(np.dot(ref_emb, cand_emb) / (
                    np.linalg.norm(ref_emb) * np.linalg.norm(cand_emb) + 1e-8
                ))
                scored.append((sim, candidates[i]))

            return scored
        except Exception as e:
            print(f"[LLMClient] Embedding scoring failed ({e}), falling back to TF-IDF")
            return None

    def _score_by_tfidf(self, ref_msgs: list, candidates: list):
        """Score candidates by weighted TF-IDF. Fallback when embedding unavailable."""
        import math
        from collections import Counter

        def _tokenize(text):
            if isinstance(text, list):
                text = " ".join(
                    p.get("text", "") for p in text
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            if not isinstance(text, str):
                return []
            text = text.lower()[:3000]
            result = []
            for token in text.replace("\n", " ").split():
                token = token.strip()
                if not token:
                    continue
                result.append(token)
                for ch in token:
                    if '一' <= ch <= '鿿':
                        result.append(ch)
            return result

        ref_text = " ".join(str(m.get("content", "")) for m in ref_msgs)
        ref_tokens = set(_tokenize(ref_text))

        scored = []
        for msg in candidates:
            text = str(msg.get("content", ""))
            tokens = _tokenize(text)
            tf = Counter(tokens)
            score = sum(count for word, count in tf.items() if word in ref_tokens)
            scored.append((score, msg))

        return scored

    def _sanitize_for_llamacpp(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Make messages compatible with strict GGUF chat templates.

        Many GGUF models (Qwen, etc.) require system message at position 0 and
        may not handle 'tool' role messages from prior turns correctly.
        We merge the system prompt into the first user message and strip
        orphaned tool calls from previous conversation rounds.
        """
        sanitized = []
        system_content = None

        for msg in messages:
            role = msg.get("role", "")
            if role == "system":
                system_content = msg.get("content", "")
                continue

            if role == "tool":
                # Keep tool results only if the previous message was an assistant
                # with tool_calls (i.e., same turn). Otherwise skip orphaned ones.
                if sanitized and sanitized[-1].get("role") == "assistant" and sanitized[-1].get("tool_calls"):
                    sanitized.append(msg)
                continue

            sanitized.append(msg)

        # If there was a system message, prepend it to the first user message
        if system_content:
            for i, msg in enumerate(sanitized):
                if msg.get("role") == "user":
                    sanitized[i] = {
                        **msg,
                        "content": f"{system_content}\n\n---\n\n{msg['content']}"
                    }
                    break

        # Remove assistant messages that have tool_calls but no subsequent tool results
        # (these were orphaned when we stripped tool results above)
        cleaned = []
        for i, msg in enumerate(sanitized):
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                # Check if next message is a tool result
                if i + 1 < len(sanitized) and sanitized[i + 1].get("role") == "tool":
                    cleaned.append(msg)
                # else: skip orphaned tool call message
            else:
                cleaned.append(msg)

        return cleaned

    @staticmethod
    def _remove_orphaned_tool_calls(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Remove assistant messages with tool_calls that lack tool responses.
        Prevents API validation errors like 'insufficient tool messages following tool_calls'.
        This is a general-purpose sanitizer safe for all providers."""
        cleaned = []
        in_tool_round = False  # True while accumulating tool results for an assistant(tc) message
        for i, msg in enumerate(messages):
            role = msg.get("role", "")
            if role == "assistant" and msg.get("tool_calls"):
                # Keep only if the next message is a tool response (any tool_call_id)
                if i + 1 < len(messages) and messages[i + 1].get("role") == "tool":
                    cleaned.append(msg)
                    in_tool_round = True
                # else: drop orphaned tool_calls message, reset flag
                else:
                    in_tool_round = False
            elif role == "tool":
                # Keep tool result if we're inside an active tool round
                if in_tool_round:
                    cleaned.append(msg)
                # else: drop orphaned tool result
            else:
                cleaned.append(msg)
                in_tool_round = False  # non-tool message ends any tool round
        return cleaned

    def _build_model_kwargs(self, model: str, messages: List[Dict[str, Any]],
                             tools: Optional[List[Dict[str, Any]]] = None,
                             stream: bool = False) -> Dict[str, Any]:
        """Build common kwargs for litellm.completion — handles llamacpp and orphan removal."""
        kwargs = {
            "model": model,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        if stream:
            kwargs["stream"] = True

        if "llamacpp/" in model:
            kwargs["api_base"] = self.llamacpp_api_base
            if not model.startswith("openai/"):
                kwargs["model"] = f"openai/{model.replace('llamacpp/', '')}"
            if "api_key" not in kwargs:
                kwargs["api_key"] = "sk-no-key-required"
            truncated = self._truncate_for_context(messages, max_tokens=self.llamacpp_ctx_size)
            kwargs["messages"] = self._sanitize_for_llamacpp(truncated)
            if not stream:
                kwargs["timeout"] = 600
        else:
            kwargs["messages"] = self._remove_orphaned_tool_calls(messages)

        return kwargs

    def chat(self, messages: List[Dict[str, Any]], model: Optional[str] = None,
             tools: Optional[List[Dict[str, Any]]] = None) -> Tuple[Any, str]:
        """
        Send a chat completion request with automatic model failover.
        
        Returns:
            Tuple of (response, actual_model_used)
        """
        target_model = model or self.default_model
        
        # Build the ordered list of models to try
        models_to_try = [target_model]
        for fb in self.fallback_models:
            fb = fb.strip()
            if fb and fb not in models_to_try:
                models_to_try.append(fb)
        
        last_error = None
        for attempt_model in models_to_try:
            # Retry transient errors (connection reset, timeout, 5xx) up to 3 times
            _max_retries = 3
            _retry_delay = 2  # seconds, doubles each retry
            for _retry in range(_max_retries):
                if _retry > 0:
                    import time as _rt
                    _rt.sleep(_retry_delay)
                    _retry_delay *= 2
                    print(f"[LLMClient] Retry {_retry}/{_max_retries} for {attempt_model}...")
                kwargs = self._build_model_kwargs(attempt_model, messages, tools, stream=False)

                try:
                    t0 = time.time()
                    try:
                        response = litellm.completion(**kwargs)
                    except ContextWindowExceededError:
                        print(f"[LLMClient] Context window exceeded for {attempt_model}, compressing...")
                        truncated = self._truncate_for_context(messages, max_tokens=1000000)
                        if len(truncated) < len(messages):
                            note = {"role": "system", "content": (
                                "[上下文压缩] 较早的对话历史已被截断以适应该模型的上下文窗口。"
                                "关键信息已在当前消息中保留，如果需要更早的上下文，请使用 search_history 工具检索。"
                            )}
                            truncated.insert(1, note)
                        kwargs["messages"] = truncated
                        response = litellm.completion(**kwargs)
                        messages[:] = truncated
                    t1 = time.time()

                    try:
                        usage = getattr(response, "usage", None)
                        pt = getattr(usage, "prompt_tokens", 0) if usage else 0
                        ct = getattr(usage, "completion_tokens", 0) if usage else 0
                        tt = pt + ct
                        req_text = json.dumps(messages, ensure_ascii=False) if messages else ""
                        resp_text = ""
                        if hasattr(response, "choices") and response.choices:
                            msg = response.choices[0].message
                            resp_text = getattr(msg, "content", "") or ""
                            if not resp_text:
                                tc = getattr(msg, "tool_calls", None)
                                if tc:
                                    resp_text = json.dumps([{"function": {"name": t.function.name, "arguments": t.function.arguments} if hasattr(t, 'function') and hasattr(t.function, 'name') else str(t)} for t in tc], ensure_ascii=False)
                        _log_model_call(
                            provider=_infer_provider(attempt_model), model=attempt_model,
                            prompt_tokens=pt, completion_tokens=ct, total_tokens=tt,
                            request_data=req_text, response_data=resp_text,
                            cache_hit=_detect_cache_hit(response), cached_tokens=_detect_cached_tokens(response),
                            latency_ms=int((t1 - t0) * 1000),
                        )
                    except Exception as log_e:
                        print(f"[LLMClient] Logging failed: {log_e}")

                    return response, attempt_model
                except Exception as e:
                    last_error = e
                    _err_str = str(e).lower()
                    print(f"[LLMClient] Model {attempt_model} failed: {str(e)[:150]}")
                    if any(kw in _err_str for kw in ["authentication", "api_key", "api key",
                           "invalid_api_key", "not found", "model_not_found",
                           "insufficient_quota", "exceeded quota"]):
                        print(f"[LLMClient] Non-retryable error, skipping retries")
                        break
                    if _retry < _max_retries - 1:
                        print(f"[LLMClient] Will retry ({_retry+1}/{_max_retries})...")
                    else:
                        print(f"[LLMClient] All retries exhausted for {attempt_model}")
                        if attempt_model != models_to_try[-1]:
                            print(f"[LLMClient] Trying next fallback...")
        
        # All models failed
        raise last_error

    def chat_stream(self, messages: List[Dict[str, Any]], model: Optional[str] = None,
                    tools: Optional[List[Dict[str, Any]]] = None):
        """
        Send a streaming chat completion request with thought tag filtering.
        """
        target_model = model or self.default_model
        kwargs = self._build_model_kwargs(target_model, messages, tools, stream=True)
            
        try:
            response = litellm.completion(**kwargs)

            # Stateful filtering for streaming thought tags
            in_thought_block = False

            # Track accumulated data for logging
            _stream_start = time.time()
            _stream_content = ""
            _last_usage = None

            for chunk in response:
                # Capture usage from final chunk
                try:
                    if hasattr(chunk, 'usage') and chunk.usage:
                        _last_usage = chunk.usage
                except Exception as _usage_e:
                    print(f"[LLMClient] Stream usage detection error: {_usage_e}")

                content = None
                try:
                    content = chunk.choices[0].delta.content
                except Exception as _delta_e:
                    print(f"[LLMClient] Stream delta content error: {_delta_e}")

                if content:
                    # Simple state machine to skip content between tags
                    lower_content = content.lower()
                    if "<think" in lower_content or "<thought" in lower_content:
                        in_thought_block = True
                        # If the chunk contains both start and end, we might need more complex splitting
                        # but for simple chunk-based streaming, this heuristic often works.
                        if "/think" in lower_content or "/thought" in lower_content:
                            in_thought_block = False
                        continue

                    if "/think" in lower_content or "/thought" in lower_content:
                        in_thought_block = False
                        continue

                    if in_thought_block:
                        continue

                    # Clean the content just in case any markers remain
                    cleaned = clean_llm_text(content)
                    chunk.choices[0].delta.content = cleaned
                    if not cleaned:
                        continue
                    _stream_content += cleaned
                else:
                    # Check for tool_calls in delta
                    try:
                        delta = chunk.choices[0].delta
                        tc = getattr(delta, "tool_calls", None)
                        if tc:
                            _stream_content += json.dumps([{"function": {"name": t.function.name, "arguments": getattr(t.function, 'arguments', '')}} for t in tc if hasattr(t, 'function') and hasattr(t.function, 'name')], ensure_ascii=False) + " "
                    except Exception:
                        pass

                yield chunk

            # Log after stream completes
            pt = getattr(_last_usage, 'prompt_tokens', 0) if _last_usage else 0
            ct = getattr(_last_usage, 'completion_tokens', 0) if _last_usage else 0
            if pt or ct or _stream_content:
                try:
                    _cached = _detect_cached_tokens(_last_usage) if _last_usage else 0
                    _log_model_call(
                        provider=_infer_provider(target_model),
                        model=target_model,
                        prompt_tokens=pt,
                        completion_tokens=ct,
                        total_tokens=pt + ct,
                        request_data=json.dumps(messages, ensure_ascii=False) if messages else "",
                        response_data=_stream_content,
                        cache_hit="hit" if _cached > 0 else "miss",
                        cached_tokens=_cached,
                        latency_ms=int((time.time() - _stream_start) * 1000),
                    )
                except Exception as log_e:
                    print(f"[LLMClient] Stream log failed: {log_e}")

        except Exception as e:
            print(f"Error calling LLM stream ({target_model}): {str(e)}")
            raise
