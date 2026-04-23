import requests
import json

url = "http://127.0.0.1:11434/api/chat"
payload = {
    "model": "qwen3.5:9b",
    "messages": [
        {"role": "user", "content": "Hello!"}
    ],
    "stream": False
}

print(f"--- Testing {url} with qwen3.5:9b ---")
try:
    response = requests.post(url, json=payload, timeout=20)
    print(f"Status: {response.status_code}")
    print(f"Response: {response.text}")
except Exception as e:
    print(f"Error: {e}")

url_tags = "http://127.0.0.1:11434/api/tags"
print(f"\n--- Testing {url_tags} ---")
try:
    response = requests.get(url_tags, timeout=10)
    print(f"Status: {response.status_code}")
    print(f"Response: {response.text[:200]}...")
except Exception as e:
    print(f"Error: {e}")
