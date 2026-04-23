import litellm
import json
import os

# Use IP instead of localhost for Windows stability
ollama_base = "http://127.0.0.1:11434"
os.environ["OLLAMA_API_BASE"] = ollama_base
# litellm.set_verbose = True

tools = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather for a location",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string"}
                },
                "required": ["location"]
            }
        }
    }
]

messages = [
    {"role": "user", "content": "What is the weather in Tokyo? Use the get_weather tool."}
]

print(f"--- Testing with ollama_chat/qwen3.5:9b (Base: {ollama_base}) ---")
try:
    response = litellm.completion(
        model="ollama_chat/qwen3.5:9b",
        messages=messages,
        tools=tools,
        api_base=ollama_base,
        timeout=30
    )
    print("Response choice:")
    print(response.choices[0].message)
except Exception as e:
    print(f"Error: {e}")
