import os
import json
import uuid
import litellm
# Fix for PyInstaller bundling issue with tiktoken
litellm.num_tokens_logging = False 
litellm.supports_token_counter = False
from typing import List, Dict, Any, Optional, Tuple
from litellm.llms.ollama.completion.transformation import OllamaConfig

# Optional logging or debugging controls for litellm
# litellm.set_verbose = True

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

# Patch LiteLLM's OllamaConfig to support the 'thinking' field (e.g. Qwen3.5)
# This is done here at runtime to avoid modifying venv directly.
class PatchedOllamaConfig(OllamaConfig):
    def transform_response(self, *args, **kwargs):
        # Call original transform first
        resp = super().transform_response(*args, **kwargs)
        
        # Access raw_response from args (model, raw_response, model_response, ...)
        raw_response = args[1] if len(args) > 1 else kwargs.get("raw_response")
        if not raw_response:
            return resp
            
        try:
            response_json = raw_response.json()
            thinking_text = response_json.get("thinking", "")
            response_text = response_json.get("response", "")
            
            # If primary response is empty but thinking has content,
            # treat thinking as the actual output
            if (not response_text or not response_text.strip()) and thinking_text:
                # 1. Check if thinking contains a tool call JSON
                msg = resp.choices[0].message
                if not msg.tool_calls and \
                   thinking_text.strip().startswith("{") and \
                   "\"name\"" in thinking_text and \
                   "\"arguments\"" in thinking_text:
                    try:
                        tool_data = json.loads(thinking_text)
                        if isinstance(tool_data, dict) and "name" in tool_data:
                            msg.content = None
                            msg.tool_calls = [
                                {
                                    "id": f"call_{str(uuid.uuid4())}",
                                    "type": "function",
                                    "function": {
                                        "name": tool_data["name"],
                                        "arguments": json.dumps(
                                            tool_data["arguments"]
                                        )
                                    }
                                }
                            ]
                            resp.choices[0].finish_reason = "tool_calls"
                    except Exception:
                        pass
                
                # 2. If it wasn't a tool call, treat thinking as content
                if not msg.tool_calls:
                    if not msg.content or not msg.content.strip():
                        msg.content = thinking_text
                    else:
                        msg.reasoning_content = thinking_text
                        
        except Exception as e:
            # Silent failure for patch
            print(f"[LLMClient] Ollama patch warning: {str(e)}")
            
        return resp

# Apply the monkeypatch to LiteLLM's internal registry
import litellm.llms.ollama.completion.transformation as transformation
transformation.OllamaConfig = PatchedOllamaConfig


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
            "ollama": "OLLAMA_API_BASE"
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
        ollama_base = config.get("api_keys", {}).get("ollama", "http://localhost:11434")
        
        # Sanitize Ollama URL: LiteLLM expects the base host/port, not the full endpoint.
        # If it ends with /api/generate, /api/chat, etc., strip it.
        # We also handle optional trailing slashes for robustness.
        ollama_base = ollama_base.rstrip("/")
        suffixes_to_strip = ["/api/generate", "/api/chat", "/api/show", "/api/tags"]
        for suffix in suffixes_to_strip:
            if ollama_base.endswith(suffix):
                ollama_base = ollama_base[:-len(suffix)]
                break
        
        # Keep clean OLLAMA_API_BASE in env and as an instance variable for explicit passing
        self.ollama_api_base = ollama_base
        os.environ["OLLAMA_API_BASE"] = ollama_base
        
        # Ensure local connections bypass proxy (important for Ollama on Windows)
        for var in ["no_proxy", "NO_PROXY"]:
            current = os.environ.get(var, "")
            local_hosts = "localhost,127.0.0.1"
            if not current:
                os.environ[var] = local_hosts
            elif "localhost" not in current or "127.0.0.1" not in current:
                os.environ[var] = f"{current.rstrip(',')},{local_hosts}"

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
        Send a streaming chat completion request.
        """
        target_model = model or self.default_model
        
        kwargs = {
            "model": target_model,
            "messages": messages,
            "stream": True
        }
        
        if tools:
            kwargs["tools"] = tools
            
        try:
            response = litellm.completion(**kwargs)
            for chunk in response:
                yield chunk
        except Exception as e:
            print(f"Error calling LLM stream ({target_model}): {str(e)}")
            raise
