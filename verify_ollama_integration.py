import sys
import os
import json

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), ".")))

from core.llm_client import LLMClient
from agent.agent import OpenAGCAgent

def test_config_sanitization():
    print("--- Testing Config Sanitization ---")
    client = LLMClient()
    print(f"Ollama API Base: {client.ollama_api_base}")
    if "127.0.0.1" in client.ollama_api_base:
        print("PASS: localhost resolved to 127.0.0.1")
    else:
        print("FAIL: localhost not resolved")

def test_rescue_logic():
    print("\n--- Testing Tool Rescue Logic ---")
    from core.llm_client import PatchedOllamaConfig
    patch = PatchedOllamaConfig()
    
    # Test case 1: Standard JSON
    text1 = '{"name": "execute_shell", "arguments": {"command": "ls"}}'
    rescued1 = patch._rescue_tool_call(text1)
    print(f"Rescued 1: {rescued1}")
    assert rescued1["name"] == "execute_shell"
    
    # Test case 2: JSON in markdown block
    text2 = 'Here is the tool call:\n```json\n{"name": "read_file", "arguments": {"path": "test.txt"}}\n```'
    rescued2 = patch._rescue_tool_call(text2)
    print(f"Rescued 2: {rescued2}")
    assert rescued2["name"] == "read_file"
    
    # Test case 3: JSON with thought
    text3 = '{"thought": "I need to check files", "name": "ls", "parameters": {"path": "."}}'
    rescued3 = patch._rescue_tool_call(text3)
    print(f"Rescued 3: {rescued3}")
    assert rescued3["name"] == "ls"
    assert rescued3["reasoning"] == "I need to check files"
    
    print("PASS: Rescue logic works for various formats")

if __name__ == "__main__":
    try:
        test_config_sanitization()
        test_rescue_logic()
    except Exception as e:
        print(f"VERIFICATION FAILED: {e}")
        sys.exit(1)
