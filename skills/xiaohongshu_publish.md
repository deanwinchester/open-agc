# 小红书配图生成 + 一键发布技能

## 触发条件
当用户需要：
- 生成小红书风格的高颜值配图
- 自动发布内容到小红书创作者平台
- 批量制作社交媒体图文内容

## 步骤

### 第一步：生成高颜值配图

> **核心注意事项**：emoji 在 Windows PIL 下会显示为方块，必须用 PIL 手绘彩色图标圆替代！

```python
from PIL import Image, ImageDraw, ImageFont
import os

# ===== 配置 =====
W, H = 1080, 1440  # 小红书推荐 3:4 比例
output_path = "输出路径/xxx.png"

# Windows 中文字体路径
font_regular = r"C:\Windows\Fonts\msyh.ttc"   # 微软雅黑
font_bold = r"C:\Windows\Fonts\msyhbd.ttc"     # 微软雅黑粗体

# ===== 创建画布 =====
img = Image.new('RGB', (W, H), '#1a1a2e')
draw = ImageDraw.Draw(img)

# ===== 绘制字体 =====
try:
    title_font = ImageFont.truetype(font_bold, 80)
    subtitle_font = ImageFont.truetype(font_regular, 45)
    body_font = ImageFont.truetype(font_regular, 35)
except:
    title_font = ImageFont.load_default()
    subtitle_font = ImageFont.load_default()
    body_font = ImageFont.load_default()

# ===== 绘制熊猫形象（用彩色圆形组合） =====
# 大熊猫脸（白色大圆）
draw.ellipse([370, 150, 710, 490], fill='white', outline='#333', width=6)
# 耳朵（黑色半圆）
draw.ellipse([320, 100, 430, 230], fill='#333', outline='#333', width=4)
draw.ellipse([650, 100, 760, 230], fill='#333', outline='#333', width=4)
# 眼睛（黑色小圆 + 白色高光）
draw.ellipse([430, 250, 480, 310], fill='#333')
draw.ellipse([600, 250, 650, 310], fill='#333')
draw.ellipse([445, 265, 470, 290], fill='white')  # 高光
draw.ellipse([615, 265, 640, 290], fill='white')  # 高光
# 鼻子
draw.ellipse([525, 350, 555, 380], fill='#333')
# 嘴巴弧形
draw.arc([510, 370, 570, 410], 0, 180, fill='#333', width=3)

# ===== 手绘替代 emoji 的彩色圆点 =====
icons = [
    (130, 600, '#FF6B9D', 30),  # 粉色圆点
    (200, 600, '#FFD93D', 30),  # 黄色圆点
    (270, 600, '#6BCB77', 30),  # 绿色圆点
    (340, 600, '#4D96FF', 30),  # 蓝色圆点
]
for x, y, color, r in icons:
    draw.ellipse([x-r, y-r, x+r, y+r], fill=color)

# ===== 标题 =====
draw.text((100, 700), "熊猫 AI 助手", fill='white', font=title_font)
draw.text((100, 800), "你的智能编程伙伴，让开发更简单", fill='#FFD93D', font=subtitle_font)
draw.text((100, 900), "🚀 支持多种编程语言", fill='#ccc', font=body_font)

# ===== 保存 =====
os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
img.save(output_path)
print(f"图片已保存: {output_path}")
```

### 第二步：一键发布到小红书

```python
# 使用 browser_automation 发布
# 1. 打开 https://creator.xiaohongshu.com/publish/publish
# 2. 点击上传按钮，选择生成的图片文件
# 3. 编写标题和正文
# 4. 添加话题标签
# 5. 点击发布按钮
```

## 注意事项
- Windows 下必须使用手绘彩色圆点替代 emoji
- 小红书图片推荐 3:4 比例（1080×1440）
- 发布前验证文案无违禁词

## 涉及工具
- `execute_python` — PIL 生成配图
- `browser_automation` — 发布到小红书平台
