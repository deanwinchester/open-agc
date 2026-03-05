# 网页数据抓取技能
当用户需要从网页提取信息时：
1. 使用 `search_web` 搜索目标网页或直接使用用户提供的URL
2. 使用 `execute_python` 运行抓取脚本：
   ```python
   import requests
   from bs4 import BeautifulSoup
   resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
   soup = BeautifulSoup(resp.text, 'html.parser')
   ```
3. 根据用户需求提取：标题、正文、链接、表格、图片等
4. 将提取的数据整理为结构化格式（Markdown表格或JSON）
5. 如果页面需要JavaScript渲染，提醒用户可能需要安装selenium

注意事项：
- 尊重robots.txt，不要过于频繁请求
- 添加合适的请求间隔（time.sleep）
- 处理好编码问题（encoding）
