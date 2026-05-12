# 网页数据抓取技能

## 触发条件
当用户需要从网页提取信息、抓取数据、或采集网络内容时。

## 步骤
1. 使用 `search_web` 搜索目标网页或直接使用用户提供的 URL
2. 使用 `execute_python` 运行抓取脚本
   ```python
   import requests
   from bs4 import BeautifulSoup

   resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
   soup = BeautifulSoup(resp.text, 'html.parser')
   ```
3. 根据用户需求提取：标题、正文、链接、表格、图片等
4. 将提取的数据整理为结构化格式（Markdown 表格或 JSON）
5. 如果页面需要 JavaScript 渲染，建议使用 `browser_automation` 工具

## 注意事项
- 尊重 robots.txt，不要过于频繁请求
- 添加合适的请求间隔（time.sleep）
- 处理好编码问题（encoding）

## 涉及工具
- `search_web` — 搜索目标网页
- `execute_python` — 运行抓取脚本
- `browser_automation` — 处理 JavaScript 渲染的页面
