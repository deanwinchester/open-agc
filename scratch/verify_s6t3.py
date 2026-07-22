# -*- coding: utf-8 -*-
"""S6 Task3 实测：kimi_code/k3（视觉模型）通过 image_view 查看本地图片。

链路：PIL 生成测试图（workspace/ 沙箱内）→ 真实 Agent（k3）→
search_available_tools 惰性启用 image_view → image_view 执行 →
[IMAGE_DATA:] 标记 → agent 注入 user 消息 image_url → k3 描述图片内容。

运行：python scratch/verify_s6t3.py
"""
import os
import sys

# Windows 控制台默认 GBK，强制 UTF-8 输出
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from PIL import Image, ImageDraw  # noqa: E402

IMG_PATH = os.path.join(PROJECT_ROOT, "workspace", "s6t3_test_image.png")
SECRET = "K3-VISION-OK-42"


def make_image():
    img = Image.new("RGB", (800, 500), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle([40, 40, 300, 200], fill=(220, 30, 30))    # 红色方块
    d.rectangle([340, 40, 600, 200], fill=(30, 30, 220))   # 蓝色方块
    d.ellipse([100, 260, 360, 460], fill=(30, 180, 30))    # 绿色圆形
    d.text((400, 330), SECRET, fill=(0, 0, 0))
    img.save(IMG_PATH)
    print(f"[setup] test image -> {IMG_PATH}")


def main():
    from agent.agent import OpenAGCAgent

    make_image()
    agent = OpenAGCAgent(model="kimi_code/k3")

    # 1) 注册检查：惰性集（在 full_available_tools，不在 resident core）
    assert "image_view" in agent.full_available_tools, "image_view 未注册"
    assert "image_view" not in agent.active_tool_names, "image_view 不应常驻 core"
    print("[check] image_view 已注册（惰性集，未常驻 core）[OK]")

    # 2) 惰性发现：通过 search_available_tools 检索启用（真实惰性路径）
    discovery = agent.full_available_tools["search_available_tools"]
    out = discovery.execute(query="查看图片 image")
    print(f"[check] discovery output:\n{out}")
    assert "image_view" in agent.active_tool_names, "discovery 未启用 image_view"
    print("[check] search_available_tools 已惰性启用 image_view [OK]")

    # 3) 实测：让 k3 看图说话
    prompt = (
        f"请调用 image_view 工具查看本地图片 {IMG_PATH} ，"
        "然后告诉我：图片里有哪些图形和颜色，以及图片中写的文字是什么。"
    )
    print(f"[run] prompt: {prompt}")
    reply = agent.run_turn(prompt, verbose=True)
    print(f"\n[reply]\n{reply}\n")

    # 4) 注入链路检查：messages 中应出现携带 image_url 的 user 消息
    injected = [
        m for m in agent.messages
        if m["role"] == "user" and isinstance(m["content"], list)
        and any(p.get("type") == "image_url" for p in m["content"])
    ]
    assert injected, "未找到注入的图片消息（image_url user message）"
    url = next(p for p in injected[0]["content"]
               if p["type"] == "image_url")["image_url"]["url"]
    assert url.startswith("data:image/"), f"注入的 url 异常: {url[:40]}"
    print(f"[check] 图片已注入对话（{len(injected)} 条，data-url 前缀 {url[:30]}...）[OK]")

    # 5) 内容识别检查（宽松：任一关键内容命中即视为看懂）
    hits = [kw for kw in ("红", "蓝", "绿", "圆", "方块", "矩形", SECRET) if kw in reply]
    assert hits, f"回复未命中任何预期内容关键词: {reply[:300]}"
    print(f"[check] k3 识别出图片内容，命中关键词: {hits} [OK]")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
