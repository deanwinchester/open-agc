# 小红书配图生成 + 一键发布技能

## 触发条件
当用户需要：
- 生成小红书风格的高颜值配图
- 自动发布内容到小红书创作者平台
- 批量制作社交媒体图文内容

## 分步实施指令

### 第一步：生成高颜值配图

> **核心教训**：emoji 在 Windows PIL 下会显示为方块，必须用 PIL 手绘彩色图标圆替代！

```python
from PIL import Image, ImageDraw, ImageFont
import os

# ===== 配置 =====
W, H = 1080, 1440  # 小红书推荐 3:4 比例
output_path = "路径/xxx.png"

# Windows 中文字体路径
font_regular = r"C:\Windows\Fonts\msyh.ttc"   # 微软雅黑
font_bold = r"C:\Windows\Fonts\msyhbd.ttc"     # 微软雅黑粗体

# ===== 创建画布 =====
img = Image.new('RGB', (W, H), '#1a1a2e')
draw = ImageDraw.Draw(img)

# ===== 渐变背景 =====
for y in range(H):
    ratio = y / H
    r = int(26 + (10 - 26) * ratio)
    g = int(26 + (8 - 26) * ratio)
    b = int(46 + (36 - 46) * ratio)
    draw.line([(0, y), (W, y)], fill=(r, g, b))

# ===== 装饰光晕（增加氛围感）=====
for i in range(600, 0, -1):
    a = int(35 * (1 - i / 600))
    draw.ellipse([W+100-i, -200-i, W+100+i, -200+i], fill=(170+a, 90+a, 200+a))

# ===== 画彩色图标圆（替代 emoji）=====
def draw_icon_circle(draw, cx, cy, r, color_hex, icon_text, font):
    """PIL 手绘彩色圆形图标，比 emoji 更可靠更精致"""
    # 发光外圈
    for i in range(r+8, r-2, -1):
        alpha = (i - r + 8) / 10
        rc = int(int(color_hex[1:3], 16) * 0.4 * alpha)
        gc = int(int(color_hex[3:5], 16) * 0.4 * alpha)
        bc = int(int(color_hex[5:7], 16) * 0.4 * alpha)
        draw.ellipse([cx-i, cy-i, cx+i, cy+i], fill=(rc, gc, bc))
    # 主圆
    r_hex, g_hex, b_hex = int(color_hex[1:3],16), int(color_hex[3:5],16), int(color_hex[5:7],16)
    draw.ellipse([cx-r, cy-r, cx+r, cy+r], fill=(r_hex, g_hex, b_hex))
    # 中心文字
    tbox = draw.textbbox((0, 0), icon_text, font=font)
    tw = tbox[2] - tbox[0]
    th = tbox[3] - tbox[1]
    draw.text((cx - tw//2, cy - th//2 - 2), icon_text, font=font, fill='#ffffff')

# ===== 卡片式排版 =====
tips = [
    {"icon": "★", "title": "标题1", "body": "正文内容1", "color": "#f472b6"},
    # ... 更多卡片
]

card_y = 235
card_h = 175
gap = 14
mx = 55  # 左右边距
card_w = W - 2*mx

for i, tip in enumerate(tips):
    yt = card_y + i*(card_h + gap)
    yb = yt + card_h
    # 半透明卡片背景
    for ry in range(yt, yb):
        draw.line([(mx, ry), (mx+card_w, ry)], fill=(35, 30, 55))
    # 图标
    draw_icon_circle(draw, mx+55, (yt+yb)//2, 28, tip["color"], tip["icon"], icon_font)
    # 标题和正文
    draw.text((mx+105, yt+22), tip["title"], font=font_tip_title, fill='#f0e6ff')
    draw.text((mx+105, yt+68), tip["body"], font=font_tip_body, fill='#a098c0', spacing=6)

# ===== 底部金句 + 标签 =====
# 渐变分隔线 → 金句 → 署名 → 话题标签

img.save(output_path, quality=95)
```

### 第二步：浏览器自动发布

```python
# 1. 导航到创作者平台
browser_automation(action="goto", url="https://creator.xiaohongshu.com/publish/publish")

# 2. 上传图片（上传图文模式下）
browser_automation(action="upload", selector="input.upload-input", 
                   path="D:\\绝对\\路径\\图片.png")

# 3. 填写标题（⚠️ 不能超过20字！）
browser_automation(action="fill", 
                   selector='input.d-text[placeholder="填写标题会有更多赞哦"]',
                   text="女生运动科学指南💪")

# 4. 填写正文
browser_automation(action="fill", 
                   selector="div[contenteditable=\"true\"]",
                   text="正文内容...")

# 5. 点击发布
browser_automation(action="click", selector="button.bg-red")
```

### 第三步：验证发布结果

发布成功后，URL 会变为 `?published=true`。

## 关键注意事项

### 图片规范
- **尺寸**：1080 × 1440（3:4 比例，小红书推荐）
- **格式**：PNG，quality=95
- **风格**：深色调 + 莫兰迪色系 + 光晕氛围，避免花花绿绿
- **字体**：Windows 用 `C:\Windows\Fonts\msyh.ttc`（微软雅黑）

### emoji 陷阱 ⚠️
- **Windows PIL 无法正确渲染 emoji**，会显示为方块
- **解决方案**：用 PIL 手绘彩色圆形图标 + 文字替代 emoji
- 图标颜色鲜明、带发光外圈，比 emoji 更精致

### 标题限制 ⚠️
- **小红书标题最多 20 字**（含标点和 emoji）
- 填写前务必数字数

### 浏览器自动化
- 首次使用需手动登录小红书，之后 session 保持
- 文件上传使用 `upload` 动作，对准 `input[type="file"]` 元素
- CSS 选择器用 `.bg-red` 定位发布按钮

## 配色参考（莫兰迪高级感）

| 用途 | 色号 | 说明 |
|------|------|------|
| 粉色卡片 | #f472b6 | 温暖活泼 |
| 紫色卡片 | #a78bfa | 优雅神秘 |
| 蓝色卡片 | #60a5fa | 冷静专业 |
| 绿色卡片 | #34d399 | 健康清新 |
| 橙色卡片 | #fb923c | 活力温暖 |
| 背景主色 | #1a1a2e | 深紫暗色调 |
| 标题文字 | #f0e6ff | 浅紫白 |
| 正文文字 | #a098c0 | 灰紫 |
