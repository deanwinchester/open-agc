# -*- coding: utf-8 -*-
"""Unlimited-OCR GPU 推理服务：open-agc 远程 OCR 后端的 FastAPI 薄封装。

模型：Unlimited-OCR（DeepSeek-OCR 架构 VLM，HF transformers 自定义代码，
trust_remote_code=True，bf16 + cuda）。懒加载单例 + 全局锁串行化推理
（单卡一次只能跑一条）。

契约（open-agc 端 tools/pdf_ocr.py 的消费口径）：
    POST /ocr  {"image_b64": "<png base64>", "page": 1}
    → {"text": "<识别全文>"}

部署手册见同目录 README.md。
"""
import base64
import os
import tempfile
import threading

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# 模型目录：默认取仓库内 models/Unlimited-OCR（本脚本在 scripts/ocr_server/ 下，
# 上两级即仓库根）；部署到 GPU 机器时用 OCR_MODEL_DIR 覆盖
_DEFAULT_MODEL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "models", "Unlimited-OCR")
MODEL_DIR = os.environ.get("OCR_MODEL_DIR", _DEFAULT_MODEL_DIR)

app = FastAPI(title="Unlimited-OCR Service")

_model = None
_tokenizer = None
_lock = threading.Lock()


def _get_model():
    """懒加载模型单例（首次调用耗时数十秒，之后常驻显存）。"""
    global _model, _tokenizer
    if _model is None:
        import torch
        from transformers import AutoModel, AutoTokenizer
        _tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR, trust_remote_code=True)
        _model = AutoModel.from_pretrained(
            MODEL_DIR,
            trust_remote_code=True,
            use_safetensors=True,
            torch_dtype=torch.bfloat16,
        )
        _model = _model.eval().cuda()
    return _model, _tokenizer


class OcrRequest(BaseModel):
    image_b64: str
    page: int = 1


@app.get("/health")
def health():
    return {"status": "ok", "model_loaded": _model is not None}


@app.post("/ocr")
def ocr(req: OcrRequest):
    try:
        img_bytes = base64.b64decode(req.image_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="image_b64 不是合法 base64")

    # infer 只接受文件路径，落临时 PNG（用完即删）
    fd, tmp = tempfile.mkstemp(suffix=".png", prefix="ocr_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(img_bytes)
        model, tokenizer = _get_model()
        with _lock:
            text = model.infer(
                tokenizer,
                prompt="<image>document parsing.",
                image_file=tmp,
                output_path="",
                # gundam 配置：单页文档通用（base_size=1024, image_size=640, 切块）
                base_size=1024, image_size=640, crop_mode=True,
                max_length=32768,
                no_repeat_ngram_size=35, ngram_window=128,
                save_results=False, eval_mode=True,
            )
        return {"text": (text or "").strip()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OCR 推理失败: {e}")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("OCR_PORT", "8377")))
