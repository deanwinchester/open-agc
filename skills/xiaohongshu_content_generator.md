# 小红书内容生成与自动发布技能

## 触发条件
当用户需要：
- 生成小红书风格的内容（图片+文案）
- 自动发布内容到小红书创作者平台
- 创建带熊猫形象的品牌宣传图文

## 分步实施指令

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
    subtitle_font = content_font = title_font

# 绘制内容
draw.text((width//2, 100), "🤖 Open-AGC", font=title_font, fill='white', anchor='mm')
draw.text((width//2, 200), "你的智能AI助手", font=subtitle_font, fill='white', anchor='mm')

# 绘制熊猫图案（简笔画）
def draw_panda(draw, x, y, size=150):
    # 耳朵
    draw.ellipse([x-size//2, y-size//2, x-size//3, y-size//3], fill='black')
    draw.ellipse([x+size//3, y-size//2, x+size//2, y-size//3], fill='black')
    # 脸
    draw.ellipse([x-size//2, y-size//3, x+size//2, y+size//2], fill='white')
    # 眼睛
    draw.ellipse([x-size//3, y-size//6, x-size//6, y+size//12], fill='black')
    draw.ellipse([x+size//6, y-size//6, x+size//3, y+size//12], fill='black')
    draw.ellipse([x-size//4, y-size//8, x-size//5, y], fill='white')
    draw.ellipse([x+size//5, y-size//8, x+size//4, y], fill='white')
    # 鼻子
    draw.ellipse([x-size//12, y+size//12, x+size//12, y+size//6], fill='black')

draw_panda(draw, width//2, height//2 - 50)

# 功能列表
features = ["📊 数据分析", "🌐 网页抓取", "💻 代码审查", "📝 文档生成", "🔧 系统监控"]
for i, feature in enumerate(features):
    y_pos = height//2 + 150 + i * 70
    draw.rounded_rectangle([150, y_pos-30, width-150, y_pos+30], radius=15, fill='white')
    draw.text((width//2, y_pos), feature, font=content_font, fill='#FF6B9D', anchor='mm')

# 保存
save_path = "/Users/liuhonghao/Projects/open-agc/workspace/xiaohongshu_panda.png"
img.save(save_path)
print(f"图片已保存: {save_path}")
```

### 第二步：编写小红书爆款文案

**标题模板**：
```
🤖 发现了一个超实用的AI助手！工作学习效率直接翻倍💯
```

**正文结构**：
1. **开场吸引**：姐妹们！最近发现了一个宝藏AI助手...
2. **功能列表**：用 emoji + 分点列出核心功能
3. **价值点**：强调"真能干活"而不仅是聊天
4. **行动号召**：自从用了...效率提升...
5. **互动引导**：评论区聊聊...
6. **话题标签**：#AI助手 #效率工具 #打工人必备 等（6-8个）

### 第三步：自动登录小红书创作者平台

```python
# 使用浏览器自动化工具
browser_automation(action="goto", url="https://creator.xiaohongshu.com/publish/publish")

# 如需登录，可点击登录按钮并填写验证码
# 注意：小红书登录可能需要短信验证码，需用户协助
```

### 第四步：上传内容

1. **切换到图文模式**：
   - 点击页面上的"上传图文"选项卡
   
2. **上传图片**：
   - 使用 `upload` 动作上传生成的图片
   - 或手动拖拽图片到上传区域

3. **填写标题和正文**：
   - 使用 `fill` 动作填写标题输入框
   - 使用 `fill` 动作填写正文文本域

4. **发布**：
   - 点击"发布"或"立即发布"按钮

## 关键配置

### 图片规格
- **尺寸**：900 x 1200px（3:4 比例，小红书推荐）
- **背景色**：粉色系（#FF6B9D）或其他品牌色
- **格式**：PNG 或 JPG

### 文案规范
- **标题**：20-25字，含 emoji 和情绪词（超实用、绝绝子、yyds）
- **正文**：分段清晰，每段3-4行
- **表情**：多用 emoji 增加活泼感
- **标签**：结尾加6-8个相关话题标签

### 发布路径
- 小红书创作者平台：https://creator.xiaohongshu.com/publish/publish

## 注意事项

1. **登录问题**：小红书可能需要短信验证码，首次登录需用户协助
2. **图片路径**：确保使用绝对路径 `/Users/liuhonghao/Projects/open-agc/workspace/`
3. **字体兼容性**：macOS 使用 PingFang 字体，Linux 可能需要更换
4. **浏览器限制**：文件选择对话框可能需要用户手动操作
5. **内容审核**：避免敏感词，确保内容符合社区规范

## 扩展能力

- 可生成多张图片组成轮播图
- 可针对不同主题（科技、美妆、美食）调整配色和内容
- 可批量生成多组内容用于矩阵账号
