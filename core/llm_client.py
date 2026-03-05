import os
import litellm
from typing import List, Dict, Any, Optional

# Optional logging or debugging controls for litellm
# litellm.set_verbose = True

class LLMClient:
    """
    A unified wrapper for LLM calls using litellm.
    Supports API models (OpenAI, Anthropic, Gemini, DeepSeek) and local models.
    """
    def __init__(self, default_model: str = "gpt-4o"):
        self.default_model = default_model

    def chat(self, messages: List[Dict[str, Any]], model: Optional[str] = None, tools: Optional[List[Dict[str, Any]]] = None) -> Any:
        """
        Send a chat completion request to the specified LLM.
        
        Args:
            messages: List of message dicts with 'role' and 'content'.
            model: The model to use (defaults to self.default_model).
            tools: List of tool schemas for function calling.
        
        Returns:
            The complete response object from litellm.
        """
        target_model = model or self.default_model
        
        kwargs = {
            "model": target_model,
            "messages": messages,
        }
        
        if tools:
            kwargs["tools"] = tools
            
        try:
            response = litellm.completion(**kwargs)
            return response
        except Exception as e:
            print(f"Error calling LLM ({target_model}): {str(e)}")
            raise

    def chat_stream(self, messages: List[Dict[str, Any]], model: Optional[str] = None, tools: Optional[List[Dict[str, Any]]] = None):
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
