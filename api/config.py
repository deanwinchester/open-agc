"""Configuration loading/saving for the Open-AGC API server."""
import json
import os
import shutil
import threading
from datetime import datetime
from core.paths import get_data_path

CONFIG_PATH = get_data_path("config.json")

# Protects read-modify-write cycles on config.json across threads
_config_lock = threading.Lock()


def load_config() -> dict:
    """Load configuration from config.json."""
    with _config_lock:
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                # Don't silently discard: back up the corrupt file for inspection
                backup = f"{CONFIG_PATH}.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
                try:
                    shutil.copy2(CONFIG_PATH, backup)
                    print(f"[Config] WARNING: config.json corrupt ({e}); backed up to {backup}")
                except Exception as be:
                    print(f"[Config] WARNING: config.json corrupt ({e}); backup failed: {be}")
                return {}
        return {}


def save_config(config: dict) -> None:
    """Save configuration to config.json atomically (tmp file + os.replace)."""
    with _config_lock:
        tmp_path = CONFIG_PATH + ".tmp"
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, CONFIG_PATH)
        except Exception as e:
            print(f"[Config] Save error: {e}")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass


_agent_log_file = None


def log_agent_error(error_text: str):
    """Log an agent error to the agent error log file."""
    global _agent_log_file
    if _agent_log_file is None:
        log_dir = get_data_path("logs")
        os.makedirs(log_dir, exist_ok=True)
        from datetime import datetime
        _agent_log_file = os.path.join(
            log_dir, f"agent_errors_{datetime.now().strftime('%Y%m%d')}.log"
        )
    try:
        from datetime import datetime
        with open(_agent_log_file, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] {error_text}\n")
    except Exception:
        pass
