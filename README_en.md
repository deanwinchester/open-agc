# Open-AGC (Agentic Computer Control)

![Panda Logo](/static/icon-panda.svg) 

Open-AGC is an all-in-one AI agent framework built for local computer control. It provides an autonomous assistant capable of planning, thinking, and executing terminal commands, file system operations, temporary Python scripts, and direct keyboard/mouse control. It features a modern, sleek web interface.

## 🌟 Core Features

- **Plug & Play LLMs**: Supports OpenAI, Anthropic, Gemini, DeepSeek, Kimi (Moonshot), and more via `litellm`. One-click configuration via Web UI.
- **Physical Device Control**: Agent can take over mouse and keyboard using `PyAutoGUI`.
- **Modern Web Interface**: **Panda Theme** with smooth transitions and real-time tool status updates.
- **Smart Memory Engine**: Persistent cross-session memory powered by SQLite FTS5.
- **Skill Tree**: Agent can autonomously learn and write Markdown-based SOPs for frequent tasks.
- **Agent Safeguards**: Loop detection and context compression to prevent infinite loops and excessive token usage.

---

## 🛠️ Installation & Usage

### 1. Clone the project
```bash
git clone https://github.com/deanwinchester/open-agc.git
cd open-agc
```

### 2. Quick Start
We provide automated scripts that handle environment setup and dependency installation:

- **macOS / Linux**: Run `./start.sh`
- **Windows**: Run `start.bat`

### 3. API Key Configuration
After launching, go to **Settings** -> **System Config** in the web interface to save your API keys. No manual file editing required.

---

## 🚀 Packaging

To create standalone binaries:
- **macOS**: Run `./build_mac.sh` to generate a `.dmg` installer.
- **Windows**: Run `build_win.bat` to generate a portable `.zip` archive.

---

## ⚠️ Security Caveats
- **Permission**: The agent executes real shell commands and Python code. Do not run as root or without supervision in production.
- **Fail-safe**: In hardware control mode, move the mouse cursor to any corner of the screen to trigger an emergency stop.

## 🤝 Contributing
Contributions to the `tools/` library or custom `skills/` are welcome!
