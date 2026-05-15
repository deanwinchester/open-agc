"""
DownloadTool — Queue downloads via system download manager.
Supports resume on partial downloads via LlamaCppManager.
"""
import threading
from typing import Optional


class DownloadTool:
    """Queue a download in the background. Returns immediately, does NOT block."""

    def __init__(self, models_dir: str = None):
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
            from api.server import (
                create_download_record, _llamacpp_download_state,
                _broadcast_to_websockets, get_data_path
            )
        except ImportError as e:
            return f"Error: Cannot access download system: {e}"

        if not filename:
            return "Error: Please provide a filename for the download."

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

        # Create DB record
        try:
            record_id = create_download_record(
                type_='model',
                label=label,
                repo_id=repo_id,
                filename=filename,
                source=source,
                url=download_url,
                target_path=f"{mgr.models_dir}/{filename}",
                partial_path=f"{mgr.models_dir}/{filename}.partial"
            )
        except Exception as e:
            return f"Error creating download record: {e}"

        # Start background download
        def _download_thread():
            try:
                _llamacpp_download_state.update({
                    "active": True, "type": "model", "label": label,
                    "progress": 0.0, "stage": "downloading", "error": ""
                })
                _broadcast_to_websockets({
                    "type": "llamacpp_download",
                    "task": "model", "label": label,
                    "progress": 0.0, "stage": "downloading", "error": ""
                })

                def progress_cb(pct):
                    from api.server import update_download_progress
                    _llamacpp_download_state["progress"] = pct
                    update_download_progress(record_id, pct, 'downloading')
                    _broadcast_to_websockets({
                        "type": "llamacpp_download",
                        "task": "model", "label": label,
                        "progress": pct, "stage": "downloading", "error": ""
                    })

                success = mgr.download_model(
                    url=download_url,
                    filename=filename,
                    progress_callback=progress_cb,
                    resume=True
                )

                if success:
                    from api.server import update_download_progress
                    update_download_progress(record_id, 1.0, 'completed')
                    _llamacpp_download_state.update({
                        "active": False, "progress": 1.0,
                        "stage": "complete", "error": ""
                    })
                    _broadcast_to_websockets({
                        "type": "llamacpp_download",
                        "task": "model", "label": label,
                        "progress": 1.0, "stage": "complete", "error": ""
                    })
                else:
                    raise RuntimeError("Download failed")

            except Exception as e:
                from api.server import update_download_progress
                err_msg = str(e)
                update_download_progress(record_id, None, 'failed', err_msg)
                _llamacpp_download_state.update({
                    "active": False, "stage": "error", "error": err_msg
                })
                _broadcast_to_websockets({
                    "type": "llamacpp_download",
                    "task": "model", "label": label,
                    "progress": _llamacpp_download_state.get("progress", 0),
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
