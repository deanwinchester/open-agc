# 数据分析技能
当用户需要分析数据文件时：
1. 识别数据格式（CSV、JSON、Excel、SQLite等）
2. 使用 `execute_python` 读取和分析数据：
   ```python
   import csv, json
   # CSV
   with open('data.csv', 'r') as f:
       reader = csv.DictReader(f)
       data = list(reader)
   # JSON
   with open('data.json', 'r') as f:
       data = json.load(f)
   ```
3. 提供基础统计分析：
   - 数据行数、列数、字段类型
   - 数值列的最大/最小/平均/中位数
   - 缺失值统计
   - 分类字段的频率分布
4. 生成可视化图表（使用 matplotlib 或纯文本图表）
5. 如果用户需要更复杂的分析，提供 pandas 方案

输出格式：
- 使用 Markdown 表格展示统计结果
- 用 ASCII 柱状图做简单可视化
- 给出数据质量评估和改进建议
