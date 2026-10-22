# -*- coding: utf-8 -*-
"""文档 OCR 接口：POST /api/ocr 对沙箱内扫描版 PDF / 图片出全文。

路径只放行沙箱 uploads/ downloads/ outputs/ 三个分区（realpath 判据与
routes_sandbox/deliverables_registry 同口径），防目录逃逸。OCR 是 CPU/远程
密集操作，走 run_in_executor 不阻塞事件循环（与 routes_settings 同模式）。
实际 OCR 逻辑在 tools/pdf_ocr.py（远程 GPU 服务优先，本地 rapidocr 兜底）。
"""
import asyncio
import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.deliverables_registry import sandbox_root, is_under_root
from tools.pdf_ocr import ocr_pdf

router = APIRouter(prefix="", tags=["ocr"])

# 只允许 OCR 沙箱内这三个分区下的文件（用户上传 / 下载 / 任务产出）
_ALLOWED_PREFIXES = ("uploads", "downloads", "outputs")

_ALLOWED_EXTS = {".pdf", ".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif",
                 ".tif", ".tiff"}


class OcrRequest(BaseModel):
    path: str                 # 沙箱相对路径，如 "uploads/xxx.pdf"
    max_pages: int = 10


def _safe_ocr_path(rel: str) -> str:
    """解析沙箱相对路径并校验：必须在 uploads/downloads/outputs 分区下，
    且 realpath 不逃逸出沙箱根。"""
    rel = str(rel or "").strip().replace("\\", "/").strip("/")
    parts = rel.split("/")
    if not rel or parts[0] not in _ALLOWED_PREFIXES \
            or any(p in ("", ".", "..") for p in parts):
        raise HTTPException(
            status_code=403,
            detail="仅允许 OCR 沙箱 uploads/downloads/outputs 下的文件")
    root = sandbox_root()
    full = os.path.join(root, *parts)
    if not is_under_root(root, full):
        raise HTTPException(status_code=403, detail="Forbidden directory traversal")
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="File not found")
    ext = os.path.splitext(full)[1].lower()
    if ext not in _ALLOWED_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型 '{ext}'（支持 PDF 及常见图片）")
    return full


@router.post("/api/ocr")
async def ocr_document_api(req: OcrRequest):
    full = _safe_ocr_path(req.path)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, ocr_pdf, full, req.max_pages)
    if result.get("error"):
        raise HTTPException(status_code=500, detail=result["error"])
    return {
        "text": result["text"],
        "pages": result["pages"],
        "engine": result["engine"],
        "truncated": result["truncated"],
        "cached": result.get("cached", False),
    }
