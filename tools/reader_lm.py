import os
import sys
import json
import time
import subprocess
import threading
import requests
from typing import Any, Dict, Optional
from tools.base import BaseTool


def _check_hardware() -> tuple:
    """Check if hardware meets Reader-lm requirements.
    Returns (ok: bool, reason: str)."""
    # 1. Available disk space in models directory
    try:
        from core.paths import get_data_dir
        models_dir = os.path.join(get_data_dir(), "models")
        os.makedirs(models_dir, exist_ok=True)
        if sys.platform == "win32":
            free_bytes = 0
            try:
                import ctypes
                free_bytes = ctypes.c_ulonglong(0)
                ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                    ctypes.c_wchar_p(models_dir), None, None, ctypes.pointer(free_bytes)
                )
                free_bytes = free_bytes.value
            except Exception:
                free_bytes = 0
            if free_bytes > 0 and free_bytes < 1_000_000_000:  # 1GB minimum
                return False, f"磁盘空间不足（{free_bytes // (1024**3)}GB 可用），Reader-lm 需要至少 1GB 空闲空间"
        else:
            stat = os.statvfs(models_dir)
            free_bytes = stat.f_frsize * stat.f_bavail
            if free_bytes < 1_000_000_000:
                return False, f"磁盘空间不足（{free_bytes // (1024**3)}GB 可用），Reader-lm 需要至少 1GB 空闲空间"
    except Exception:
        pass  # Skip disk check if we can't determine

    # 2. Memory check (using psutil if available)
    try:
        import psutil
        mem = psutil.virtual_memory()
        if mem.total < 2_000_000_000:  # 2GB minimum total RAM
            return False, f"内存不足（{mem.total // (1024**3)}GB），Reader-lm 需要至少 2GB 内存"
        if mem.available < 500_000_000:  # 500MB minimum available
            return False, f"可用内存不足（{mem.available // (1024**2)}MB），Reader-lm 需要至少 500MB 可用内存"
    except Exception:
        pass  # psutil unavailable or failed, skip memory check

    # 3. CPU check -- at least 2 cores
    try:
        cpu_count = os.cpu_count() or 0
        if cpu_count < 2:
            return False, f"CPU 核心数不足（{cpu_count}核），Reader-lm 需要至少 2 核 CPU"
    except Exception:
        pass

    # 4. Architecture check -- must be 64-bit
    is_64bit = sys.maxsize > 2**32
    if not is_64bit:
        return False, "Reader-lm 需要 64 位操作系统"

    # 5. GPU check (optional -- Reader-lm 默认 CPU 运行)
    try:
        import subprocess as _sp
        _r = _sp.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                     capture_output=True, text=True, timeout=5)
        if _r.returncode == 0 and _r.stdout.strip():
            for _l in _r.stdout.strip().split("\n"):
                _p = _l.split(",")
                if len(_p) >= 2:
                    try:
                        _mn = int(_p[1].replace("MiB", "").strip())
                        if _mn < 1024:
                            return False, f"GPU 显存不足（{_p[0].strip()} {_p[1].strip()}），Reader-lm 需要至少 1GB 显存"
                    except Exception:
                        pass
    except Exception:
        pass  # nvidia-smi not available, GPU check skipped

    return True, ""

# Pre-compute hardware capability at module load time
_HARDWARE_OK, _HARDWARE_REASON = _check_hardware()

_READER_LM_MODEL = "reader-lm-0.5b.Q8_0.gguf"
_READER_LM_PORT = 8082
_READER_LM_SERVER: Optional[subprocess.Popen] = None
_READER_LM_LOCK = threading.Lock()


def _get_models_dir() -> str:
    from core.paths import get_data_dir
    d = os.path.join(get_data_dir(), "models")
    os.makedirs(d, exist_ok=True)
    return d


def _get_bin_dir() -> str:
    from core.paths import get_data_dir
    d = os.path.join(get_data_dir(), "bin")
    os.makedirs(d, exist_ok=True)
    return d


def _download_model() -> bool:
    """Download Reader-lm GGUF from ModelScope if not present."""
    model_path = os.path.join(_get_models_dir(), _READER_LM_MODEL)
    if os.path.exists(model_path):
        return True

    print(f"[ReaderLM] Model not found, downloading from ModelScope...")
    model_id = "BAAI/Reader-LM"
    filename = _READER_LM_MODEL
    url = f"https://modelscope.cn/models/{model_id}/resolve/master/{filename}"

    try:
        import requests as req
        resp = req.get(url, stream=True, timeout=30)
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        with open(model_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
        print(f"[ReaderLM] Download complete: {model_path}")
        return True
    except Exception as e:
        print(f"[ReaderLM] Download failed: {e}")
        return False


def _ensure_server() -> bool:
    """Start llama-server for Reader-lm if not already running."""
    global _READER_LM_SERVER

    with _READER_LM_LOCK:
        if _READER_LM_SERVER is not None:
            # Check if still alive
            try:
                r = requests.get(f"http://127.0.0.1:{_READER_LM_PORT}/health", timeout=0.5)
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            _READER_LM_SERVER = None

        model_path = os.path.join(_get_models_dir(), _READER_LM_MODEL)
        if not os.path.exists(model_path):
            if not _download_model():
                return False

        # Find llama-server binary
        bin_dir = _get_bin_dir()
        server_exe = os.path.join(bin_dir, "llama-server")
        if sys.platform == "win32" and not server_exe.endswith(".exe"):
            server_exe += ".exe"
        if not os.path.exists(server_exe):
            # Try fallback paths
            alt = os.path.join(bin_dir, "llama-b8954", "llama-server")
            if sys.platform == "win32" and not alt.endswith(".exe"):
                alt += ".exe"
            if os.path.exists(alt):
                server_exe = alt
            else:
                print(f"[ReaderLM] llama-server binary not found at {server_exe}")
                return False

        # Use configured ctx-size or default to 65536
        try:
            _cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "config.json")
            if os.path.exists(_cfg_path):
                with open(_cfg_path) as _f:
                    _cfg = json.load(_f)
                _ctx = _cfg.get("llamacpp_ctx_size", 65536)
            else:
                _ctx = 65536
        except Exception:
            _ctx = 65536
        print(f"[ReaderLM] Starting server on port {_READER_LM_PORT} (ctx={_ctx})...")
        cmd = [
            server_exe,
            "--model", model_path,
            "--port", str(_READER_LM_PORT),
            "--host", "127.0.0.1",
            "--n-gpu-layers", "-1",
            "--ctx-size", str(_ctx),
        ]

        try:
            _READER_LM_SERVER = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Wait for server to be ready (up to 30s)
            for i in range(30):
                time.sleep(1)
                try:
                    r = requests.get(f"http://127.0.0.1:{_READER_LM_PORT}/health", timeout=0.5)
                    if r.status_code == 200:
                        print(f"[ReaderLM] Server ready on port {_READER_LM_PORT}")
                        return True
                except Exception:
                    pass
            print(f"[ReaderLM] Server failed to start within 30s")
            _READER_LM_SERVER = None
            return False
        except Exception as e:
            print(f"[ReaderLM] Failed to start server: {e}")
            _READER_LM_SERVER = None
            return False


def _convert_html(html: str) -> Optional[str]:
    """Send HTML to Reader-lm and return markdown."""
    if not _ensure_server():
        return None

    # Truncate very long HTML
    if len(html) > 50000:
        html = html[:50000] + "\n<!-- [truncated] -->"

    # Use Qwen2.5 chat template format (ReaderLM-0.5B is based on Qwen2.5)
    prompt = (
        f"<|im_start|>system\nConvert the following HTML to Markdown. "
        f"Return ONLY the markdown output, no extra text.<|im_end|>\n"
        f"<|im_start|>user\n{html}<|im_end|>\n"
        f"<|im_start|>assistant\n"
    )

    try:
        # Use ensure_ascii=False to send UTF-8 directly (not as \\uXXXX escapes)
        _body = json.dumps({
            "prompt": prompt,
            "temperature": 0.0,
            "max_tokens": 4096,
            "stop": ["<|im_end|>", "<|endoftext|>"],
        }, ensure_ascii=False).encode("utf-8")
        resp = requests.post(
            f"http://127.0.0.1:{_READER_LM_PORT}/v1/completions",
            data=_body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=120,
        )
        if resp.status_code == 200:
            # Parse JSON from raw bytes to prevent mojibake
            import json as _json
            data = _json.loads(resp.content)
            text = data.get("choices", [{}])[0].get("text", "")
            return text.strip()
        else:
            print(f"[ReaderLM] API error: {resp.status_code}")
            try:
                detail = resp.json()
                print(f"[ReaderLM] Error detail: {str(detail)[:300]}")
            except Exception:
                print(f"[ReaderLM] Response: {resp.text[:300]}")
            return None
    except Exception as e:
        print(f"[ReaderLM] API call failed: {e}")
        return None


class ReaderLMTool(BaseTool):
    name: str = "parse_html"
    description: str = (
        "用 Reader-lm 小模型把 HTML 或 URL 解析为干净 Markdown。"
        "页面噪音大时用它提取正文；首次调用自动下载模型（约 500MB）。"
    )

    @staticmethod
    def is_available() -> bool:
        return _HARDWARE_OK

    def execute(self, html: str = "", file_path: str = "", url: str = "", save_path: str = "", **kwargs) -> str:
        agent_ctx = kwargs.get("_agent_context")

        if not html and not file_path and not url:
            return "[ReaderLM] 需要 html、file_path 或 url 参数。"

        if url:
            try:
                import requests as req
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Referer": "https://www.google.com/",
                }
                resp = req.get(url, timeout=30, headers=headers)
                resp.raise_for_status()
                html = resp.text
            except Exception as e:
                return (f"[ReaderLM] 直接获取 URL 失败: {e}\n\n"
                        "建议先用 browser_automation 打开该页面获取完整 HTML，"
                        "再将得到的 HTML 传入 parse_html 工具。")

        if file_path:
            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    html = f.read()
            except Exception as e:
                return f"[ReaderLM] 读取文件失败: {e}"

        if not html or not html.strip():
            return "[ReaderLM] HTML 内容为空。"

        # Check if model exists first — if not, tell agent to wait
        model_path = os.path.join(_get_models_dir(), _READER_LM_MODEL)
        if not os.path.exists(model_path):
            if not _download_model():
                return (
                    "[ReaderLM] 模型下载失败。请检查网络连接后重试。\n"
                    "模型地址: https://modelscope.cn/models/BAAI/Reader-LM"
                )
            return "[ReaderLM] 模型下载完成，请重新调用 parse_html 解析页面。"

        result = _convert_html(html)
        if result is None:
            return "[ReaderLM] 解析失败。请稍后重试。"

        if save_path:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
                with open(save_path, "w", encoding="utf-8") as f:
                    f.write(result)
                return f"[ReaderLM] Markdown 已保存到 {save_path}\n\n{result[:2000]}"
            except Exception as e:
                return f"[ReaderLM] 保存文件失败: {e}\n\n{result}"

        return result

    def get_openai_schema(self) -> dict:
        if not _HARDWARE_OK:
            return None
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "html": {
                            "type": "string",
                            "description": "HTML 源码（html/file_path/url 三选一）。"
                        },
                        "file_path": {
                            "type": "string",
                            "description": "HTML 文件路径（三选一）。"
                        },
                        "url": {
                            "type": "string",
                            "description": "网页 URL，自动抓取解析（三选一）。"
                        },
                        "save_path": {
                            "type": "string",
                            "description": "可选，Markdown 保存路径。"
                        }
                    }
                }
            }
        }
