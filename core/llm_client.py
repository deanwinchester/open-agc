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
from litellm.llms.ollama.completion.transformation import OllamaConfig

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

# Patch LiteLLM's OllamaConfig to support the 'thinking' field and robust tool rescue
class PatchedOllamaConfig(OllamaConfig):
    # No longer needed as we use clean_llm_text globally


    def _rescue_tool_call(self, text: str) -> Optional[Dict[str, Any]]:
        """Attempt to extract a tool call and optional reasoning from a raw JSON string."""
        if not text:
            return None
        
        # Find potential JSON block in the text
        # Handles cases where the model wraps JSON in text or markdown
        content = text.strip()
        
        # Try to find the first '{' and corresponding last '}'
        start_idx = content.find('{')
        end_idx = content.rfind('}')
        
        if start_idx == -1 or end_idx == -1 or end_idx < start_idx:
            return None
            
        json_str = content[start_idx:end_idx+1]
        
        try:
            data = json.loads(json_str)
            if not isinstance(data, dict):
                return None
            
            result = None
            
            # Format 1: Action/Parameters {"action": "...", "parameters": {...}} or {"action": "...", "action_input": {...}}
            if "action" in data and ("parameters" in data or "action_input" in data):
                result = {
                    "name": data["action"],
                    "arguments": data.get("parameters") or data.get("action_input")
                }
            
            # Format 2: OpenAI-like but in content {"name": "...", "arguments": {...}}
            elif "name" in data and ("arguments" in data or "parameters" in data):
                result = {
                    "name": data["name"],
                    "arguments": data.get("arguments") or data.get("parameters")
                }
            
            # Format 3: Tool/Args {"tool": "...", "args": {...}}
            elif "tool" in data and "args" in data:
                result = {
                    "name": data["tool"],
                    "arguments": data.get("args")
                }
            
            # Format 4: Execution Plan {"execution": {"action_type": "...", ...}}
            elif "execution" in data and isinstance(data["execution"], dict):
                exec_data = data["execution"]
                action = exec_data.get("action_type")
                
                if action == "code":
                    result = {
                        "name": "execute_python",
                        "arguments": {"code": exec_data.get("code_content") or exec_data.get("code") or ""}
                    }
                elif action == "shell":
                    result = {
                        "name": "execute_shell",
                        "arguments": {"command": exec_data.get("command") or exec_data.get("code_content") or ""}
                    }
                elif "name" in exec_data and ("arguments" in exec_data or "parameters" in exec_data):
                    result = {
                        "name": exec_data["name"],
                        "arguments": exec_data.get("arguments") or exec_data.get("parameters")
                    }
            
            # Format 5: Qwen-style internal reasoning {"tool_selection": "..."}
            elif "tool_selection" in data:
                result = {
                    "name": data["tool_selection"],
                    "arguments": data.get("tool_arguments") or data.get("arguments") or data.get("parameters") or {}
                }

            if result:
                # Capture reasoning if present in the same JSON
                reasoning = data.get("thought") or data.get("reasoning") or data.get("description")
                if reasoning:
                    result["reasoning"] = reasoning
                return result

        except Exception:
            pass
        return None

    def transform_response(self, *args, **kwargs):
        # Call original transform first
        try:
            resp = super().transform_response(*args, **kwargs)
        except Exception as e:
            # If transform fails, create a skeleton response to try to rescue content
            print(f"[LLMClient] Ollama transform error: {str(e)}")
            return None # Fail safely or handle if possible
            
        if not resp or not resp.choices:
            return resp

        # Access raw_response from args (model, raw_response, model_response, ...)
        raw_response = args[1] if len(args) > 1 else kwargs.get("raw_response")
        if not raw_response:
            return resp
            
        try:
            response_json = raw_response.json()
            thinking_text = response_json.get("thinking", "")

            response_text = response_json.get("response", "") or response_json.get("message", {}).get("content", "")
            
            msg = resp.choices[0].message
            
            # 1. Always preserve thinking as reasoning_content if it exists
            if thinking_text and not getattr(msg, 'reasoning_content', None):
                setattr(msg, 'reasoning_content', thinking_text)
            
            # 2. Rescue tool calls if native tool_calls is empty
            if not getattr(msg, 'tool_calls', None):
                rescued = None
                # Try response first (primary output)
                if response_text:
                    rescued = self._rescue_tool_call(response_text)
                
                # If failed, try thinking (secondary output/fallback)
                if not rescued and thinking_text:
                    rescued = self._rescue_tool_call(thinking_text)
                
                if rescued:
                    msg.content = None
                    msg.tool_calls = [
                        {
                            "id": f"call_{str(uuid.uuid4())}",
                            "type": "function",
                            "function": {
                                "name": rescued["name"],
                                "arguments": json.dumps(rescued["arguments"]) if not isinstance(rescued["arguments"], (str, type(None))) else (rescued["arguments"] or "{}")
                            }
                        }
                    ]
                    resp.choices[0].finish_reason = "tool_calls"
                    
                    if rescued.get("reasoning") and not getattr(msg, 'reasoning_content', None):
                        setattr(msg, 'reasoning_content', rescued["reasoning"])
            
            # 3. Handle fallback if primary response is empty but thinking has content
            if (not getattr(msg, 'tool_calls', None)) and (not response_text or not response_text.strip()):
                if thinking_text and (not msg.content or not msg.content.strip()):
                    msg.content = thinking_text
                    
            # Final cleanup for both content and reasoning
            if msg.content:
                msg.content = clean_llm_text(msg.content)
            
            reasoning = getattr(msg, 'reasoning_content', None)
            if reasoning:
                setattr(msg, 'reasoning_content', clean_llm_text(reasoning))
                
        except Exception as e:
            print(f"[LLMClient] Ollama patch warning: {str(e)}")
            
        return resp

# Apply the monkeypatch to LiteLLM's internal registry
import litellm.llms.ollama.completion.transformation as transformation
transformation.OllamaConfig = PatchedOllamaConfig
transformation.OllamaChatConfig = PatchedOllamaConfig # Patch Chat config too



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
            "ollama": "OLLAMA_API_BASE",
            "sglang": "SGLANG_API_BASE",
            "vllm": "VLLM_API_BASE",
            "llamacpp": "LLAMACPP_API_BASE"
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

        # Default Ollama API base and proxy bypass for local connections
        ollama_base = config.get("api_keys", {}).get("ollama", "http://127.0.0.1:11434")
        
        # Resolve localhost to 127.0.0.1 for stability on Windows
        ollama_base = ollama_base.replace("://localhost", "://127.0.0.1")
        
        # Sanitize Ollama URL
        ollama_base = ollama_base.rstrip("/")
        suffixes_to_strip = ["/api/generate", "/api/chat", "/api/show", "/api/tags", "/v1"]
        for suffix in suffixes_to_strip:
            if ollama_base.endswith(suffix):
                ollama_base = ollama_base[:-len(suffix)]
                break
        
        # Keep clean OLLAMA_API_BASE in env and as an instance variable
        self.ollama_api_base = ollama_base
        os.environ["OLLAMA_API_BASE"] = ollama_base
        
        # SGLang API base
        self.sglang_api_base = config.get("api_keys", {}).get("sglang", "http://localhost:8009/v1")
        os.environ["SGLANG_API_BASE"] = self.sglang_api_base

        # vLLM API base
        self.vllm_api_base = config.get("api_keys", {}).get("vllm", "http://localhost:8000/v1")
        os.environ["VLLM_API_BASE"] = self.vllm_api_base

        # llama.cpp API base
        self.llamacpp_api_base = config.get("api_keys", {}).get("llamacpp", "http://localhost:8080/v1")
        os.environ["LLAMACPP_API_BASE"] = self.llamacpp_api_base

        # Ensure local connections bypass proxy (important for Ollama on Windows)
        for var in ["no_proxy", "NO_PROXY"]:
            current = os.environ.get(var, "")
            local_hosts = "localhost,127.0.0.1"
            if not current:
                os.environ[var] = local_hosts
            elif "localhost" not in current or "127.0.0.1" not in current:
                os.environ[var] = f"{current.rstrip(',')},{local_hosts}"

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
            
            # For Ollama models, explicitly pass api_base to bypass LiteLLM's internal miscalculations
            # that sometimes lead to 404 errors (appending /api/generate/api/show)
            if "ollama" in attempt_model:
                kwargs["api_base"] = self.ollama_api_base
            if "sglang" in attempt_model:
                kwargs["api_base"] = self.sglang_api_base
            if "vllm" in attempt_model:
                kwargs["api_base"] = self.vllm_api_base
            if "llamacpp/" in attempt_model:
                kwargs["api_base"] = self.llamacpp_api_base
                if not attempt_model.startswith("openai/"):
                    kwargs["model"] = f"openai/{attempt_model.replace('llamacpp/', '')}"
                if "api_key" not in kwargs:
                    kwargs["api_key"] = "sk-no-key-required"
                kwargs["messages"] = self._sanitize_for_llamacpp(messages)
                # Increase timeout for local models (loading + inference)
                kwargs["timeout"] = 600

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
        if "ollama" in target_model:
            kwargs["api_base"] = self.ollama_api_base
        if "sglang/" in target_model:
            kwargs["api_base"] = self.sglang_api_base
        elif "vllm/" in target_model:
            kwargs["api_base"] = self.vllm_api_base
        elif "llamacpp/" in target_model:
            kwargs["api_base"] = self.llamacpp_api_base
            # LiteLLM needs 'openai/' prefix to use the OpenAI-compatible logic for llama-server
            if not target_model.startswith("openai/"):
                kwargs["model"] = f"openai/{target_model.replace('llamacpp/', '')}"
            if "api_key" not in kwargs:
                kwargs["api_key"] = "sk-no-key-required"
            
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
