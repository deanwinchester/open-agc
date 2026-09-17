# -*- coding: utf-8 -*-
"""GUI 元素定位服务（grounder）：把「搜索框」翻译成像素坐标。

弱视觉模型（qwen3.8 实测偏差数百像素且迭代不收敛）自己做 grounding 不可靠，
业界标准解法是 planner-grounder 分层：规划模型决定做什么，专用定位模型
（UI-TARS / OS-Atlas 等几 B 的小模型，可部署在昆仑芯 P800 上）把自然语言
目标翻译成坐标。本模块是定位服务的客户端：OpenAI 兼容端点，截图+目标
描述进去，(x, y) 像素坐标出来。

配置（data/config.json）：
  "computer_grounder": {
      "base_url": "http://<p800-host>:<port>/v1",
      "api_key": "",
      "model": "UI-TARS-1.5-7B"
  }
未配置时 locate 动作返回配置指引，不影响其他功能。
"""
import base64
import io
import json
import os
import re

_cfg_cache = None
_cfg_mtime = 0


def _load_config() -> dict:
    """读 computer_grounder 配置（带 mtime 缓存，设置页改完即时生效）。"""
    global _cfg_cache, _cfg_mtime
    try:
        from core.paths import get_data_path
        path = get_data_path("config.json")
        mtime = os.path.getmtime(path) if os.path.exists(path) else 0
        if _cfg_cache is not None and mtime == _cfg_mtime:
            return _cfg_cache
        cfg = {}
        if mtime:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f).get("computer_grounder") or {}
        _cfg_cache = cfg
        _cfg_mtime = mtime
        return cfg
    except Exception:
        return _cfg_cache or {}


def grounder_ready() -> bool:
    cfg = _load_config()
    return bool(cfg.get("base_url") and cfg.get("model"))


# UI-TARS/OS-Atlas 的惯例输出形如 click(start_box='(1345, 692)') 或裸 (x, y)。
# 坐标制式按配置 coord_mode 解释（默认 abs 绝对像素——UI-TARS 惯例）；
# abs 模式下两个值都 ≤1.0 且带小数时按 [0,1] 归一化兜底（没有 UI 元素的
# 像素坐标会是 0.x）。注意不要盲目猜 [0,1000]——(100,200) 这种绝对坐标
# 会被误放大近一倍（测试实证），需要时用 coord_mode="norm1000" 显式指定。
_POINT_RE = re.compile(r"\(\s*([0-9]*\.?[0-9]+)\s*[,，]\s*([0-9]*\.?[0-9]+)\s*\)")


def parse_point(text: str, img_w: int, img_h: int, mode: str = "abs"):
    """从模型回复解析坐标点，换算到图像像素坐标。失败返回 None。"""
    if not text:
        return None
    m = _POINT_RE.search(text)
    if not m:
        return None
    x, y = float(m.group(1)), float(m.group(2))
    if mode == "norm1":
        x, y = x * img_w, y * img_h
    elif mode == "norm1000":
        x, y = x / 1000.0 * img_w, y / 1000.0 * img_h
    elif x <= 1.0 and y <= 1.0 and ("." in m.group(1) or "." in m.group(2)):
        x, y = x * img_w, y * img_h
    px, py = int(round(x)), int(round(y))
    if 0 <= px <= img_w and 0 <= py <= img_h:
        return px, py
    return None


def locate_element(img, target: str):
    """对 PIL Image 调定位模型找 target 的中心点。
    返回 (x, y)（图像像素坐标）；未配置/调用失败/解析失败返回错误文本。"""
    cfg = _load_config()
    if not grounder_ready():
        return ("Error: 定位服务未配置。请在 设置→模型 或 config.json 里配置 "
                "computer_grounder（base_url/api_key/model，如部署在 P800 上的 "
                "UI-TARS），或用截图+网格/OCR 锚点手动定位。")
    w, h = img.size
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=92)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    prompt = (
        "You are a GUI grounding assistant. Find the element described below in the "
        "screenshot and output ONLY the (x, y) pixel coordinates of its center.\n"
        f"Element: {target}\n"
        "Output format: (x, y)"
    )
    try:
        import requests
        resp = requests.post(
            cfg["base_url"].rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {cfg.get('api_key') or 'none'}"},
            json={
                "model": cfg["model"],
                "messages": [{"role": "user", "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": prompt},
                ]}],
                "max_tokens": 300,
                "temperature": 0.0,
            },
            timeout=float(cfg.get("timeout", 120)),
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"].get("content") or ""
    except Exception as e:
        return f"Error: 定位服务调用失败（{cfg['base_url']}）: {e}"
    pt = parse_point(content, w, h, mode=str(cfg.get("coord_mode", "abs")))
    if not pt:
        return (f"Error: 定位服务未能给出有效坐标（原始回复: "
                f"{content.strip()[:120]!r}）。可换截图网格/OCR 锚点手动定位。")
    return pt
