# Open-AGC (Agentic Computer Control)

![Panda Logo](/static/icon-panda.svg) 

Open-AGC is an omnipotent agentic framework built directly on top of your local operating system. It features an AI assistant capable of autonomously planning, reasoning, and executing terminal commands, interacting with the file system, running temporary Python computational scripts, and physically controlling your mouse and keyboard. Not only does it provide powerful and extensible core abstractions, but it also comes with a sleek, modern, and highly interactive web UI.

*阅读其他语言版本: [简体中文](README.md)*

## 🌟 Core Features

- **Plug & Play Multi-LLM Support**: Powered by `litellm`, it supports OpenAI, Anthropic, Gemini, DeepSeek, or locally deployed models (via Ollama / vLLM). Switch models instantly via the Web UI.
- **Physical Device Control (PyAutoGUI)**: Allows the Agent to take over your mouse and keyboard, clicking on specific screen coordinates and inputting complex shortcuts on your behalf.
- **Modern Web Interface**: A brand new **Panda Theme**, blending bamboo-green accents with a minimalist, rounded glassmorphism aesthetic. It supports dynamic tool execution status bubbles for a futuristic yet warm user experience.
- **Internationalization (i18n)**: The frontend seamlessly auto-detects your browser's language setting, offering native English and Chinese dual-language support.
- **Smart Memory Engine**: 
  - Integrated **SQLite FTS5 Full-Text Semantic Search Engine**, granting the LLM true persistent memory across infinite sessions.
  - **Zero-shot Auto-Retrieve & Background Extraction**: The LLM autonomously extracts your preferences and long-term knowledge via a background thread during conversations, instantly loading relavent context the next time you say hello.
- **Skills System**:
  - The Agent can **proactively create** and self-compile Markdown-based SOP completely tailored for your high-frequency specific workflows (stored in the `skills/` directory).
  - When equipped with the right skills, the Agent can navigate and solve highly complex, multi-environment cross-application tasks effortlessly.
- **Agent Safeguards**:
  - Pioneering **Tool Loop Detection**, which automatically intervenes when the LLM gets stuck repetitively calling identical error-prone commands, forcing it to pivot its reasoning.
  - Features flexible context truncation windows and a global iterations hard-limit to meticulously guard against astronomical accidental API token bills.

---

## 🛠️ Installation & Setup

### 1. Clone & Enter Directory
```bash
cd open-agc
```

### 2. Isolate Python Environment
To prevent dependency conflicts, using a Python `venv` is highly recommended:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```
*(Note for hardware features: Ensure `pyautogui` and `Pillow` are installed if you plan on using physical computer control tools)*

### 3. Configure API Keys
Copy the environment template to securely store your keys:
```bash
cp .env.example .env
```
Alternatively, since the new Web UI offers a **visual settings modal**, you can skip manual `.env` editing entirely and dynamically bind your preferred Provider and API Keys right inside the browser.

---

## 🚀 Usage

Open-AGC offers two distinct entry points tailored to user preferences.

### Option A: Advanced Web UI (Highly Recommended 🌟)
Inside the project root with the environment activated, start the FastAPI backend:
```bash
uvicorn api.server:app --host 0.0.0.0 --port 8000
```
Then, open your browser to: **http://localhost:8000**

### Option B: Terminal CLI Mode
If you prefer a lightweight, hacker-style pure terminal interface without the Web GUI:
```bash
python main.py
```

---

## ⚠️ Security Caveats
- **Irreversible Command Execution**: This agent executes terminal `bash` and Python scripts **natively and directly** on your host OS with your user permissions (including `rm -rf`). **NEVER** run this unguarded in a production environment or grant it tasks modifying highly sensitive personal data.
- **Hardware Control Failsafe**: When in physical control mode (`computer_control` tool), if the mouse cursor ever goes rogue or enters an infinite loop, **violently slam your physical mouse to any of the 4 extreme corners of your monitor**. This triggers PyAutoGUI's `FAILSAFE` exception and instantly kills the agent's execution!
- **Billing Awareness**: Although looping constraints and token limits are built-in, due to the autonomous nature of commercial LLM API calls, closely monitor your API Provider's billing dashboard periodically.

## 🤝 Contributing
Pull Requests are extremely welcome! Feel free to enrich the `tools/` library with broader integrations (like Browser automation extensions) or commit your own awesome specialized SOPs to the Skills directory!
