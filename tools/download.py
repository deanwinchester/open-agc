import os
import threading
from typing import Optional

from tools.base import BaseTool

# Session-scoped pending download IDs, linked to tasks when created
_pending_task_links: dict = {}  # {session_id: [download_id, ...]}

class DownloadTool(BaseTool):
    """Queue a download in the background. Returns immediately, does NOT block."""
    model_config = {"extra": "allow", "arbitrary_types_allowed": True}
    
    name: str = "queue_download"
    description: str = (
        "通过系统下载管理器异步下载模型文件。立即返回，下载在后台运行并带有进度追踪。"
        "支持 HuggingFace, ModelScope 和直接 URL 链接。支持断点续传。"
    )

    def __init__(self, models_dir: str = None, **kwargs):
        super().__init__(**kwargs)
        self.models_dir = models_dir

    def execute(self, url: str = "", repo_id: str = "", filename: str = "",
                source: str = "huggingface", **kwargs) -> str:
        """Queue a model download. Supports HuggingFace, ModelScope, and direct URLs.

        Args:
            url: Direct download URL (use with source='direct').
            repo_id: HF repo ID like 'Qwen/Qwen2-7B-Instruct-GGUF'.
            filename: Target filename (e.g. 'qwen2-7b.gguf').
            source: 'huggingface', 'modelscope', or 'direct'.
        """
        try:
            from core.llamacpp_manager import get_llamacpp_manager
            from core.paths import get_data_path
            from api.state import _llamacpp_download_state, _broadcast_to_websockets
            from api.routes.routes_settings import (
                create_download_record, update_download_progress, log_download_event,
            )
        except ImportError as e:
            return f"Error: Cannot access download system: {e}"

        if not filename:
            return "Error: Please provide a filename for the download."

        if not filename:
            return "Error: Please provide a filename for the download."

        # Sanitize: strip any directory components, reject separators/traversal
        filename = os.path.basename(filename.replace("\\", "/"))
        if not filename or filename in (".", "..") or "/" in filename or "\\" in filename:
            return "Error: Invalid filename."

        # Check for existing download
        try:
            import sqlite3
            db_path = get_data_path("chat_history.db")
            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                "SELECT id, status FROM downloads WHERE filename=? ORDER BY id DESC LIMIT 1",
                (filename,)).fetchall()
            conn.close()
            if rows:
                existing = rows[0]
                if existing[1] in ('downloading', 'paused'):
                    return (
                        f"Download '{filename}' already exists (id={existing[0]}, status={existing[1]}). "
                        "It can be resumed from the download manager."
                    )
                elif existing[1] == 'completed':
                    return (
                        f"Download '{filename}' already completed (id={existing[0]}). "
                        "File is ready in the models directory."
                    )
        except Exception:
            pass

        mgr = get_llamacpp_manager()

        # FTP: use dedicated FTP download handler
        is_ftp = url and url.lower().startswith("ftp://") if url else False

        # Determine download directory: models/ for GGUF, downloads/ for everything else
        from core.paths import get_data_path as _gdp
        is_gguf = filename.lower().endswith('.gguf')
        dl_type = 'model' if is_gguf else 'file'
        dl_dir = mgr.models_dir if is_gguf else _gdp("downloads")
        os.makedirs(dl_dir, exist_ok=True)

        # Build download label
        if url and source == 'direct':
            label = f"{filename} (direct)"
            download_url = url
        elif repo_id:
            label = f"{repo_id}/{filename}"
            from urllib.parse import quote
            if source == 'modelscope':
                download_url = f"https://modelscope.cn/api/v1/models/{repo_id}/repo?Revision=master&FilePath={quote(filename)}"
            else:
                download_url = f"https://huggingface.co/{repo_id}/resolve/main/{quote(filename)}"
        else:
            return "Error: Provide either 'url' (with source='direct') or 'repo_id' (with source='huggingface'/'modelscope')."

        # Create DB record (link to task if available)
        try:
            task_id = kwargs.get("_task_id")
            record_id = create_download_record(
                type_=dl_type,
                label=label,
                repo_id=repo_id,
                filename=filename,
                source=source,
                url=download_url,
                target_path=f"{dl_dir}/{filename}",
                partial_path=f"{dl_dir}/{filename}.partial",
                task_id=task_id
            )
        except Exception as e:
            return f"Error creating download record: {e}"

        # Register for session→task linking (server will assign task_id later)
        sid = kwargs.get("_session_id")
        if sid is not None and record_id:
            _pending_task_links.setdefault(sid, []).append(record_id)
            print(f"[Download] Pending task link: session={sid} dl_id={record_id}")

        # Start background download
        def _download_thread():
            try:
                log_download_event(record_id, "started", f"开始下载: {label}", f"url={download_url}")
                slot_key = f"{dl_type}_{record_id}"
                _llamacpp_download_state[slot_key] = {
                    "active": True, "type": dl_type, "label": label, "id": record_id,
                    "progress": 0.0, "stage": "downloading", "error": "", "cancelled": False
                }
                _llamacpp_download_state["active"] = True
                _llamacpp_download_state["cancelled"] = False
                _broadcast_to_websockets({
                    "type": "llamacpp_download",
                    "download_id": record_id,
                    "task": dl_type, "label": label,
                    "progress": 0.0, "stage": "downloading", "error": ""
                })

                def progress_cb(pct):
                    if _llamacpp_download_state.get("cancelled"):
                        return
                    from api.routes.routes_settings import update_download_progress
                    _llamacpp_download_state[slot_key]["progress"] = pct
                    update_download_progress(record_id, pct, status='downloading')
                    _broadcast_to_websockets({
                        "type": "llamacpp_download",
                        "download_id": record_id,
                        "task": dl_type, "label": label,
                        "progress": pct, "stage": "downloading", "error": ""
                    })

                if _llamacpp_download_state.get("cancelled"):
                    return

                if is_ftp:
                    print(f"[Download] FTP download starting: {download_url} -> {dl_dir}/{filename}")
                    success = _download_ftp(
                        url=download_url,
                        target=f"{dl_dir}/{filename}",
                        progress_callback=progress_cb
                    )
                    print(f"[Download] FTP download result: {'success' if success else 'failed'}")
                elif is_gguf:
                    success = mgr.download_model(
                        url=download_url,
                        filename=filename,
                        progress_callback=progress_cb,
                        resume=True
                    )
                else:
                    import requests
                    target = f"{dl_dir}/{filename}"
                    partial = target + ".partial"
                    resume_offset = 0
                    headers = {}
                    if os.path.exists(partial):
                        resume_offset = os.path.getsize(partial)
                        headers["Range"] = f"bytes={resume_offset}-"
                    resp = requests.get(download_url, stream=True, headers=headers, timeout=30)
                    total = int(resp.headers.get("content-length", 0)) + (resume_offset if resp.status_code == 206 else 0)
                    mode = "ab" if resp.status_code == 206 else "wb"
                    downloaded = resume_offset if mode == "ab" else 0
                    with open(partial, mode) as f:
                        for chunk in resp.iter_content(8192):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                if total > 0 and progress_cb:
                                    progress_cb(downloaded / total)
                    os.replace(partial, target)
                    success = True

                # Check if cancelled before broadcasting complete/error
                if _llamacpp_download_state.get("cancelled"):
                    return

                if success:
                    from api.routes.routes_settings import update_download_progress
                    update_download_progress(record_id, 1.0, status='completed')
                    _llamacpp_download_state[slot_key].update({
                        "active": False, "progress": 1.0,
                        "stage": "complete", "error": ""
                    })
                    # Only set top-level active=False when ALL slots are done
                    if not any(isinstance(v, dict) and v.get("active") for k, v in _llamacpp_download_state.items() if k != slot_key):
                        _llamacpp_download_state["active"] = False
                    _broadcast_to_websockets({
                        "type": "llamacpp_download",
                        "download_id": record_id,
                        "task": dl_type, "label": label,
                        "progress": 1.0, "stage": "complete", "error": ""
                    })
                else:
                    raise RuntimeError("Download failed")

            except Exception as e:
                if _llamacpp_download_state.get("cancelled"):
                    return
                err_msg = str(e)
                print(f"[Download] EXCEPTION in download thread #{record_id}: {err_msg}")
                from api.routes.routes_settings import update_download_progress
                update_download_progress(record_id, None, status='failed', error_message=err_msg)
                # Also notify session directly if pending task link exists
                try:
                    sid = kwargs.get("_session_id")
                    if sid is not None:
                        linked_ids = _pending_task_links.get(sid, [])
                        if record_id in linked_ids:
                            print(f"[Download] download #{record_id} failed, pending link to session {sid} task (will notify via tool_done)")
                except Exception as notify_err:
                    print(f"[Download] Failed to check pending links: {notify_err}")
                _llamacpp_download_state[slot_key].update({
                    "active": False, "stage": "error", "error": err_msg
                })
                # Only set top-level active=False when ALL slots are done
                if not any(isinstance(v, dict) and v.get("active") for k, v in _llamacpp_download_state.items() if k != slot_key):
                    _llamacpp_download_state["active"] = False
                _broadcast_to_websockets({
                    "type": "llamacpp_download",
                    "download_id": record_id,
                    "task": dl_type, "label": label,
                    "progress": _llamacpp_download_state[slot_key].get("progress", 0),
                    "stage": "error", "error": err_msg
                })

        threading.Thread(target=_download_thread, daemon=True).start()

        return (
            f"Download queued successfully:\n"
            f"  ID: {record_id}\n"
            f"  File: {filename}\n"
            f"  Source: {source}\n"
            f"  Status: Downloading... (progress visible in download manager)\n"
            f"Auto-resume is enabled — if interrupted, the download will continue from where it left off."
        )

    def get_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "queue_download",
                "description": (
                    "Queue a model file download via the system download manager. "
                    "Returns immediately — downloads run in background with progress tracking. "
                    "Supports HuggingFace, ModelScope, and direct URLs. "
                    "Resume is automatic — interrupted downloads pick up from where they stopped."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": {
                            "type": "string",
                            "description": "Target filename to save as (e.g. 'qwen2-7b.gguf')."
                        },
                        "repo_id": {
                            "type": "string",
                            "description": "HuggingFace repo (e.g. 'Qwen/Qwen2-7B-Instruct-GGUF'). Use with source='huggingface' or 'modelscope'."
                        },
                        "url": {
                            "type": "string",
                            "description": "Direct download URL. Use with source='direct'."
                        },
                        "source": {
                            "type": "string",
                            "enum": ["huggingface", "modelscope", "direct"],
                            "description": "Download source. Default: 'huggingface'."
                        }
                    },
                    "required": ["filename"]
                }
            }
        }


def _download_ftp(url: str, target: str, progress_callback=None) -> bool:
    """Download a file via FTP with progress and resume support."""
    from urllib.parse import urlparse, unquote
    import ftplib
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or 21
    path = unquote(parsed.path).lstrip("/")
    user = parsed.username or "anonymous"
    pwd = parsed.password or "guest"
    try:
        ftp = ftplib.FTP()
        ftp.connect(host, port, timeout=30)
        ftp.login(user, pwd)
        ftp.voidcmd("TYPE I")
        total = ftp.size(path) or 0
        partial = target + ".partial"
        resume_offset = 0
        mode = "wb"
        if os.path.exists(partial):
            resume_offset = os.path.getsize(partial)
            if total > 0 and resume_offset >= total:
                os.replace(partial, target)
                ftp.quit()
                return True
            if resume_offset > 0:
                ftp.voidcmd(f"REST {resume_offset}")
                mode = "ab"
        downloaded = resume_offset
        with open(partial, mode) as fout:
            def cb(data):
                nonlocal downloaded
                fout.write(data)
                downloaded += len(data)
                if total > 0 and progress_callback:
                    progress_callback(downloaded / total)
            ftp.retrbinary(f"RETR {path}", cb, blocksize=8192)
        ftp.quit()
        if total == 0 or downloaded >= total * 0.99:
            os.replace(partial, target)
            return True
        return False
    except Exception as e:
        print(f"[Download] FTP error: {e}")
        return False
