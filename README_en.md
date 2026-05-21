# Open-AGC (Agentic Computer Control)

![Panda Logo](/static/icon-panda.svg)
![Open-AGC Home](assets/screenshot_home.png)

Open-AGC is an all-in-one AI agent framework for local computer control. It provides an autonomous assistant capable of planning, thinking, and executing terminal commands, file system operations, Python scripts, browser automation, and keyboard/mouse control. It features a modern Panda Theme web interface with glass-morphism panels.

## 🌟 Core Features

- **Plug & Play LLMs**: Supports OpenAI, Anthropic, Gemini, DeepSeek, Moonshot, and local models (Ollama, vLLM, SGLang, llama.cpp) via `litellm`. One-click configuration via Web UI.
- **Background Task System**: Scheduled and one-shot agent tasks, auto-backgrounding for long-running shell commands, task recovery and orphan process management after server restart.
- **Physical Device Control (PyAutoGUI)**: Agent can take over mouse and keyboard. Move cursor to any screen corner to trigger FAILSAFE emergency stop.
- **Browser Automation (Playwright)**: Built-in Chromium engine for web navigation, form filling, screenshots, JS execution, and full browser control.
- **Modern Web Interface**: **Panda Theme** with bamboo-green accents and glass-morphism panels. WebSocket-based real-time tool status streaming. Vite-bundled frontend with modular JS and sourcemap debugging support.
- **i18n**: Auto-detects browser language (zh-CN / en), seamless bilingual switching.
- **Smart Memory Engine**:
  - **SQLite FTS5** full-text search with BM25 ranking for persistent cross-session memory.
  - **Vector Semantic Memory**: semantic-level memory retrieval.
  - **Auto-Memory**: background extraction of key facts after each conversation, auto-injected on next session.
- **Search History**: word-level partial-match search across full conversation history and task_steps database, with automatic context injection of key findings.
- **Skills System**: Agent autonomously learns and writes Markdown SOPs (`skills/` directory). Supports import, validation, and security scanning.
- **Auto-Tool Graduation**: adaptive trust scoring based on tool call success rate; trusted tools auto-graduate to reduce permission prompts.
- **Parallel Sub-Agents**: concurrent sub-agent execution with deep synthesis reports.
- **Agent Safeguards**:
  - **Loop Detection**: identifies repeated failed tool calls and forces alternative approaches.
  - **Context Compression**: auto-truncates tool results exceeding 15K characters.
  - **Max Iterations**: configurable hard limit to prevent runaway token usage.
- **MCP Protocol Support**: integrate external MCP tool services.
- **Docker Deployment**: `Dockerfile` and `docker-compose.yml` included (with xvfb headless display support).

---

## 🚀 Quick Start

### 1. Clone
```bash
git clone https://github.com/deanwinchester/open-agc.git
cd open-agc
```

### 2. Launch
Automated scripts handle venv creation, dependency installation, and server startup:

- **macOS / Linux**: `./start.sh`
- **Windows**: `start.bat`

Open `http://localhost:8000` in your browser.

### 3. Configure API Keys
Go to **Settings → System Config** in the Web UI to save your API keys. Optionally use a `.env` file (see `.env.example`).

### 4. CLI Mode
```bash
# Interactive REPL
python main.py

# One-shot query
python main.py "list files in current directory"
```

---

## 🐳 Docker

```bash
# Build and start
docker compose up -d

# Or manually
docker build -t open-agc .
docker run -d -p 8000:8000 -v ./data:/app/data -v ./workspace:/app/workspace open-agc
```

---

## 🔌 Plugin System

Plugins live in `plugins/` and are auto-discovered on startup. Each plugin needs a `plugin.json` manifest and `__init__.py` entry point.

```
plugins/<name>/
├── plugin.json          # manifest: name, version, menu, dependencies
├── __init__.py          # init_plugin(context) → PluginInstance
├── routes.py            # FastAPI APIRouter (optional)
├── static/              # frontend assets (optional)
└── requirements.txt     # plugin dependencies (optional)
```

The built-in `open-agc-train` plugin provides model training, finetuning, PPL evaluation, and benchmark testing.

```bash
pip install -r plugins/open-agc-train/requirements.txt
```

---

## 🛠️ Tools

| Tool | Description | Dependency |
|------|-------------|------------|
| `shell` | Execute terminal commands | subprocess |
| `python_repl` | Execute Python scripts | subprocess |
| `filesystem` | Read/write files | — |
| `browser` | Browser automation (Chromium) | Playwright |
| `computer` | Keyboard/mouse control | PyAutoGUI |
| `web_search` | Web search | duckduckgo-search |
| `memory` | Memory CRUD | SQLite FTS5 |
| `search` | Full conversation history search | SQLite FTS5 |
| `download` | File downloads with resume support | — |
| `interaction` | User interaction (ask/confirm) | — |
| `auto_tool` | Trust scoring & auto-graduation | — |
| `permissions` | Tool permission management | — |
| `sandbox` | Unified sandbox path checks | — |
| `mcp_tool` | MCP protocol external tool integration | — |
| `discovery` | Tool introspection & discovery | — |
| `email_tool` | Email search & send | — |
| `save_skill` | Skill learning & persistence | — |
| `system_mac` | macOS system actions | — |

---

## 🏗️ Project Structure

```
open-agc/
├── agent/              # Agent core loop & orchestration
├── api/                # FastAPI server (routes, WebSocket)
├── core/               # Core modules (LLM client, memory, plugins, paths)
├── tools/              # Tool set (18 tools)
├── plugins/            # Plugin directory
├── static/             # Frontend SPA (Vite-bundled)
│   ├── js/             # JS modules (cache, nav, plugins, sessions, settings, tasks)
│   ├── vendor/         # Localized third-party libs (marked, highlight.js, chart.js)
│   └── views/          # View templates
├── skills/             # Agent skills (Markdown SOPs)
├── data/               # Runtime data (SQLite, config)
├── workspace/          # Sandbox working directory
├── main.py             # CLI entry point
├── launcher.py         # PyInstaller entry point
├── start.sh / start.bat   # One-click launch scripts
├── Dockerfile          # Docker image
└── docker-compose.yml  # Docker Compose config
```

---

## 📦 Packaging

- **macOS**: `./build_mac.sh` → `dist/*.dmg`
- **Windows**: `build_win.bat` → `dist/*.zip`

Packaged apps store user data at:
- macOS: `~/Library/Application Support/Open-AGC/`
- Windows: `%APPDATA%\Open-AGC\`

---

## ⚠️ Security Caveats

- **File Permissions**: The agent executes real shell commands and Python code with the user's privileges. Enable `sandbox_mode` (on by default) to restrict file access to the `workspace/` directory.
- **Hardware Fail-safe**: In `computer_control` mode, move the cursor to any screen corner to trigger an emergency PyAutoGUI FAILSAFE stop.
- **API Costs**: Monitor commercial LLM API usage despite built-in loop detection and context compression safeguards.

---

## 🤝 Contributing

Pull Requests are welcome for the `tools/` library, `plugins/` ecosystem, and `skills/` templates!
