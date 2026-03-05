# 文档生成技能
当用户需要生成项目文档时：
1. **分析项目结构**：
   ```bash
   find . -type f -name "*.py" -o -name "*.js" -o -name "*.ts" | head -50
   tree -L 2 -I "node_modules|__pycache__|.git|venv"
   ```
2. **生成 README.md**：
   - 项目名称和简介
   - 功能特性列表
   - 技术栈说明
   - 安装步骤（pip install / npm install）
   - 使用方法和示例
   - 配置说明
   - 项目结构目录树
   - License 信息
3. **生成 API 文档**：
   - 从路由文件（如 FastAPI/Express）提取端点
   - 列出每个端点的：URL、方法、参数、响应格式
   - 提供 curl 示例
4. **生成变更日志**：
   ```bash
   git log --oneline --since="2024-01-01" | head -30
   ```
   整理为 CHANGELOG.md 格式

输出格式：使用标准 Markdown，包含代码块、表格、徽章等元素。
