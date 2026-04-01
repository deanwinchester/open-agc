import subprocess
import os
import sys
import threading
import time
import requests
from typing import Optional

class SGLangManager:
    """Manages the SGLang background process."""
    def __init__(self, model: str = "Qwen/Qwen3.5-9B-Instruct", port: int = 8009):
        self.model = model
        self.port = port
        self.process: Optional[subprocess.Popen] = None
        self._stop_event = threading.Event()

    def is_running(self) -> bool:
        """Check if SGLang is already responding on the specified port."""
        try:
            resp = requests.get(f"http://localhost:{self.port}/v1/models", timeout=2)
            return resp.status_code == 200
        except:
            return False

    def start(self):
        """Start SGLang server in a background process."""
        if self.is_running():
            print(f"[SGLang] Already running on port {self.port}")
            return

        print(f"[SGLang] Starting SGLang with model {self.model} on port {self.port}...")
        
        # Command to start SGLang
        cmd = [
            sys.executable, "-m", "sglang.launch_server",
            "--model-path", self.model,
            "--port", str(self.port)
        ]

        try:
            # Run as a subprocess
            creation_flags = 0x08000000 if os.name == 'nt' else 0 # CREATE_NO_WINDOW
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
                creationflags=creation_flags
            )

            # Thread to log output
            def log_output():
                proc = self.process
                if proc and proc.stdout:
                    for line in proc.stdout:
                        if self._stop_event.is_set():
                            break
                    proc.stdout.close()

            threading.Thread(target=log_output, daemon=True).start()
            proc = self.process
            if proc:
                print(f"[SGLang] Process started with PID {proc.pid}")

        except Exception as e:
            print(f"[SGLang] Failed to start: {e}")

    def stop(self):
        """Stop the SGLang process."""
        self._stop_event.set()
        proc = self.process
        if proc:
            print(f"[SGLang] Stopping process {proc.pid}...")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            self.process = None

# Singleton instance
_sglang_manager: Optional[SGLangManager] = None

def get_sglang_manager() -> SGLangManager:
    global _sglang_manager
    if _sglang_manager is None:
        _sglang_manager = SGLangManager()
    return _sglang_manager
