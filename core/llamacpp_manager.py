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
                    
                    # Ensure llama-server and libraries exist in the root bin_dir
                    for root, dirs, files in os.walk(self.bin_dir):
                        if root == self.bin_dir:
                            continue
                        for f in files:
                            if f == self.exe_name or f.endswith(".dylib") or f.endswith(".so") or f.endswith(".dll"):
                                target = os.path.join(self.bin_dir, f)
                                if not os.path.exists(target):
                                    os.rename(os.path.join(root, f), target)
                    
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

    def download_model(self, url: str, filename: str, progress_callback=None, resume: bool = True) -> bool:
        """Download a GGUF model from a URL. Supports HTTP Range resume when resume=True."""
        if not filename.endswith(".gguf"):
            filename += ".gguf"

        target_path = os.path.join(self.models_dir, filename)
        partial_path = target_path + ".partial"

        try:
            headers = {}
            resume_offset = 0
            total_size = 0

            if resume and os.path.exists(partial_path):
                resume_offset = os.path.getsize(partial_path)
                headers["Range"] = f"bytes={resume_offset}-"
                print(f"[LlamaCPP] Resuming download from byte {resume_offset}...")

            print(f"[LlamaCPP] Downloading model to {target_path}...")
            resp = requests.get(url, stream=True, headers=headers, timeout=30)
            resp.raise_for_status()

            # Determine total size and whether resume was accepted
            if resp.status_code == 206:
                # Server accepted range request
                content_range = resp.headers.get("Content-Range", "")
                if "/" in content_range:
                    total_size = int(content_range.split("/")[-1])
                if total_size == 0:
                    total_size = int(resp.headers.get("content-length", 0)) + resume_offset
                mode = "ab"
                downloaded = resume_offset
            else:
                # Server doesn't support range or no partial file; fresh download
                total_size = int(resp.headers.get("content-length", 0))
                resume_offset = 0
                mode = "wb"
                downloaded = 0

            with open(partial_path, mode) as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback and total_size > 0:
                            progress_callback(downloaded / total_size)

            # Download complete — rename partial to final
            if os.path.exists(target_path):
                os.remove(target_path)
            os.rename(partial_path, target_path)
            return True
        except Exception as e:
            print(f"[LlamaCPP] Model download failed: {e}")
            # Keep .partial file for future resume
            return False

    def search_hf_models(self, query: str, limit: int = 20) -> List[dict]:
        """Search HuggingFace Hub for GGUF models by name."""
        try:
            url = "https://huggingface.co/api/models"
            params = {
                "search": query,
                "filter": "gguf",
                "sort": "downloads",
                "limit": limit,
                "full": "false"
            }
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            results = []
            for item in resp.json():
                results.append({
                    "repo_id": item.get("id", ""),
                    "author": item.get("author", ""),
                    "downloads": item.get("downloads", 0),
                    "likes": item.get("likes", 0),
                    "description": (item.get("description") or "")[:200]
                })
            return results
        except Exception as e:
            print(f"[LlamaCPP] HF search failed: {e}")
            return []

    def get_hf_model_files(self, repo_id: str) -> List[dict]:
        """List GGUF files in a HuggingFace model repo."""
        try:
            url = f"https://huggingface.co/api/models/{repo_id}"
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            files = []
            for sibling in data.get("siblings", []):
                fname = sibling.get("rfilename", "")
                if fname.endswith(".gguf"):
                    size_bytes = sibling.get("size", 0)
                    if size_bytes >= 1024**3:
                        size_str = f"{size_bytes / 1024**3:.2f} GB"
                    elif size_bytes >= 1024**2:
                        size_str = f"{size_bytes / 1024**2:.1f} MB"
                    elif size_bytes >= 1024:
                        size_str = f"{size_bytes / 1024:.1f} KB"
                    else:
                        size_str = f"{size_bytes} B"
                    files.append({"filename": fname, "size": size_str, "size_bytes": size_bytes})
            return files
        except Exception as e:
            print(f"[LlamaCPP] HF model files failed: {e}")
            return []

    def download_model_from_hf(self, repo_id: str, filename: str, progress_callback=None, resume: bool = True) -> bool:
        """Download a GGUF model from HuggingFace by repo_id and filename."""
        url = f"https://huggingface.co/{repo_id}/resolve/main/{filename}"
        local_name = filename.split("/")[-1]
        return self.download_model(url, local_name, progress_callback, resume=resume)

    # ---- ModelScope (modelscope.cn) integration ----

    MS_API_BASE = "https://modelscope.cn"

    def search_ms_models(self, query: str, limit: int = 20) -> List[dict]:
        """Search ModelScope for GGUF models by name."""
        try:
            url = f"{self.MS_API_BASE}/api/v1/models"
            body = {
                "Name": query,
                "PageNumber": 1,
                "PageSize": limit
            }
            resp = requests.put(url, json=body, timeout=15,
                               headers={"Content-Type": "application/json"})
            resp.raise_for_status()
            data = resp.json()
            results = []
            models = data.get("Data", {}).get("Models", [])
            if not models and isinstance(data.get("Data"), list):
                models = data["Data"]
            for item in models:
                path = item.get("Path", "")
                name = item.get("Name", "")
                repo_id = f"{path}/{name}" if path and name else (path or name)
                results.append({
                    "repo_id": repo_id,
                    "author": path or item.get("Author", "") or item.get("Owner", {}).get("Name", ""),
                    "downloads": item.get("Downloads", 0) or item.get("DownloadCount", 0),
                    "likes": item.get("Likes", 0) or item.get("LikeCount", 0),
                    "description": (item.get("Description", "") or "")[:200]
                })
            return results
        except Exception as e:
            print(f"[LlamaCPP] ModelScope search failed: {e}")
            return []

    def get_ms_model_files(self, repo_id: str) -> List[dict]:
        """List GGUF files in a ModelScope model repo."""
        try:
            url = f"{self.MS_API_BASE}/api/v1/models/{repo_id}/repo/files"
            params = {"Revision": "master", "Recursive": "false"}
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            files = []
            for f in data.get("Data", {}).get("Files", []):
                fname = f.get("Name", "")
                if fname.endswith(".gguf"):
                    size_bytes = f.get("Size", 0)
                    if size_bytes >= 1024**3:
                        size_str = f"{size_bytes / 1024**3:.2f} GB"
                    elif size_bytes >= 1024**2:
                        size_str = f"{size_bytes / 1024**2:.1f} MB"
                    elif size_bytes >= 1024:
                        size_str = f"{size_bytes / 1024:.1f} KB"
                    else:
                        size_str = f"{size_bytes} B"
                    files.append({"filename": fname, "size": size_str, "size_bytes": size_bytes})
            return files
        except Exception as e:
            print(f"[LlamaCPP] ModelScope file listing failed: {e}")
            return []

    def download_model_from_ms(self, repo_id: str, filename: str, progress_callback=None, resume: bool = True) -> bool:
        """Download a GGUF model from ModelScope by repo_id and filename."""
        url = f"{self.MS_API_BASE}/models/{repo_id}/resolve/master/{filename}"
        local_name = filename.split("/")[-1]
        return self.download_model(url, local_name, progress_callback, resume=resume)

    def is_running(self) -> bool:
        """Check if llama-server is already responding."""
        try:
            # Try /health first (standard endpoint)
            resp = requests.get(f"http://localhost:{self.port}/health", timeout=2)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        try:
            # Fallback: try /v1/models (OpenAI-compatible endpoint always available)
            resp = requests.get(f"http://localhost:{self.port}/v1/models", timeout=2)
            return resp.status_code == 200
        except Exception:
            return False

    def start(self, model_filename: str) -> bool:
        """Start the llama-server with the specified model. Returns True on success, False on failure."""
        if self.is_running():
            print(f"[LlamaCPP] Already running on port {self.port}")
            return True

        model_path = os.path.join(self.models_dir, model_filename)
        if not os.path.exists(model_path):
            print(f"[LlamaCPP] Model NOT found: {model_path}")
            return False

        print(f"[LlamaCPP] Starting server with model {model_filename} on port {self.port}...")

        # Read context size from config, default 32768
        ctx_size = 32768
        try:
            from core.paths import get_data_path
            import json
            config_path = get_data_path("config.json")
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    ctx_size = config.get("llamacpp_ctx_size", 32768)
        except Exception:
            pass

        # Command to start llama-server
        cmd = [
            self.exe_path,
            "--model", model_path,
            "--port", str(self.port),
            "--host", "127.0.0.1",
            "--n-gpu-layers", "-1",
            "--ctx-size", str(ctx_size)
        ]

        try:
            kwargs = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
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
