import os
import json
import re
import uuid
import base64
import litellm
# Fix for PyInstaller bundling issue with tiktoken
litellm.num_tokens_logging = False
litellm.supports_token_counter = False
from typing import List, Dict, Any, Optional, Tuple


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

        Keeps system messages and the most recent turns that fit.
        Drops oldest messages first.
        """
        if not messages:
            return messages

        system_msgs = [m for m in messages if m.get("role") == "system"]
        other_msgs = [m for m in messages if m.get("role") != "system"]

        sys_tokens = self._estimate_tokens(system_msgs)
        available = max(max_tokens - sys_tokens, max_tokens // 4)

        # Keep messages from newest to oldest until we run out of budget
        kept = []
        used = 0
        for msg in reversed(other_msgs):
            msg_tokens = self._estimate_tokens([msg])
            if used + msg_tokens > available:
                # Truncate this message's content to fit the remaining budget
                if used < available and msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        truncated_len = (available - used) * 3
                        msg = {**msg, "content": content[:truncated_len] + "..."}
                        kept.append(msg)
                break
            kept.append(msg)
            used += msg_tokens

        kept.reverse()
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
                response = litellm.completion(**kwargs)
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
            
            for chunk in response:
                content = chunk.choices[0].delta.content
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
                    chunk.choices[0].delta.content = clean_llm_text(content)
                    if not chunk.choices[0].delta.content:
                        continue
                        
                yield chunk
        except Exception as e:
            print(f"Error calling LLM stream ({target_model}): {str(e)}")
            raise
