import sys
import os

# Add project root to sys.path
sys.path.append("/Users/lhh/workspace/open-agc")

from core.logger import SessionLogger
from core.stats_manager import get_stats_manager

def test_logger():
    print("Testing SessionLogger...")
    logger = SessionLogger(log_dir="data/logs", session_id=999, model="test-model-v1")
    logger.log_user_query("Hello test")
    
    # Check if file exists and contains model
    log_path = logger.log_path
    if os.path.exists(log_path):
        with open(log_path, "r") as f:
            lines = f.readlines()
            print(f"Latest log entry: {lines[-1].strip()}")
            if '"model": "test-model-v1"' in lines[-1]:
                print("✅ Model info found in logs.")
            else:
                print("❌ Model info NOT found in logs.")
    else:
        print("❌ Log file not created.")

def test_stats():
    print("\nTesting StatsManager Provider Detection...")
    manager = get_stats_manager("data/memory.db")
    
    # Test recording with a provider that contains deepseek
    # This simulates our refined agent.py detection
    provider = "deepseek" # This would be result of keyword check
    manager.record_usage(provider=provider, model="deepseek-chat", prompt_tokens=100, completion_tokens=50, session_id=999)
    
    # Verify retrieval
    history = manager.get_usage_history("deepseek", days=1)
    if history and any(h['total'] >= 150 for h in history):
        print("✅ DeepSeek stats recorded and retrieved correctly.")
    else:
        print("❌ DeepSeek stats not found.")

if __name__ == "__main__":
    test_logger()
    test_stats()
