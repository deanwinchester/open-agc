# Open-AGC (Agentic Computer Control)

Open-AGC 是一款基于本地电脑操作环境构建的全能型智能体框架。该项目提供能够自主规划、思考并执行终端命令、文件系统操作、临时 Python 计算脚本以及直接控制键鼠的 AI 助手。不仅包含了强大易用的抽象核心代码，还配有极具现代感的炫酷网页交互界面。

![Interface Preview](/static/preview.png) *(可将预览图放在此处)*

## 🌟 核心特性 (Features)
- **多模型支持 (Plug & Play LLMs)**: 基于 [`litellm`](https://docs.litellm.ai/) 支持 OpenAI, Anthropic, Gemini, DeepSeek, Kimi, GLM 或本地部署模型（Ollama / vLLM）。
- **物理设备控制 (PyAutoGUI)**: 支持让 Agent 取代您接管鼠标和键盘，点击特定屏幕像素与输入快捷键。
- **动态隔离验证 (Python REPL)**: Agent 可在运行中编写代码，下发到隔离的零时环境中试运行检查，获取控制台报错日志后再自我纠正。
- **现代化 Web 界面**: 全新打造的流光渐变拟物设计（Glassmorphism），不仅有暗黑模式，更支持工具状态气泡悬浮推送，科技感十足。
- **纯粹且轻量**: 后端使用标准 Python 和 FastAPI 构建，前端使用原生 Vanilla JS + CSS 打造，无需配置 Node.js 繁重的依赖。

## 🛠️ 安装与配置 (Installation)

### 1. 克隆/打开项目目录
进入项目目录：
```bash
cd open-agc
```

### 2. 构建独立 Python 环境
为了保证依赖不冲突，建议使用 Python `venv` 虚拟环境：
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```
*(如果需要物理硬件控制模块，请确保安装了：`pip install pyautogui Pillow`)*

### 3. 配置您的 API Key
我们将配置文件拆分保障安全。通过复制并编辑环境模板来填入您的大模型密钥：
```bash
cp .env.example .env
nano .env  # 或者使用您喜欢的编辑器打开并填入 OPENAI_API_KEY 之类
```

## 🚀 运行方法 (Usage)

Open-AGC 提供了两种不同的运行入口，满足不同用户的偏好。

### 选项 A：启动高级 Web 界面服务 (推荐)
进入工作目录并确保激活了环境后，启动 FastAPI 后端服务：
```bash
uvicorn api.server:app --host 0.0.0.0 --port 8000
```
启动后在浏览器中打开： **http://localhost:8000** 

### 选项 B：纯终端命令行交互 (CLI)
如果您更喜欢 Hacker 风格的极简命令行应用：
```bash
python main.py
```

## ⚠️ 安全警告 (Security Caveats)
- **不可逆的命令权限**：该智能体会**真实地**在您的宿主机环境中执行终端 `bash` 指令和 Python 代码，包含删除文件等的最高权限！请不要赋予它修改敏感数据的命令或者切勿在生产环境中无看护运行。
- **硬件控制防呆机制**：物理操控模式下（`computer_control`工具），如果它的鼠标走向了失控的死循环，请立刻**将实体鼠标滑向屏幕四角的任意一个顶角**，这会触发 PyAutoGUI 的 `FAILSAFE` 异常并强制中止其运行！

## 🤝 贡献 (Contributing)
欢迎提交 Pull Requests 来丰富 `tools/` 目录下的可用工具库（例如：搜索网络扩展、浏览器控制扩展等）。
