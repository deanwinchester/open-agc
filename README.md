# 🐼 Open-AGC · 熊猫事务所

> **Open-AGC (Agency) — 熊猫事务所**，一款基于本地电脑操作环境构建的全能型 AI 智能体框架。

![Panda Logo](/static/icon_rounded.png)

Open-AGC 能够自主规划、思考并执行终端命令、文件系统操作、Python 脚本、浏览器自动化以及键鼠控制。项目包含强大的抽象核心与极具现代感的 **Panda Theme（熊猫流光主题）** 网页交互界面。

*Read this in other languages: [English](README_en.md)*

## 📸 界面预览

| 聊天界面 | 任务管理 | 进程管理 |
|:---:|:---:|:---:|
| ![聊天界面](assets/screenshot_home.png) | ![任务管理](assets/screenshot_tasks.png) | 截图待更新 |
| **设置面板** | **模型调用日志** | **下载管理** |
| 截图待更新 | 截图待更新 | 截图待更新 |

## 🌟 核心特性 (Features)

- **多模型即插即用 (Plug & Play LLMs)**: 基于 `litellm` 支持 OpenAI、Anthropic、Gemini、DeepSeek、Moonshot 等商业模型及 Ollama、vLLM、SGLang、llama.cpp 等本地部署方案。Web 界面提供一键切换与可视化配置。
- **后台任务系统 (Background Task System)**: 支持定时/一次性任务调度，shell 命令超时自动后台化，服务重启后自动恢复未完成任务与孤儿进程管理。
- **物理设备控制 (PyAutoGUI)**: Agent 可直接操控鼠标键盘，触发 FAILSAFE 防呆机制（鼠标移至屏幕四角强制中止）。
- **浏览器自动化 (Playwright)**: 内置 Chromium 浏览器引擎，支持网页导航、表单填写、截图、JS 执行等完整浏览器操控能力。
- **现代化 Web 界面**: **Panda Theme（熊猫流光主题）**，竹青色系 + 玻璃拟态面板，WebSocket 实时推送工具执行状态。前端基于 Vite 构建，JS 模块化组织，支持 sourcemap 调试。
- **国际化 (i18n)**: 自动检测浏览器语言，中英双语无缝切换。
- **智能记忆系统 (Smart Memory Engine)**:
  - **SQLite FTS5 全文检索** + BM25 排序，持久化跨会话记忆。
  - **向量语义记忆 (Vector Semantic Memory)**: 支持语义级记忆检索。
  - **静默后台摘要 (Auto-Memory)**: 每轮对话后自动萃取关键事实存入记忆库，下次对话自动注入相关上下文。
- **对话历史检索 (Search History)**: 全量对话历史词级部分匹配搜索，支持检索 task_steps 数据库，上下文自动注入关键发现。
- **技能树系统 (Skills System)**: Agent 可自主编写 Markdown 格式 SOP 模板（`skills/` 目录），支持导入、验证与安全管理。
- **工具信任评分 (Auto-Tool Graduation)**: 工具调用成功率自适应统计，自动毕业为可信工具，减少权限提示。
- **并行子代理 (Parallel Sub-Agent)**: 支持并行执行多个子代理任务，输出深度综合分析报告。
- **智能体防失控护盾 (Agent Safeguards)**:
  - **死循环检测 (Tool Loop Detection)**: 自动识别重复无效调用并在连续出错时强制注入新思路。
  - **上下文字段动态压缩**: 单次工具返回超 15K 字符自动截断。
  - **最大迭代次数硬保险 (Max Iterations)**: 可配置的全局回合数上限，防范意外 Token 账单。
  - **PID 自保护机制**: 自动记录服务进程 PID，拦截对服务本身及其父进程（VS Code Debugger）的误杀操作。
  - **tool_call JSON 自动修复**: LLM 返回格式不标准的 JSON 参数时自动修复并继续。
  - **异常自动恢复**: 未预期异常时自动重试一次，注入错误上下文让 Agent 换策略。
- **MCP 协议支持 (MCP Tool)**: 支持 Model Context Protocol，可集成外部 MCP 工具服务。
- **Docker 部署**: 提供 `Dockerfile` 和 `docker-compose.yml`，一键容器化运行（含 xvfb 无头显示支持）。

---

## 🚀 快速开始

### 1. 克隆项目
```bash
git clone https://github.com/deanwinchester/open-agc.git
cd open-agc
```

### 2. 一键启动
自动化脚本会处理虚拟环境创建、依赖安装并启动服务：

- **macOS / Linux**: 运行 `./start.sh`
- **Windows**: 双击运行 `start.bat`

启动后访问 `http://localhost:8000`。

### 3. 配置 API Key
在 Web 界面的 **Settings（设置）→ 系统配置** 中填写 API Key 并保存，无需手动编辑配置文件。支持通过 `.env` 文件设置环境变量（参考 `.env.example`）。

### 4. 命令行模式
```bash
# 交互式 REPL
python main.py

# 单次查询
python main.py "列出当前目录的文件"
```

---

## 🐳 Docker 部署

```bash
# 构建并启动
docker compose up -d

# 或单独构建
docker build -t open-agc .
docker run -d -p 8000:8000 -v ./data:/app/data -v ./workspace:/app/workspace open-agc
```

---

## 🔌 插件系统 (Plugin System)

Open-AGC 支持即插即用的插件架构。插件存放在 `plugins/` 目录，服务启动时自动发现并加载，前端菜单动态渲染。

### 插件规范

每个插件是一个子目录，必须包含 `plugin.json` 清单文件和 `__init__.py` 入口：

```
plugins/<name>/
├── plugin.json          # 清单：name, version, menu, dependencies
├── __init__.py          # init_plugin(context) → PluginInstance
├── routes.py            # FastAPI APIRouter（可选）
├── static/              # 前端资源（可选）
│   ├── plugin.js        # 前端入口
│   └── plugin.css       # 样式
└── requirements.txt     # 插件依赖（可选）
```

`plugin.json` 示例：

```json
{
  "name": "my-plugin",
  "version": "1.0.0",
  "description": "插件描述",
  "menu": {
    "section": "my-section",
    "label": "菜单名称",
    "icon": "🔧",
    "views": [
      {"id": "view-id", "label": "视图名称"}
    ]
  }
}
```

### 内置插件：open-agc-train

模型训练、微调、PPL 评估与 Benchmark 测评功能作为独立插件存放于 `plugins/open-agc-train/`。

```bash
# 安装训练依赖（可选，默认跳过以加速主程序启动）
pip install -r plugins/open-agc-train/requirements.txt

# 数据迁移（如有旧版训练数据）
cd plugins/open-agc-train
python db.py migrate --from ../../data/chat_history.db --to ../../data/plugins/open-agc-train/training.db
```

---

## 🛠️ 工具矩阵 (Tools)

| 工具 | 功能 | 依赖 |
|------|------|------|
| `shell` | 执行终端命令 | subprocess |
| `python_repl` | 执行临时 Python 脚本 | subprocess |
| `filesystem` | 文件读写操作 | — |
| `browser` | 浏览器自动化（Chromium） | Playwright |
| `computer` | 键鼠物理控制 | PyAutoGUI |
| `web_search` | 网络搜索 | duckduckgo-search |
| `memory` | 记忆管理（增删查） | SQLite FTS5 |
| `search` | 对话历史全文搜索 | SQLite FTS5 |
| `download` | 文件下载管理（含断点续传） | — |
| `interaction` | 用户交互（询问/确认） | — |
| `auto_tool` | 工具信任评分与自动毕业 | — |
| `permissions` | 工具权限管理 | — |
| `sandbox` | 统一沙箱路径检查 | — |
| `mcp_tool` | MCP 协议外部工具集成 | — |
| `discovery` | 工具发现与自省 | — |
| `email_tool` | 邮件搜索与发送 | — |
| `save_skill` | 技能学习与持久化 | — |
| `system_mac` | macOS 系统级操作 | — |

---

## 🏗️ 项目结构

```
open-agc/
├── agent/              # Agent 核心循环与编排
├── api/                # FastAPI 服务端（路由、WebSocket）
├── core/               # 核心模块（LLM 客户端、记忆存储、插件管理、路径管理）
├── tools/              # 工具集（18 个工具）
├── plugins/            # 插件目录
├── static/             # 前端资源（SPA + Vite 构建）
│   ├── js/             # JS 模块（缓存、导航、插件、会话、设置、任务等）
│   ├── css/            # CSS 模块
│   ├── vendor/         # 本地化第三方库（marked、highlight.js、chart.js）
│   └── views/          # 视图模板
├── skills/             # Agent 技能库（Markdown SOP）
├── data/               # 运行时数据（SQLite、配置）
├── models/             # 本地模型文件
├── workspace/          # 沙箱工作目录
├── main.py             # CLI 入口
├── launcher.py         # PyInstaller 打包入口
├── start.sh / start.bat   # 一键启动脚本
├── Dockerfile          # Docker 镜像
└── docker-compose.yml  # Docker Compose 编排
```

---

## 📦 打包分发

- **macOS**: 运行 `./build_mac.sh` → `dist/*.dmg`
- **Windows**: 运行 `build_win.bat` → `dist/*.zip`

打包后的应用将用户数据存储在系统标准路径：
- macOS: `~/Library/Application Support/Open-AGC/`
- Windows: `%APPDATA%\Open-AGC\`

---

## ⚠️ 安全警告 (Security Caveats)

- **命令权限**: Agent 会真实地在宿主机执行 shell 指令和 Python 代码，请勿赋予其修改敏感数据的权限或在无看护的生产环境中运行。
- **沙箱模式**: 建议开启 `sandbox_mode`（默认启用），限制 Agent 的文件访问范围在 `workspace/` 目录内。
- **硬件控制防呆**: 物理操控模式（`computer_control`）下，将鼠标滑向屏幕四角任意顶角可触发 PyAutoGUI FAILSAFE 强制中止。
- **费用控制**: 虽然内置了死循环检测和上下文截断，仍建议定期检查商业大模型 API 的调用开销。

---

## 🤝 贡献 (Contributing)

欢迎提交 Pull Requests 丰富 `tools/` 工具库、`plugins/` 插件生态或 `skills/` 技能模板！
