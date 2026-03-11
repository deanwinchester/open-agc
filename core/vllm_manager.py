import subprocess
import os
import sys
import threading
import time
import requests
from typing import Optional

class VLLMManager:
    """Manages the vLLM background process."""
    def __init__(self, model: str = "Qwen/Qwen3.5-9B-Instruct", port: int = 8009):
        self.model = model
        self.port = port
        self.process: Optional[subprocess.Popen] = None
        self._stop_event = threading.Event()

    def is_running(self) -> bool:
        """Check if vLLM is already responding on the specified port."""
        try:
            resp = requests.get(f"http://localhost:{self.port}/v1/models", timeout=2)
            return resp.status_code == 200
        except:
            return False

    def start(self):
        """Start vLLM server in a background process."""
        if self.is_running():
            print(f"[vLLM] Already running on port {self.port}")
            return

        print(f"[vLLM] Starting vLLM with model {self.model} on port {self.port}...")
        
        # Command to start vLLM
        # We use --tool-call-parser qwen35_coder as requested for Qwen 3.5
        cmd = [
            sys.executable, "-m", "vllm.entrypoints.openai.api_server",
            "--model", self.model,
            "--port", str(self.port),
            "--tool-call-parser", "qwen35_coder",
            "--reasoning-parser", "qwen3",
            "--gpu-memory-utilization", "0.8" 
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
                print(f"[vLLM] Process started with PID {proc.pid}")

        except Exception as e:
            print(f"[vLLM] Failed to start: {e}")

    def stop(self):
        """Stop the vLLM process."""
        self._stop_event.set()
        proc = self.process
        if proc:
            print(f"[vLLM] Stopping process {proc.pid}...")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            self.process = None

# Singleton instance
_vllm_manager: Optional[VLLMManager] = None

def get_vllm_manager() -> VLLMManager:
    global _vllm_manager
    if _vllm_manager is None:
        _vllm_manager = VLLMManager()
    return _vllm_manager
