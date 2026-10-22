# -*- coding: utf-8 -*-
"""ocr_document 工具：扫描版 PDF / 图片 OCR 出全文。

扫描版文档没有文本层，read_file 读不出内容——典型场景是 1954-2000 年的
历史报纸 PDF。实现委托 tools/pdf_ocr.ocr_pdf（远程 GPU 服务优先，本地
rapidocr 兜底，结果有 MD5 缓存）。长结果不在这里截断：agent 主循环的
_truncate_tool_result_for_context 会把超长结果溢出到沙箱 tool_results/
并附指引（与 es_search 大结果同一处理模式），工具只需返回完整文本。
"""
import os
from typing import Any, Dict

from tools.base import BaseTool
from tools.pdf_ocr import ocr_pdf


class OcrDocumentTool(BaseTool):
    name: str = "ocr_document"
    description: str = (
        "OCR 识别扫描版 PDF 或图片中的文字（无文本层、read_file 读不出内容的文档用本工具）。"
        "典型场景：1954-2000 年的扫描版报纸 PDF、拍照/扫描的图片文档。"
        "页数多耗时长（约 3-5 秒/页），先用小 max_pages（如 2-3 页）确认识别质量再放大。"
        "受沙箱限制。"
    )

    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "沙箱内 PDF / 图片文件路径（如 uploads/xxx.pdf）。"
                        },
                        "max_pages": {
                            "type": "integer",
                            "description": "最多识别页数（默认 10，上限 50；超出部分截断）。"
                        },
                    },
                    "required": ["path"],
                },
            },
        }

    def execute(self, path: str = "", max_pages: int = 10, **kwargs) -> str:
        if not path or not str(path).strip():
            return "Error: 缺少必填参数 path。"
        path = str(path).strip()

        # 沙箱限制（与 read_file / image_view 同一模式）
        import json as _json
        from core.paths import get_data_path
        config = {}
        config_path = get_data_path("config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = _json.load(f)
            except Exception:
                pass
        if config.get("sandbox_mode", True):
            # 同 write_file/read_file：相对路径按沙箱目录解析（缺了这步会按
            # 进程 CWD 解析，uploads/xxx.pdf 找不到文件还误触发沙箱外授权弹窗）
            from tools.filesystem import _resolve_relative
            path = _resolve_relative(path, config)
            whitelist = kwargs.get("_session_whitelist", None)
            self.check_sandbox(path, config=config, session_whitelist=whitelist)

        result = ocr_pdf(path, max_pages=max_pages)
        if result.get("error"):
            return f"Error: OCR 失败：{result['error']}"
        text = result["text"] or ""
        if not text:
            return (f"OCR 完成但未识别到文字（engine={result['engine']}，"
                    f"pages={result['pages']}）。页面可能是空白或图像质量过低。")
        header = (f"[OCR 完成] engine={result['engine']}，识别页数={result['pages']}"
                  f"{'，已截断（全文更多页，可调大 max_pages）' if result['truncated'] else ''}"
                  f"{'，命中缓存' if result.get('cached') else ''}\n\n")
        return header + text
