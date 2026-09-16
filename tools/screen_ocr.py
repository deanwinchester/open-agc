# -*- coding: utf-8 -*-
"""截图文字锚点（OCR）：弱视觉模型不看像素看文字。

rapidocr 提取截图中的文字及中心坐标，作为 computer_control 截图结果的
锚点清单返回——模型按「搜索」「郭路明」这类文字锚点定位，坐标由 OCR
精确给出，与模型的视觉 grounding 能力脱钩（qwen3.8 实测视觉定位偏差
数百像素且迭代不收敛，OCR 锚点实测偏差 <10px）。

引擎单例懒加载；缺依赖（rapidocr-onnxruntime 未装）时静默降级为空清单。
"""
import os

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            # use_cls=False：跳过文字方向分类，CPU 上快约 30%
            _engine = RapidOCR(use_cls=False)
        except Exception as e:
            print(f"[ScreenOCR] rapidocr 不可用（pip install rapidocr-onnxruntime）: {e}")
            _engine = False
    return _engine or None


_OCR_MAX_EDGE = 1280  # 输入降采样上限（1920 全尺寸 CPU 要 12s，1280 约 3-4s）


def ocr_anchors(img, max_items: int = 40):
    """对 PIL Image 或图片路径做 OCR，返回 [(text, cx, cy, score), ...]，
    坐标为原图像素坐标。无引擎或无结果返回 []。"""
    engine = _get_engine()
    if not engine:
        return []
    try:
        from PIL import Image as _Image
        import numpy as np
        if isinstance(img, str):
            im = _Image.open(img)
        else:
            im = img
        ow, oh = im.size
        scale = 1.0
        if max(ow, oh) > _OCR_MAX_EDGE:
            scale = _OCR_MAX_EDGE / max(ow, oh)
            im = im.resize((int(ow * scale), int(oh * scale)))
        arr = np.asarray(im.convert("RGB"))
        result, _ = engine(arr)
        anchors = []
        for box, text, score in (result or []):
            if score < 0.5 or not text.strip():
                continue
            cx = sum(p[0] for p in box) / 4 / scale
            cy = sum(p[1] for p in box) / 4 / scale
            anchors.append((text.strip(), cx, cy, score))
        # 置信度优先，取前 max_items
        anchors.sort(key=lambda a: -a[3])
        return anchors[:max_items]
    except Exception as e:
        print(f"[ScreenOCR] OCR failed: {e}")
        return []


def format_anchors(anchors, view_scale: float = 1.0, max_items: int = 40,
                   offset=(0, 0)) -> str:
    """把锚点格式化成结果文本。view_scale = 全图坐标/图内坐标；
    offset = 图内原点在全图坐标系中的位置（region 放大图的裁剪原点）。
    纯数字/网格刻度噪声会被过滤。"""
    items = []
    for text, cx, cy, score in anchors:
        # 过滤纯数字短串（网格刻度数字是噪声）
        if text.isdigit() and len(text) <= 4:
            continue
        vx = round(cx * view_scale + offset[0])
        vy = round(cy * view_scale + offset[1])
        items.append(f'"{text}"({vx},{vy})')
        if len(items) >= max_items:
            break
    if not items:
        return ""
    return "文字锚点(全图坐标): " + "、".join(items)
