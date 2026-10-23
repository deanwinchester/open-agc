# -*- coding: utf-8 -*-
"""文档 OCR（tools/pdf_ocr.py + api/routes/routes_ocr.py）测试。

rapidocr 与 pymupdf 全部 mock：假引擎返回乱序 boxes 验证阅读顺序拼接；
假 fitz 验证 max_pages 截断；缓存命中、远程超时降级本地、沙箱路径逃逸
拒绝（API 层 TestClient）逐一覆盖。
"""
import io
import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import tools.pdf_ocr as pdf_ocr
import tools.screen_ocr as screen_ocr


# ── 测试替身 ────────────────────────────────────────────────────────────

def _png_bytes() -> bytes:
    """真实的小 PNG（本地 OCR 路径要经 PIL 解码，不能用假字节）。"""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _box(x, y, w=40, h=20):
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]


class _FakeEngine:
    """模拟 rapidocr：返回乱序 boxes（验证按阅读顺序重排）。可计数。"""

    def __init__(self, boxes=None):
        self.calls = 0
        self.boxes = boxes if boxes is not None else [
            (_box(10, 100), "第二行", 0.99),
            (_box(200, 52), "右", 0.99),
            (_box(10, 50), "第一行左", 0.99),
        ]

    def __call__(self, arr):
        self.calls += 1
        return list(self.boxes), None


class _FakePixmap:
    def tobytes(self, fmt):
        assert fmt == "png"
        return _png_bytes()


class _FakePage:
    def get_pixmap(self, matrix=None):
        return _FakePixmap()


class _FakeDoc:
    def __init__(self, page_count):
        self.page_count = page_count

    def load_page(self, i):
        return _FakePage()

    def close(self):
        pass


class _FakeFitz:
    """模拟 pymupdf 模块（open / Matrix / page_count）。"""

    def __init__(self, page_count):
        self._page_count = page_count

    def open(self, path):
        return _FakeDoc(self._page_count)

    @staticmethod
    def Matrix(a, b):
        return (a, b)


@pytest.fixture
def local_env(tmp_path, monkeypatch):
    """隔离数据目录 + 关闭远程后端 + 装上假引擎。"""
    monkeypatch.setenv("OPEN_AGC_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(pdf_ocr, "_remote_url", lambda: "")
    engine = _FakeEngine()
    monkeypatch.setattr(screen_ocr, "_get_engine", lambda: engine)
    return engine


def _write_png(tmp_path, name="page.png") -> str:
    p = tmp_path / name
    p.write_bytes(_png_bytes())
    return str(p)


# ── tools/pdf_ocr.py ────────────────────────────────────────────────────

def test_reading_order(local_env, tmp_path):
    """乱序 boxes 应按「先 y 后 x」拼行：同行按 x 拼，行间按 y 换行。"""
    img = _write_png(tmp_path)
    result = pdf_ocr.ocr_pdf(img)
    assert result["engine"] == "local"
    assert result["pages"] == 1
    assert result["text"] == "第一行左 右\n第二行"
    assert result["truncated"] is False


def test_max_pages_truncation(local_env, tmp_path, monkeypatch):
    """5 页文档 max_pages=2 → 只处理 2 页且 truncated=True。"""
    monkeypatch.setattr(pdf_ocr, "_get_fitz", lambda: _FakeFitz(5))
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-fake")
    result = pdf_ocr.ocr_pdf(str(pdf), max_pages=2)
    assert result["pages"] == 2
    assert result["truncated"] is True


def test_max_pages_hard_cap(local_env, tmp_path, monkeypatch):
    """max_pages 硬顶 50：请求 100 截到 50 且 truncated=True。"""
    monkeypatch.setattr(pdf_ocr, "_get_fitz", lambda: _FakeFitz(60))
    pdf = tmp_path / "big.pdf"
    pdf.write_bytes(b"%PDF-fake")
    result = pdf_ocr.ocr_pdf(str(pdf), max_pages=100)
    assert result["pages"] == 50
    assert result["truncated"] is True


def test_cache_hit(local_env, tmp_path):
    """同一文件第二次调用命中缓存，引擎不再被调用。"""
    img = _write_png(tmp_path)
    first = pdf_ocr.ocr_pdf(img)
    assert first["cached"] is False
    assert local_env.calls == 1
    second = pdf_ocr.ocr_pdf(img)
    assert second["cached"] is True
    assert second["text"] == first["text"]
    assert local_env.calls == 1  # 未再走引擎


def test_remote_success(local_env, tmp_path, monkeypatch):
    """配置远程后端且调用成功 → engine=remote。"""
    monkeypatch.setattr(pdf_ocr, "_remote_url", lambda: "http://gpu:8377")

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"text": "远程识别结果"}

    monkeypatch.setattr(pdf_ocr.requests, "post", lambda *a, **k: _Resp())
    result = pdf_ocr.ocr_pdf(_write_png(tmp_path))
    assert result["engine"] == "remote"
    assert result["text"] == "远程识别结果"
    assert local_env.calls == 0  # 不经过本地引擎


def test_remote_timeout_fallback_local(local_env, tmp_path, monkeypatch):
    """远程超时 → 降级本地 rapidocr，engine=local。"""
    monkeypatch.setattr(pdf_ocr, "_remote_url", lambda: "http://gpu:8377")

    def _boom(*a, **k):
        raise pdf_ocr.requests.exceptions.Timeout("timeout")

    monkeypatch.setattr(pdf_ocr.requests, "post", _boom)
    result = pdf_ocr.ocr_pdf(_write_png(tmp_path))
    assert result["engine"] == "local"
    assert result["text"] == "第一行左 右\n第二行"
    assert local_env.calls == 1


# ── /v1（vLLM OpenAI 兼容端点）形态 ─────────────────────────────────────

class _VllmResp:
    """chat/completions 响应替身。"""

    def __init__(self, text="vLLM 识别结果"):
        self._text = text

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._text}}]}


def test_remote_vllm_chat_completions(local_env, tmp_path, monkeypatch):
    """/v1 结尾的 ocr_remote_url 走 chat/completions，请求体结构正确。"""
    import api.config as api_config
    monkeypatch.setattr(api_config, "load_config", lambda: {})
    monkeypatch.setattr(pdf_ocr, "_remote_url", lambda: "http://gpu:8004/v1")
    calls = []

    def _post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json,
                      "headers": headers, "timeout": timeout})
        return _VllmResp()

    monkeypatch.setattr(pdf_ocr.requests, "post", _post)
    result = pdf_ocr.ocr_pdf(_write_png(tmp_path))
    assert result["engine"] == "remote-vllm"
    assert result["text"] == "vLLM 识别结果"
    assert local_env.calls == 0  # 不经过本地引擎
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "http://gpu:8004/v1/chat/completions"
    assert call["timeout"] == pdf_ocr._REMOTE_TIMEOUT_VLLM
    assert not (call["headers"] or {}).get("Authorization")  # 未配 key 不带头
    body = call["json"]
    assert body["model"] == "Unlimited-OCR"  # ocr_remote_model 默认值
    content = body["messages"][0]["content"]
    assert [c["type"] for c in content] == ["image_url", "text"]
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "识别" in content[1]["text"]


def test_remote_vllm_model_override_and_key(local_env, tmp_path, monkeypatch):
    """ocr_remote_model 覆盖默认值；ocr_remote_key 非空时带 Bearer 头。"""
    import api.config as api_config
    monkeypatch.setattr(api_config, "load_config", lambda: {
        "ocr_remote_model": "MyOCR",
        "ocr_remote_key": "secret-key",
    })
    monkeypatch.setattr(pdf_ocr, "_remote_url", lambda: "http://gpu:8004/v1")
    calls = []

    def _post(url, json=None, headers=None, timeout=None):
        calls.append({"json": json, "headers": headers})
        return _VllmResp()

    monkeypatch.setattr(pdf_ocr.requests, "post", _post)
    pdf_ocr.ocr_pdf(_write_png(tmp_path))
    assert calls[0]["json"]["model"] == "MyOCR"
    assert calls[0]["headers"]["Authorization"] == "Bearer secret-key"


def test_remote_vllm_failure_fallback_local(local_env, tmp_path, monkeypatch):
    """vLLM 端点报错 → 降级本地 rapidocr，engine=local。"""
    monkeypatch.setattr(pdf_ocr, "_remote_url", lambda: "http://gpu:8004/v1")

    def _boom(*a, **k):
        raise pdf_ocr.requests.exceptions.ConnectionError("conn refused")

    monkeypatch.setattr(pdf_ocr.requests, "post", _boom)
    result = pdf_ocr.ocr_pdf(_write_png(tmp_path))
    assert result["engine"] == "local"
    assert result["text"] == "第一行左 右\n第二行"
    assert local_env.calls == 1


# ── api/routes/routes_ocr.py ────────────────────────────────────────────

def _api_client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.routes import routes_ocr

    sandbox = tmp_path / "sandbox"
    (sandbox / "uploads").mkdir(parents=True)
    monkeypatch.setattr(routes_ocr, "sandbox_root", lambda: str(sandbox))
    app = FastAPI()
    app.include_router(routes_ocr.router)
    return TestClient(app), routes_ocr, sandbox


def test_api_rejects_traversal(tmp_path, monkeypatch):
    """路径逃逸（.. / 非白名单分区）一律 403。"""
    client, _, _ = _api_client(tmp_path, monkeypatch)
    for bad in ("../secret.pdf", "uploads/../../secret.pdf",
                "data/x.pdf", "/etc/passwd", ""):
        r = client.post("/api/ocr", json={"path": bad})
        assert r.status_code == 403, f"{bad!r} 应为 403，实际 {r.status_code}"


def test_api_404_and_bad_ext(tmp_path, monkeypatch):
    client, _, sandbox = _api_client(tmp_path, monkeypatch)
    r = client.post("/api/ocr", json={"path": "uploads/missing.pdf"})
    assert r.status_code == 404
    (sandbox / "uploads" / "a.txt").write_text("hi", encoding="utf-8")
    r = client.post("/api/ocr", json={"path": "uploads/a.txt"})
    assert r.status_code == 400


def test_api_success(tmp_path, monkeypatch):
    """正常路径透传 ocr_pdf 结果（含 cached 字段）。"""
    client, routes_ocr, sandbox = _api_client(tmp_path, monkeypatch)
    (sandbox / "uploads" / "a.png").write_bytes(_png_bytes())
    monkeypatch.setattr(routes_ocr, "ocr_pdf", lambda path, max_pages: {
        "text": "正文", "pages": 1, "engine": "local",
        "truncated": False, "cached": False,
    })
    r = client.post("/api/ocr", json={"path": "uploads/a.png", "max_pages": 3})
    assert r.status_code == 200
    body = r.json()
    assert body == {"text": "正文", "pages": 1, "engine": "local",
                    "truncated": False, "cached": False}
