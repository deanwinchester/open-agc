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
        "下载管理器：下载模型/大文件 + 管理下载记录。action=queue（默认）排队下载，"
        "立即返回、后台运行带进度追踪，支持 HuggingFace、ModelScope 和直接 URL、"
        "断点续传；action=list 查看全部下载记录（状态/进度/来源）；"
        "action=resume 续传已暂停/失败的下载（需 download_id）；"
        "action=delete 删除记录（连带清理 partial 残留文件）。"
        "凡下载超过 100MB 的文件必须用本工具——禁止用 execute_shell/execute_python "
        "自编脚本下载（无记录、不能续传、会造成重复下载浪费）。"
        "重复提交会自动识别：已下载完成/已有部分进度时直接返回现状。"
    )

    def __init__(self, models_dir: str = None, **kwargs):
        super().__init__(**kwargs)
        self.models_dir = models_dir

    def __init__(self, models_dir: str = None, **kwargs):
        super().__init__(**kwargs)
        self.models_dir = models_dir

    def execute(self, url: str = "", repo_id: str = "", filename: str = "",
                source: str = "huggingface", action: str = "queue",
                download_id: int = None, **kwargs) -> str:
        """下载管理：queue（默认）排队下载 / list 列记录 / resume 续传 / delete 删记录。"""
        # ── 管理动作（list/resume/delete）──
        if action != "queue":
            return self._manage(action, download_id)

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

        # Check for existing download（大小写不敏感——HF 仓库原名与磁盘首次
        # 写入名可能大小写不同，Windows 下是同一个文件）
        try:
            import sqlite3
            db_path = get_data_path("chat_history.db")
            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                "SELECT id, status FROM downloads WHERE filename=? COLLATE NOCASE "
                "ORDER BY id DESC LIMIT 1",
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

        # 磁盘兜底：文件已在模型目录（可能由其他途径完成/落库丢失）→ 不重复下载
        try:
            from core.llamacpp_manager import get_llamacpp_manager as _glm
            from core.paths import resolve_sandbox_dir as _rsd
            from api.config import load_config as _lc
            _dirs = [_glm().models_dir]
            _sb = _rsd((_lc() or {}).get("sandbox_dir"))
            _dl = os.path.join(_sb, "downloads")
            if os.path.isdir(_dl):
                _dirs.append(_dl)  # agent 历史自行下载的落点
            for _md in _dirs:
                if not os.path.isdir(_md):
                    continue
                for f in os.listdir(_md):
                    if f.lower() == filename.lower():
                        return (f"'{filename}' 已存在于 {_md}，无需重复下载"
                                f"（如需装到模型目录，把它移动过去即可）。")
                    if f.lower() == (filename + ".partial").lower():
                        _sz = os.path.getsize(os.path.join(_md, f))
                        return (f"'{filename}' 已有部分下载进度（{_sz // 1048576} MB，在 {_md}）。"
                                "请通过下载管理器续传，不要重新下载。")
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

        # Preflight: verify the file actually exists BEFORE creating any record.
        # A failed preflight returns an error directly — nothing is queued and
        # no downloads row is created (zero dirty data).
        if download_url.lower().startswith(("http://", "https://")):
            preflight_err = _preflight_download_url(download_url)
            if preflight_err:
                return preflight_err

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
                    success = _download_direct(
                        url=download_url,
                        target=f"{dl_dir}/{filename}",
                        progress_callback=progress_cb,
                    )

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
                    "下载管理器：下载模型/大文件 + 管理下载记录。action=queue（默认）排队下载，"
                    "后台运行带进度追踪、断点续传，支持 huggingface/modelscope/直链；"
                    "action=list 查看下载记录（状态/进度/来源）；action=resume 续传"
                    "已暂停/失败的下载（需 download_id）；action=delete 删除记录"
                    "（连带清理 partial 残留）。凡下载超过 100MB 的文件必须用本工具，"
                    "禁止用 execute_shell/execute_python 自编脚本下载。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["queue", "list", "resume", "delete"],
                            "description": "queue（默认）排队下载；list 列记录；resume 续传；delete 删记录。"
                        },
                        "download_id": {
                            "type": "integer",
                            "description": "resume/delete 时的记录 ID（从 action=list 获取）。"
                        },
                        "filename": {
                            "type": "string",
                            "description": "保存文件名（queue 时必填）。"
                        },
                        "repo_id": {
                            "type": "string",
                            "description": "仓库 ID（huggingface/modelscope 用）。"
                        },
                        "url": {
                            "type": "string",
                            "description": "直链地址（source=direct 用）。"
                        },
                        "source": {
                            "type": "string",
                            "enum": ["huggingface", "modelscope", "direct"],
                            "description": "下载源，默认 huggingface。"
                        }
                    },
                    "required": []
                }
            }
        }


    # ── 管理动作（与下载页同一数据源，复用 routes_settings 记录函数）──
    def _manage(self, action: str, download_id: int = None) -> str:
        import asyncio
        try:
            from api.routes.routes_settings import (
                list_download_records, resume_download, delete_download,
            )
        except ImportError as e:
            return f"Error: 下载系统不可用: {e}"

        if action == "list":
            rows = list_download_records()
            if not rows:
                return "下载管理器中没有记录。"
            lines = []
            for r in rows:
                size_mb = (r.get("total_size") or 0) / 1048576
                done_mb = (r.get("downloaded_bytes") or 0) / 1048576
                pct = round((r.get("progress") or 0) * 100)
                err = f"，错误: {r['error_message']}" if r.get("error_message") else ""
                lines.append(
                    f"- #{r['id']} [{r['status']}] {r.get('label') or r.get('filename')} "
                    f"— {done_mb:.0f}/{size_mb:.0f}MB ({pct}%)，来源 {r.get('source') or '?'}，"
                    f"创建于 {r.get('created_at')}{err}")
            return "下载记录（按创建倒序）：\n" + "\n".join(lines)

        if action == "resume":
            if not download_id:
                return "Error: resume 需要 download_id"
            try:
                asyncio.run(resume_download(int(download_id)))
                return f"已恢复下载 #{download_id}（断点续传，进度见下载管理面板）。"
            except Exception as e:
                return f"Error: 续传失败: {getattr(e, 'detail', str(e))}"

        if action == "delete":
            if not download_id:
                return "Error: delete 需要 download_id"
            try:
                asyncio.run(delete_download(int(download_id)))
                return f"已删除下载记录 #{download_id}（含 partial 残留文件）。"
            except Exception as e:
                return f"Error: 删除失败: {getattr(e, 'detail', str(e))}"

        return f"Error: 未知 action '{action}'（queue/list/resume/delete）"


def _preflight_download_url(url: str, timeout: float = 12.0) -> Optional[str]:
    """Verify an HTTP(S) download URL before a download record is created.

    Issues GET with ``Range: bytes=0-0`` (HEAD is rejected by many CDNs).
    200/206/301/302/307/308 mean the file is reachable; 416 (range not
    satisfiable) still proves the file exists. 404/410 and connection
    failures are hard rejections. Other statuses (401/403/5xx...) are
    inconclusive — the download proceeds and the downloader's own
    raise_for_status reports them accurately.

    Returns None when the URL passes, else an error message for the agent.
    """
    import requests
    try:
        with requests.get(url, stream=True, headers={"Range": "bytes=0-0"},
                          timeout=timeout, allow_redirects=True) as resp:
            code = resp.status_code
    except requests.RequestException as e:
        return (
            f"Error: 无法连接到下载源 ({e})，文件不存在或源不可用。\n"
            f"URL: {url}\n"
            "文件不存在或源不可用，换源前请先验证文件是否存在"
            "（用 search_web / fetch_url 确认仓库与文件名后再试）。"
        )
    if code in (200, 206, 301, 302, 307, 308, 416):
        return None
    if code in (404, 410):
        return (
            f"Error: 源服务器返回 HTTP {code}，目标文件不存在。\n"
            f"URL: {url}\n"
            "文件不存在或源不可用，换源前请先验证文件是否存在"
            "（用 search_web / fetch_url 确认仓库与文件名后再试）。"
        )
    print(f"[Download] Preflight got HTTP {code} for {url} — proceeding anyway")
    return None


def _download_direct(url: str, target: str, progress_callback=None) -> bool:
    """Download a file over HTTP(S) with resume support.

    Raises on HTTP errors (404/500/...) so an error page is never saved as
    the target file, and sanity-checks the final size against Content-Length
    when the server provided one. Returns True on success.
    """
    import requests
    partial = target + ".partial"
    resume_offset = 0
    headers = {}
    if os.path.exists(partial):
        resume_offset = os.path.getsize(partial)
        headers["Range"] = f"bytes={resume_offset}-"
    with requests.get(url, stream=True, headers=headers, timeout=30) as resp:
        # Reject HTTP error pages (404/500...) instead of writing them to disk.
        resp.raise_for_status()
        if resp.status_code not in (200, 206):
            raise RuntimeError(f"Unexpected HTTP status {resp.status_code} for {url}")
        total = int(resp.headers.get("content-length", 0)) + (resume_offset if resp.status_code == 206 else 0)
        mode = "ab" if resp.status_code == 206 else "wb"
        downloaded = resume_offset if mode == "ab" else 0
        with open(partial, mode) as f:
            for chunk in resp.iter_content(8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total > 0 and progress_callback:
                        progress_callback(downloaded / total)
    # Size sanity check: when the server told us the size, the completed file
    # must match it exactly; an empty result is always a failure.
    final_size = os.path.getsize(partial)
    if total > 0 and final_size != total:
        raise RuntimeError(
            f"Download incomplete: got {final_size} bytes, expected {total} ({url})"
        )
    if final_size == 0:
        raise RuntimeError(f"Download produced an empty file ({url})")
    os.replace(partial, target)
    return True


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
