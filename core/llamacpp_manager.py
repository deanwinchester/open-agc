import subprocess
import os
import sys
import threading
import time
import requests
import zipfile
import io
from typing import Optional, List, Dict
from core.paths import get_bin_dir, get_models_dir

class LlamaCppManager:
    """Manages the llama-server binary, models, and process."""
    def __init__(self, port: int = 8080):
        self.port = port
        self.process: Optional[subprocess.Popen] = None
        self._stop_event = threading.Event()
        self.bin_dir = get_bin_dir()
        self.models_dir = get_models_dir()
        self.exe_name = "llama-server.exe" if os.name == 'nt' else "llama-server"
        self.exe_path = os.path.join(self.bin_dir, self.exe_name)

    def is_binary_installed(self) -> bool:
        """Check if llama-server exists."""
        return os.path.exists(self.exe_path)

    def download_binary(self, progress_callback=None) -> bool:
        """Download and extract the latest llama-server binary for Windows."""
        # For simplicity, we use a known working release URL or fetch from GitHub
        # Using a fallback URL if one fails
        try:
            # Try to get the latest release tag from GitHub API
            resp = requests.get("https://api.github.com/repos/ggerganov/llama.cpp/releases/latest", timeout=5)
            if resp.status_code == 200:
                assets = resp.json().get("assets", [])
                # Look for appropriate build for OS
                target_asset = None
                
                if sys.platform == "darwin": # macOS
                    import platform
                    arch = platform.machine().lower()
                    keywords = ["bin-macos-arm64", "bin-macos-m1", "bin-macos-x64"] if arch == "arm64" else ["bin-macos-x64"]
                elif sys.platform.startswith("linux"): # Linux
                    keywords = ["bin-ubuntu-x64", "bin-linux-x64"]
                else: # Windows
                    keywords = ["bin-win-vulkan-x64", "bin-win-cpu-x64", "bin-win-avx2"]

                for keyword in keywords:
                    for asset in assets:
                        name = asset["name"]
                        if keyword in name and (name.endswith(".zip") or name.endswith(".tar.gz")):
                            target_asset = asset
                            break
                    if target_asset:
                        break
                
                if target_asset:
                    download_url = target_asset["browser_download_url"]
                    print(f"[LlamaCPP] Downloading binary from {download_url}...")
                    
                    zip_resp = requests.get(download_url, stream=True)
                    zip_resp.raise_for_status()
                    
                    total_size = int(zip_resp.headers.get('content-length', 0))
                    downloaded = 0
                    
                    
                    zip_buffer = io.BytesIO()
                    for chunk in zip_resp.iter_content(chunk_size=8192):
                        if chunk:
                            zip_buffer.write(chunk)
                            downloaded += len(chunk)
                            if progress_callback and total_size > 0:
                                progress_callback(downloaded / total_size)
                    
                    zip_buffer.seek(0)
                    print(f"[LlamaCPP] Extracting to {self.bin_dir}...")
                    if target_asset["name"].endswith(".zip"):
                        with zipfile.ZipFile(zip_buffer) as z:
                            z.extractall(self.bin_dir)
                    elif target_asset["name"].endswith(".tar.gz"):
                        import tarfile
                        with tarfile.open(fileobj=zip_buffer, mode="r:gz") as tar:
                            tar.extractall(self.bin_dir)
                    
                    # Ensure llama-server exists (sometimes it's in a subfolder or named differently)
                    if not os.path.exists(self.exe_path):
                        for root, dirs, files in os.walk(self.bin_dir):
                            if self.exe_name in files:
                                os.rename(os.path.join(root, self.exe_name), self.exe_path)
                                break
                    
                    if sys.platform != "nt" and os.path.exists(self.exe_path):
                        os.chmod(self.exe_path, 0o755) # Make executable on Unix
                    
                    return True
            return False
        except Exception as e:
            print(f"[LlamaCPP] Binary download failed: {e}")
            return False

    def list_models(self) -> List[str]:
        """List all .gguf models in the models directory."""
        if not os.path.exists(self.models_dir):
            return []
        return [f for f in os.listdir(self.models_dir) if f.endswith(".gguf")]

    def download_model(self, url: str, filename: str, progress_callback=None) -> bool:
        """Download a GGUF model from a URL."""
        if not filename.endswith(".gguf"):
            filename += ".gguf"
        
        target_path = os.path.join(self.models_dir, filename)
        try:
            print(f"[LlamaCPP] Downloading model to {target_path}...")
            resp = requests.get(url, stream=True)
            resp.raise_for_status()
            
            total_size = int(resp.headers.get('content-length', 0))
            downloaded = 0
            
            with open(target_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback and total_size > 0:
                            progress_callback(downloaded / total_size)
            return True
        except Exception as e:
            print(f"[LlamaCPP] Model download failed: {e}")
            return False

    def is_running(self) -> bool:
        """Check if llama-server is already responding."""
        try:
            resp = requests.get(f"http://localhost:{self.port}/health", timeout=1)
            return resp.status_code == 200
        except:
            return False

    def start(self, model_filename: str):
        """Start the llama-server with the specified model."""
        if self.is_running():
            print(f"[LlamaCPP] Already running on port {self.port}")
            return
        
        model_path = os.path.join(self.models_dir, model_filename)
        if not os.path.exists(model_path):
            print(f"[LlamaCPP] Model NOT found: {model_path}")
            return

        print(f"[LlamaCPP] Starting server with model {model_filename} on port {self.port}...")
        
        # Command to start llama-server
        cmd = [
            self.exe_path,
            "--model", model_path,
            "--port", str(self.port),
            "--host", "127.0.0.1",
            "--n-gpu-layers", "-1" # Try to use all GPU layers by default
        ]

        try:
            kwargs = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "bufsize": 1,
                "universal_newlines": True
            }
            if os.name == 'nt':
                kwargs['creationflags'] = 0x08000000 # CREATE_NO_WINDOW
                
            self.process = subprocess.Popen(cmd, **kwargs)

            # Check if process died immediately
            time.sleep(0.5)
            if self.process.poll() is not None:
                raise Exception(f"Process died with code {self.process.returncode}")

            # Thread to log output (minimal)
            def log_output():
                proc = self.process
                if proc and proc.stdout:
                    for line in proc.stdout:
                        if self._stop_event.is_set():
                            break
                    proc.stdout.close()

            threading.Thread(target=log_output, daemon=True).start()
            print(f"[LlamaCPP] Process started with PID {self.process.pid}")
            return True
        except Exception as e:
            print(f"[LlamaCPP] Failed to start: {e}")
            self.process = None
            return False

    def stop(self):
        """Stop the llama-server process."""
        self._stop_event.set()
        proc = self.process
        if proc:
            print(f"[LlamaCPP] Stopping process {proc.pid}...")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            self.process = None

# Singleton instance
_llamacpp_manager: Optional[LlamaCppManager] = None

def get_llamacpp_manager() -> LlamaCppManager:
    global _llamacpp_manager
    if _llamacpp_manager is None:
        _llamacpp_manager = LlamaCppManager()
    return _llamacpp_manager
