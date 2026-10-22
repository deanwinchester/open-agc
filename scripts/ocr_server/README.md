# OCR GPU 服务部署手册（192.168.148.200）

open-agc 的文档 OCR 支持「远程 GPU 服务优先，本地 rapidocr 兜底」双后端。
本目录是远程后端的部署包：`ocr_service.py` 把 Unlimited-OCR（DeepSeek-OCR
架构 VLM）封装成 FastAPI 服务，open-agc 端配一个 URL 即接入。

## 接口契约

```
POST http://<host>:8377/ocr
请求: {"image_b64": "<png base64>", "page": 1}
响应: {"text": "<识别全文>"}
GET  /health → {"status": "ok", "model_loaded": true/false}
```

## GPU 机器（192.168.148.200）部署步骤

1. 环境：Python 3.10+，建 venv

   ```bash
   python3.10 -m venv ~/ocr-venv && source ~/ocr-venv/bin/activate
   ```

2. 装依赖（torch 按机器 CUDA 版本选源，下面示例为 CUDA 12.1）

   ```bash
   pip install torch --index-url https://download.pytorch.org/whl/cu121
   pip install transformers accelerate fastapi uvicorn pillow
   ```

3. 模型文件（约 6.7GB safetensors）：从 open-agc 仓库 `models/Unlimited-OCR/`
   整个目录拷到 GPU 机器，或挂内网共享盘。目录需包含
   `model-00001-of-000001.safetensors`、`modeling_unlimitedocr.py`、
   tokenizer 等全部文件。

4. 启动

   ```bash
   # 前台调试
   OCR_MODEL_DIR=/path/to/Unlimited-OCR python ocr_service.py

   # 后台常驻（nohup）
   OCR_MODEL_DIR=/path/to/Unlimited-OCR nohup python ocr_service.py > ocr.log 2>&1 &

   # 或 systemd unit（/etc/systemd/system/unlimited-ocr.service）
   # [Service]
   # Environment=OCR_MODEL_DIR=/path/to/Unlimited-OCR OCR_PORT=8377
   # ExecStart=/root/ocr-venv/bin/python /opt/ocr/ocr_service.py
   # Restart=always
   ```

   首次请求会加载模型（数十秒），之后常驻显存。`curl localhost:8377/health`
   可确认存活。

## open-agc 端接入

`data/config.json` 加一行：

```json
"ocr_remote_url": "http://192.168.148.200:8377"
```

之后 HTTP API `POST /api/ocr` 与 agent 工具 `ocr_document` 都会自动走远程
GPU 服务；服务不可达时自动降级本机 rapidocr（慢，但可用）。

## 批量回写 DAMS

`batch_ocr_dams.py` 是批量 OCR 报纸 PDF 并回写 DAMS historypdf 库的骨架
脚本（默认 dry-run），用法见脚本内 docstring。
