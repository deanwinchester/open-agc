"""
File upload management — upload/download/delete user files within sandbox.
"""
import os
import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import FileResponse


router = APIRouter(prefix="", tags=["uploads"])

MAX_UPLOAD_MB = 500


def _load_sandbox_config():
    from core.paths import get_data_path
    config_path = get_data_path("config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"sandbox_dir": os.path.abspath(os.path.join(os.getcwd(), "workspace"))}


def _uploads_dir():
    cfg = _load_sandbox_config()
    sandbox_dir = cfg.get("sandbox_dir", os.path.abspath(os.path.join(os.getcwd(), "workspace")))
    return os.path.abspath(os.path.join(sandbox_dir, "uploads"))


def _safe_path(filename: str) -> str:
    """Resolve and validate that the given file lives under uploads/."""
    uploads = _uploads_dir()
    safe_name = os.path.basename(filename)
    full = os.path.abspath(os.path.join(uploads, safe_name))
    if os.path.commonpath([uploads, full]) != uploads:
        raise HTTPException(status_code=403, detail="Forbidden directory traversal")
    return full


@router.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    uploads = _uploads_dir()
    os.makedirs(uploads, exist_ok=True)

    safe_name = os.path.basename(file.filename or "untitled")
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid filename")

    target = os.path.join(uploads, safe_name)
    # path-traversal guard: double-check after joining
    if os.path.commonpath([uploads, os.path.abspath(target)]) != uploads:
        raise HTTPException(status_code=403, detail="Forbidden filename")

    total = 0
    limit = MAX_UPLOAD_MB * 1024 * 1024
    try:
        with open(target, "wb") as f:
            while True:
                chunk = await file.read(8 * 1024 * 1024)  # 8 MB chunks
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    f.close()
                    try:
                        os.remove(target)
                    except PermissionError:
                        pass
                    raise HTTPException(status_code=413, detail=f"File exceeds {MAX_UPLOAD_MB} MB limit")
                f.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        if os.path.exists(target):
            try:
                os.remove(target)
            except PermissionError:
                # File may be locked by another process (e.g. virus scanner), schedule for later cleanup
                import time as _t
                _t.sleep(0.5)  # brief wait, then retry once
                try:
                    os.remove(target)
                except PermissionError:
                    print(f"[Upload] Could not delete partial file: {target}")
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")

    return {
        "status": "success",
        "filename": safe_name,
        "size": total,
        "path": f"uploads/{safe_name}",
    }


@router.get("/api/uploads")
async def list_uploads():
    uploads = _uploads_dir()
    if not os.path.isdir(uploads):
        return {"files": []}

    files = []
    for name in os.listdir(uploads):
        fp = os.path.join(uploads, name)
        if os.path.isfile(fp):
            st = os.stat(fp)
            files.append({
                "name": name,
                "size": st.st_size,
                "modified": st.st_mtime,
                "modified_iso": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            })

    files.sort(key=lambda f: f["modified"], reverse=True)
    return {"files": files}


@router.get("/api/upload/{filename:path}")
async def download_uploaded(filename: str):
    full = _safe_path(filename)
    if not os.path.exists(full) or not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(full, filename=os.path.basename(filename))


@router.delete("/api/upload/{filename:path}")
async def delete_uploaded(filename: str):
    full = _safe_path(filename)
    if not os.path.exists(full) or not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="File not found")
    os.remove(full)
    return {"status": "success", "filename": os.path.basename(filename)}
