import os
import json
import re
import uuid
import base64
import time
import sqlite3
import litellm
from litellm.exceptions import ContextWindowExceededError
# Fix for PyInstaller bundling issue with tiktoken
litellm.num_tokens_logging = False
litellm.supports_token_counter = False
from typing import List, Dict, Any, Optional, Tuple

from core.paths import get_data_path

# ── Model call logging ──
_MODEL_LOGS_DB = None

def _get_model_logs_conn() -> sqlite3.Connection:
    global _MODEL_LOGS_DB
    if _MODEL_LOGS_DB is None:
        db_path = get_data_path("chat_history.db")
        _MODEL_LOGS_DB = sqlite3.connect(db_path, timeout=5)
        _MODEL_LOGS_DB.execute("PRAGMA journal_mode=WAL")
    return _MODEL_LOGS_DB

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
    except Exception:
        pass

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
    except Exception:
        pass
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
    except Exception:
        pass
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
    """Insert a model call log entry."""
    if not _model_logging_enabled:
        return
    try:
        _init_model_logs_table()
        conn = _get_model_logs_conn()
        cost = _calculate_cost(provider, model, prompt_tokens, completion_tokens, cached_tokens)
        conn.execute(
            """INSERT INTO model_call_logs
               (session_id, task_id, provider, model, prompt_tokens,
                completion_tokens, total_tokens, request_data, response_data,
                cache_hit, latency_ms, cost_estimate, cached_tokens)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (session_id, task_id, provider, model, prompt_tokens,
             completion_tokens, total_tokens, request_data, response_data,
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

        Uses TF-IDF relevance scoring to keep the most relevant non-system messages
        while fitting within `max_tokens`. The last 3 user/assistant messages are
        always kept as the reference for relevance scoring.
        """
        if not messages:
            return messages

        system_msgs = [m for m in messages if m.get("role") == "system"]
        other_msgs = [m for m in messages if m.get("role") != "system"]

        sys_tokens = self._estimate_tokens(system_msgs)
        available = max(max_tokens - sys_tokens, max_tokens // 4)

        if len(other_msgs) <= 2:
            # Not enough messages to bother scoring
            return system_msgs + other_msgs

        # Always keep the last 2 messages (latest query + response)
        # Use them as the reference for relevance scoring
        keep_count = min(2, len(other_msgs))
        always_keep = other_msgs[-keep_count:]
        candidates = other_msgs[:-keep_count]

        # Build a TF-IDF relevance score for each candidate message
        import math
        from collections import Counter

        def _tokenize(text):
            if isinstance(text, list):
                text = " ".join(p.get("text", "") for p in text if isinstance(p, dict) and p.get("type") == "text")
            if not isinstance(text, str):
                return []
            text = text.lower()
            # Split English words, keep Chinese characters
            result = []
            for token in text.replace("\n", " ").split():
                token = token.strip()
                if not token:
                    continue
                result.append(token)
                # Also add individual Chinese characters for fine-grained matching
                for ch in token:
                    if '一' <= ch <= '鿿':
                        result.append(ch)
            return result

        # Build reference corpus from always_keep messages
        ref_text = " ".join(str(m.get("content", "")) for m in always_keep)
        ref_tokens = _tokenize(ref_text)
        ref_tf = Counter(ref_tokens)
        ref_unique = set(ref_tokens)

        # Compute TF-IDF score for each candidate
        scored = []
        for msg in candidates:
            text = str(msg.get("content", ""))
            tokens = _tokenize(text)
            tf = Counter(tokens)
            # TF-IDF: term frequency in this doc * idf (based on presence in ref)
            score = 0
            for word, count in tf.items():
                if word in ref_unique:
                    score += count * math.log((len(candidates) + 1) / (1 + 1))
            scored.append((score, msg))

        # Sort by relevance score descending, then preserve original order for ties
        scored.sort(key=lambda x: (-x[0], candidates.index(x[1])))

        # Fit within budget: keep highest-scoring messages
        kept = list(always_keep)
        used = self._estimate_tokens(always_keep)
        for score, msg in scored:
            msg_tokens = self._estimate_tokens([msg])
            if used + msg_tokens <= available:
                kept.insert(0, msg)
                used += msg_tokens
            else:
                # Try truncating this message's content
                if used < available and msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        trunc_len = (available - used) * 3
                        kept.insert(0, {**msg, "content": content[:trunc_len] + "..."})
                        used = available
                break

        return system_msgs + kept

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
            kwargs = {
                "model": attempt_model,
                "messages": messages,
            }
            if tools:
                kwargs["tools"] = tools
            

            if "llamacpp/" in attempt_model:
                kwargs["api_base"] = self.llamacpp_api_base
                if not attempt_model.startswith("openai/"):
                    kwargs["model"] = f"openai/{attempt_model.replace('llamacpp/', '')}"
                if "api_key" not in kwargs:
                    kwargs["api_key"] = "sk-no-key-required"
                # Truncate to fit context window, then sanitize for GGUF chat template
                truncated = self._truncate_for_context(messages, max_tokens=self.llamacpp_ctx_size)
                kwargs["messages"] = self._sanitize_for_llamacpp(truncated)
                kwargs["timeout"] = 600
            else:
                # General sanitization for API models — remove orphaned tool_calls
                kwargs["messages"] = self._remove_orphaned_tool_calls(messages)

            try:
                t0 = time.time()
                try:
                    response = litellm.completion(**kwargs)
                except ContextWindowExceededError:
                    # Context too long — compress and retry
                    print(f"[LLMClient] Context window exceeded for {attempt_model}, compressing...")
                    truncated = self._truncate_for_context(messages, max_tokens=1000000)
                    # Add a compression note so the LLM knows history was trimmed
                    if len(truncated) < len(messages):
                        note = {"role": "system", "content": (
                            "[上下文压缩] 较早的对话历史已被截断以适应该模型的上下文窗口。"
                            "关键信息已在当前消息中保留，如果需要更早的上下文，请使用 search_history 工具检索。"
                        )}
                        truncated.insert(1, note)
                    kwargs["messages"] = truncated
                    response = litellm.completion(**kwargs)
                t1 = time.time()

                # ── Log model call ──
                try:
                    usage = getattr(response, "usage", None)
                    pt = getattr(usage, "prompt_tokens", 0) if usage else 0
                    ct = getattr(usage, "completion_tokens", 0) if usage else 0
                    tt = pt + ct
                    req_text = json.dumps(messages, ensure_ascii=False)[:50000] if messages else ""
                    resp_text = ""
                    if hasattr(response, "choices") and response.choices:
                        msg = response.choices[0].message
                        resp_text = (getattr(msg, "content", "") or "")[:50000]
                        if not resp_text:
                            # Record tool_calls if no text content
                            tc = getattr(msg, "tool_calls", None)
                            if tc:
                                resp_text = json.dumps([{"function": t.function.name if hasattr(t, 'function') and hasattr(t.function, 'name') else str(t)} for t in tc], ensure_ascii=False)[:50000]
                    _log_model_call(
                        provider=_infer_provider(attempt_model),
                        model=attempt_model,
                        prompt_tokens=pt,
                        completion_tokens=ct,
                        total_tokens=tt,
                        request_data=req_text,
                        response_data=resp_text,
                        cache_hit=_detect_cache_hit(response),
                        cached_tokens=_detect_cached_tokens(response),
                        latency_ms=int((t1 - t0) * 1000),
                    )
                except Exception as log_e:
                    print(f"[LLMClient] Logging failed: {log_e}")

                return response, attempt_model
            except Exception as e:
                last_error = e
                print(f"[LLMClient] Model {attempt_model} failed: {str(e)}")
                if attempt_model != models_to_try[-1]:
                    print(f"[LLMClient] Trying next fallback...")
                continue
        
        # All models failed
        raise last_error

    def chat_stream(self, messages: List[Dict[str, Any]], model: Optional[str] = None,
                    tools: Optional[List[Dict[str, Any]]] = None):
        """
        Send a streaming chat completion request with thought tag filtering.
        """
        target_model = model or self.default_model
        
        kwargs = {
            "model": target_model,
            "messages": messages,
            "stream": True
        }
        
        if tools:
            kwargs["tools"] = tools
            
        # For local models, explicitly pass api_base to bypass LiteLLM's internal miscalculations
        if "llamacpp/" in target_model:
            kwargs["api_base"] = self.llamacpp_api_base
            if not target_model.startswith("openai/"):
                kwargs["model"] = f"openai/{target_model.replace('llamacpp/', '')}"
            if "api_key" not in kwargs:
                kwargs["api_key"] = "sk-no-key-required"
            truncated = self._truncate_for_context(messages, max_tokens=self.llamacpp_ctx_size)
            kwargs["messages"] = self._sanitize_for_llamacpp(truncated)
        else:
            # General sanitization for API models — remove orphaned tool_calls
            kwargs["messages"] = self._remove_orphaned_tool_calls(messages)
            
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
                except Exception:
                    pass

                content = None
                try:
                    content = chunk.choices[0].delta.content
                except Exception:
                    pass

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
                            _stream_content += json.dumps([{"function": {"name": t.function.name}} for t in tc if hasattr(t, 'function') and hasattr(t.function, 'name')], ensure_ascii=False) + " "
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
                        request_data=json.dumps(messages, ensure_ascii=False)[:50000] if messages else "",
                        response_data=_stream_content[:50000],
                        cache_hit="hit" if _cached > 0 else "miss",
                        cached_tokens=_cached,
                        latency_ms=int((time.time() - _stream_start) * 1000),
                    )
                except Exception as log_e:
                    print(f"[LLMClient] Stream log failed: {log_e}")

        except Exception as e:
            print(f"Error calling LLM stream ({target_model}): {str(e)}")
            raise
