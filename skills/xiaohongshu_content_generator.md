# 小红书内容生成与自动发布技能

## 触发条件
当用户需要：
- 生成小红书风格的内容（图片 + 文案）
- 自动发布内容到小红书创作者平台
- 创建带熊猫形象的品牌宣传图文

## 步骤

### 第一步：生成小红书风格配图
使用 Python PIL 创建小红书风格的图片：

```python
from PIL import Image, ImageDraw, ImageFont
import os

# 创建画布（小红书推荐尺寸 3:4）
width, height = 900, 1200
img = Image.new('RGB', (width, height), color='#FF6B9D')  # 粉色背景
draw = ImageDraw.Draw(img)

# 尝试加载字体（中文字体）
try:
    title_font = ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", 60)
    subtitle_font = ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", 40)
    content_font = ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", 32)
except:
    title_font = ImageFont.load_default()
    subtitle_font = ImageFont.load_default()
    content_font = ImageFont.load_default()

# 绘制标题
draw.text((50, 80), "熊猫 AI 助手", fill='white', font=title_font)
draw.text((50, 160), "你的智能伙伴", fill='#FFD700', font=subtitle_font)

# 在图片底部添加装饰性圆点
for i in range(5):
    x = 200 + i * 100
    y = 1000
    draw.ellipse([x, y, x+40, y+40], fill='#FFD700', outline='white', width=3)

# 保存图片
output_path = os.path.join(os.getcwd(), "xiaohongshu_card.png")
img.save(output_path)
print(f"图片已保存至: {output_path}")
```

### 第二步：编写小红书风格文案
- **标题**：吸引眼球，含 emoji，控制在 20 字以内
- **正文**：分段清晰，每段 2-3 行，关键信息加粗
- **标签**：末尾添加 3-5 个相关标签（#标签）
- **emoji 使用**：Windows PIL 下 emoji 显示为方块，需用 PIL 手绘彩色圆点替代

### 第三步：发布到小红书
```python
# 使用 browser_automation 打开小红书创作者平台
# URL: https://creator.xiaohongshu.com/publish/publish
# 1. 登录（如已登录则跳过）
# 2. 上传生成的图片
# 3. 填写标题和正文
# 4. 添加标签
# 5. 点击发布
```

## 注意事项
- Windows 系统下 PIL 不支持 emoji 渲染，需使用 `draw.ellipse()` 手绘彩色圆点替代
- macOS 可使用 PingFang 字体，Windows 可使用微软雅黑
- 小红书推荐图片尺寸为 3:4 比例

## 涉及工具
- `execute_python` — 使用 PIL 生成图片
- `browser_automation` — 发布到小红书平台
- `write_file` — 保存文案草稿


---
*⚠️ 该技能最近使用效果不佳，成功率低于30%，需要检查修正。Review date: 2026-05-19 11:03*