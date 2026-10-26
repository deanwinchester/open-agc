# -*- coding: utf-8 -*-
"""文档 OCR：扫描版 PDF / 图片出全文（远程 GPU 服务优先，本地 rapidocr 兜底）。

背景：zxs 产品线要 OCR 1954-2000 年的历史报纸 PDF（无文本层的扫描件）。
tools/screen_ocr.py 的 ocr_anchors() 是截图锚点用途（按置信度截断 40 条、
丢阅读顺序），不能复用做全文 OCR——本模块按阅读顺序（box 的 y 再 x）拼行。

双后端：
- 远程：config.json 配 ``ocr_remote_url`` 时启用，按 URL 形态分两种契约：
  - 以 ``/v1`` 结尾（如 http://192.168.148.200:8004/v1）：OpenAI 兼容
    视觉端点（vLLM 直接部署 Unlimited-OCR 的场景），走
    ``POST {url}/chat/completions``，模型取 ``ocr_remote_model``
    （默认 "Unlimited-OCR"），鉴权头取 ``ocr_remote_key``（空则不带），
    超时 180s（生产实证：vLLM 单页大图推理明显慢于薄服务）；
  - 其它（如 http://192.168.148.200:8377）：自建薄服务（部署见
    scripts/ocr_server/），每页渲染图 POST 到 ``{url}/ocr``
    （请求 {"image_b64", "page"}，响应 {"text"}，超时 120s）；
- 本地：远程未配置或调用失败时降级到 rapidocr 单例（复用
  screen_ocr._get_engine，use_cls=False）。rapidocr 无线程安全保证且
  CPU 密集，模块级 Semaphore(1) 串行化推理。

结果按文件内容 MD5 + max_pages 缓存到 data/ocr_cache/，命中直接返回。
pymupdf / rapidocr 任一缺失时返回带 error 字段的 dict（静默降级，不抛）。
"""
import base64
import hashlib
import io
import json
import os
import statistics
import threading

import requests

_DEFAULT_MAX_PAGES = 10
_MAX_PAGES_HARD = 50      # max_pages 硬顶，超出截断
_PDF_DPI = 170            # 渲染分辨率：清晰度和 CPU OCR 耗时的折中
_REMOTE_TIMEOUT = 120     # 远程 OCR 单页超时（秒，自建薄服务）
_REMOTE_TIMEOUT_VLLM = 180  # vLLM OpenAI 兼容端点超时（秒，大图推理更慢）

_IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif", ".tif", ".tiff"}

# 本地 rapidocr 推理串行化（无线程安全保证；CPU 密集也不该并发）
_local_sem = threading.Semaphore(1)

_fitz = None


def _get_fitz():
    """pymupdf 懒加载；未安装返回 None。"""
    global _fitz
    if _fitz is None:
        try:
            import pymupdf
            _fitz = pymupdf
        except Exception:
            try:
                import fitz  # 旧版包名兼容
                _fitz = fitz
            except Exception as e:
                print(f"[PdfOCR] pymupdf 不可用（pip install pymupdf）: {e}")
                _fitz = False
    return _fitz or None


def _remote_url() -> str:
    """config.json 的 ocr_remote_url（空 = 未配置远程后端）。"""
    try:
        from api.config import load_config
        cfg = load_config() or {}
    except Exception:
        cfg = {}
    return str(cfg.get("ocr_remote_url") or "").strip().rstrip("/")


def _remote_vllm_model_key():
    """config.json 的 (ocr_remote_model, ocr_remote_key)，vLLM 形态用。"""
    try:
        from api.config import load_config
        cfg = load_config() or {}
    except Exception:
        cfg = {}
    return (str(cfg.get("ocr_remote_model") or "Unlimited-OCR").strip(),
            str(cfg.get("ocr_remote_key") or "").strip())


def _cache_dir() -> str:
    from core.paths import get_data_path
    d = get_data_path("ocr_cache")
    os.makedirs(d, exist_ok=True)
    return d


def _file_md5(path: str) -> str:
    md5 = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            md5.update(chunk)
    return md5.hexdigest()


def _cache_path(path: str, max_pages: int) -> str:
    return os.path.join(_cache_dir(), f"{_file_md5(path)}_{max_pages}.json")


def _ocr_remote(img_bytes: bytes, page: int, url: str) -> str:
    """调远程 GPU OCR 薄服务（POST {url}/ocr），失败抛异常（调用方降级本地）。"""
    resp = requests.post(
        f"{url}/ocr",
        json={"image_b64": base64.b64encode(img_bytes).decode("ascii"),
              "page": page},
        timeout=_REMOTE_TIMEOUT,
    )
    resp.raise_for_status()
    return str(resp.json().get("text") or "")


def _ocr_remote_vllm(img_bytes: bytes, url: str) -> str:
    """调 OpenAI 兼容视觉端点（vLLM 直接部署或 zxs_gw 网关），
    失败抛异常（调用方降级本地）。图片以 data:image/png;base64 内联。

    默认引擎是网关 Qwen3.8-27B：同图实测 3.6s/页逐字准确，比专用
    Unlimited-OCR（43s/页带标注退化）快一个数量级；专用模型路径保留
    frequency_penalty 抑制退化。提示词按模型区分：专用模型用原生
    document parsing.（无 <image> 字面量，防双占位错位），通用视觉模型
    用中文逐字转写指令。网关地址时自动带当前登录 token 鉴权。"""
    model, key = _remote_vllm_model_key()
    headers = {}
    if not key and "172.16.100.84:8080" in url:
        try:
            from core.server_auth import get_token
            key = get_token() or ""
        except Exception:
            key = ""
    if key:
        headers["Authorization"] = f"Bearer {key}"
    is_unlimited = model.strip().lower() == "unlimited-ocr"
    resp = requests.post(
        f"{url}/chat/completions",
        json={
            "model": model,
            # 采样参数显式给零温度+封顶（默认 temperature=1.0 在 OCR 上会产生
            # 幻觉/退化）；提示词用模型原生 document parsing 指令，且文本里
            # 不带 <image> 字面量——chat_template 已为图像项插入占位符，再写
            # 就是双占位符，A/B 实证输出整页重复退化
            "temperature": 0,
            "max_tokens": 2048,
            **({"frequency_penalty": 1.0} if is_unlimited else {}),
            "messages": [{"role": "user", "content": [
                {"type": "text", "text":
                    "document parsing." if is_unlimited else
                    "把这张扫描件中的全部文字按阅读顺序逐字转写出来，不要添加任何解说。"},
                {"type": "image_url", "image_url": {"url":
                    "data:image/png;base64,"
                    + base64.b64encode(img_bytes).decode("ascii")}},
            ]}],
        },
        headers=headers,
        timeout=_REMOTE_TIMEOUT_VLLM,
    )
    resp.raise_for_status()
    return str(resp.json()["choices"][0]["message"]["content"] or "")


def _boxes_to_text(result) -> str:
    """rapidocr 原始 boxes 按阅读顺序拼文本：先按行聚类（y 接近为同行），
    行内按 x 排序拼接，行间按 y 排序换行。"""
    items = []
    for box, text, score in (result or []):
        if not text or not text.strip():
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        items.append({
            "cx": sum(xs) / len(xs),
            "cy": sum(ys) / len(ys),
            "h": max(ys) - min(ys) or 1,
            "text": text.strip(),
        })
    if not items:
        return ""
    median_h = statistics.median(it["h"] for it in items)
    line_tol = max(median_h * 0.6, 1.0)
    items.sort(key=lambda it: it["cy"])
    lines = []  # [(line_cy, [item, ...])]
    for it in items:
        if lines and abs(it["cy"] - lines[-1][0]) <= line_tol:
            lines[-1][1].append(it)
            # 行基准 y 取均值，防止长行基准漂移
            members = lines[-1][1]
            lines[-1] = (sum(m["cy"] for m in members) / len(members), members)
        else:
            lines.append((it["cy"], [it]))
    out = []
    for _, members in lines:
        members.sort(key=lambda it: it["cx"])
        out.append(" ".join(m["text"] for m in members))
    return "\n".join(out)


def _ocr_local(img_bytes: bytes) -> str:
    """本地 rapidocr 引擎 OCR（串行化；引擎不可用抛 RuntimeError）。"""
    from tools.screen_ocr import _get_engine
    engine = _get_engine()
    if not engine:
        raise RuntimeError("rapidocr 不可用（pip install rapidocr-onnxruntime）")
    from PIL import Image
    import numpy as np
    im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    arr = np.asarray(im)
    with _local_sem:
        result, _ = engine(arr)
    return _boxes_to_text(result)


def _ocr_page(img_bytes: bytes, page: int, remote_url: str):
    """单页 OCR：远程优先、失败降级本地。返回 (text, engine)。"""
    if remote_url:
        try:
            # /v1 结尾 = vLLM 的 OpenAI 兼容端点（生产实证：
            # Unlimited-OCR 已用 vLLM 部署在 192.168.148.200:8004/v1）
            if remote_url.endswith("/v1"):
                return _ocr_remote_vllm(img_bytes, remote_url), "remote-vllm"
            return _ocr_remote(img_bytes, page, remote_url), "remote"
        except Exception as e:
            print(f"[PdfOCR] 远程 OCR 失败（第 {page} 页），降级本地: {e}")
    return _ocr_local(img_bytes), "local"


def _render_pdf_pages(path: str, max_pages: int):
    """pymupdf 渲染 PDF 每页为 PNG 字节。返回 (pages_png, total_pages)。"""
    fitz = _get_fitz()
    if not fitz:
        raise RuntimeError("pymupdf 不可用（pip install pymupdf）")
    doc = fitz.open(path)
    try:
        total = doc.page_count
        n = min(total, max_pages)
        mat = fitz.Matrix(_PDF_DPI / 72, _PDF_DPI / 72)
        pages = []
        for i in range(n):
            pix = doc.load_page(i).get_pixmap(matrix=mat)
            pages.append(pix.tobytes("png"))
        return pages, total
    finally:
        doc.close()


def ocr_pdf(path: str, max_pages: int = _DEFAULT_MAX_PAGES) -> dict:
    """对 PDF / 图片文件做全文 OCR。

    返回 {text, pages, engine, truncated, cached}；失败返回带 error 字段的
    dict（不抛异常，供工具层直接透传）。engine ∈ remote-vllm / remote /
    local / 两两组合（部分页降级）。max_pages 硬顶 50，超出截断。
    """
    result = {"text": "", "pages": 0, "engine": "", "truncated": False,
              "cached": False}
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        result["error"] = f"文件不存在: {path}"
        return result

    try:
        max_pages = int(max_pages)
    except (TypeError, ValueError):
        max_pages = _DEFAULT_MAX_PAGES
    if max_pages <= 0:
        max_pages = _DEFAULT_MAX_PAGES
    requested_over_cap = max_pages > _MAX_PAGES_HARD
    max_pages = min(max_pages, _MAX_PAGES_HARD)

    # 缓存：文件内容 MD5 + 生效后的 max_pages
    try:
        cache_file = _cache_path(path, max_pages)
        if os.path.exists(cache_file):
            with open(cache_file, "r", encoding="utf-8") as f:
                cached = json.load(f)
            cached["cached"] = True
            return cached
    except Exception as e:
        print(f"[PdfOCR] 读缓存失败（忽略，重新 OCR）: {e}")

    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf":
            pages_png, total = _render_pdf_pages(path, max_pages)
            truncated = total > max_pages
        elif ext in _IMG_EXTS:
            with open(path, "rb") as f:
                pages_png = [f.read()]
            total = 1
            truncated = False
        else:
            result["error"] = (f"不支持的文件类型 '{ext}'（支持 PDF 及 "
                               f"{', '.join(sorted(_IMG_EXTS))}）")
            return result
    except Exception as e:
        result["error"] = f"页面渲染失败: {e}"
        return result

    remote = _remote_url()
    texts = []
    engines = set()
    for i, png in enumerate(pages_png, 1):
        try:
            text, eng = _ocr_page(png, i, remote)
        except Exception as e:
            result["error"] = f"第 {i} 页 OCR 失败: {e}"
            return result
        texts.append(text)
        engines.add(eng)

    result.update({
        "text": "\n\n".join(t for t in texts if t),
        "pages": len(pages_png),
        "engine": "+".join(sorted(engines)),
        "truncated": bool(truncated or requested_over_cap),
    })

    try:
        payload = dict(result)
        payload["cached"] = False
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception as e:
        print(f"[PdfOCR] 写缓存失败（忽略）: {e}")
    return result
