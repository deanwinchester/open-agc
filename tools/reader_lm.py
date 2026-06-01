import os
import json
import time
import subprocess
import threading
import requests
from typing import Any, Dict, Optional
from tools.base import BaseTool

_READER_LM_MODEL = "reader-lm-1.5b-Q8_0.gguf"
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
        server_exe = os.path.join(_get_bin_dir(), "llama-server")
        if not os.path.exists(server_exe):
            # Try fallback paths
            alt = os.path.join(_get_bin_dir(), "llama-b8954", "llama-server")
            if os.path.exists(alt):
                server_exe = alt
            else:
                print(f"[ReaderLM] llama-server binary not found")
                return False

        print(f"[ReaderLM] Starting server on port {_READER_LM_PORT}...")
        cmd = [
            server_exe,
            "--model", model_path,
            "--port", str(_READER_LM_PORT),
            "--host", "127.0.0.1",
            "--n-gpu-layers", "-1",
            "--ctx-size", "16384",
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
        "使用 Reader-lm 小模型将 HTML 源码解析为干净的 Markdown 文本。"
        "当浏览器获取到的页面包含大量 HTML 噪音（导航、广告、脚本等）时使用。\n\n"
        "适用场景：\n"
        "- 从 browser_automation 获取的页面 HTML 需要提取正文\n"
        "- 需要保留标题、列表、表格结构\n"
        "- HTML 过大不能直接传给大模型\n\n"
        "注意：首次调用会自动下载模型（约 500MB），会提示等待。"
    )

    def execute(self, html: str = "", file_path: str = "", **kwargs) -> str:
        agent_ctx = kwargs.get("_agent_context")

        if not html and not file_path:
            return "[ReaderLM] 需要 html 或 file_path 参数。"

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
        return result

    def get_openai_schema(self) -> dict:
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
                            "description": "要转换的 HTML 源码（可选，html 和 file_path 二选一）"
                        },
                        "file_path": {
                            "type": "string",
                            "description": "包含 HTML 的文件路径（可选，html 和 file_path 二选一）"
                        }
                    }
                }
            }
        }
