import os
import json
import litellm
from typing import List, Dict, Any, Optional, Tuple

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
        os.environ.setdefault("OLLAMA_API_BASE", "http://localhost:11434")
        
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
