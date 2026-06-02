# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Critical rules

- **NEVER push to the `release` branch unless the user explicitly and directly asks you to.** Pushing to `main` is fine. Pushing to `release` triggers CI builds and Docker image releases — only do it on explicit user request.
- **NEVER commit or push after making code changes until the user has tested and confirmed the changes work.** Write code, present it to the user, and wait for explicit approval ("可以", "提交", "commit", "推送" or similar) before staging, committing, or pushing.
- **ALWAYS use UTF-8 encoding for all file operations.** PowerShell's `Get-Content` and `Set-Content` default to GBK/UTF-16 which corrupts Chinese characters. Use Python's `open(path, 'w', encoding='utf-8')` or the Edit/Write tool for any file modifications involving non-ASCII text. If PowerShell is unavoidable, use `[System.IO.File]::ReadAllText()` and `Set-Content -Encoding utf8`.

## Development commands

## Development commands

```bash
# Start the web UI (auto-creates venv, installs deps, launches on :8000)
./start.sh          # macOS/Linux
start.bat           # Windows

# Start manually (if venv already set up)
python -m uvicorn api.server:app --host 0.0.0.0 --port 8000

# CLI / REPL mode (text-based agent, no browser)
python main.py

# One-shot CLI query
python main.py "list files in current directory"

# Package into standalone executable
./build_mac.sh      # macOS → dist/*.dmg
build_win.bat       # Windows → dist/*.zip

# Install ML training dependencies (optional, large)
pip install -r plugins/open-agc-train/requirements.txt
```

No formal test suite; manual testing is done by starting the server and interacting through the web UI at `http://localhost:8000`.

## Architecture

Open-AGC is an AI agent framework that gives an LLM access to real tools (shell, filesystem, Python REPL, mouse/keyboard control, browser automation). It has both a CLI (`main.py`) and a FastAPI web server with a single-page frontend (Panda theme).

### Entry points and startup flow

- **`main.py`** — CLI mode. Creates an `OpenAGCAgent`, runs a REPL loop. Supports `:image <path>` for vision, `-i` flag for one-shot queries.
- **`launcher.py`** — PyInstaller entry point for packaged builds. Sets up writable data paths, then calls `uvicorn.run("api.server:app")` and opens the browser.
- **`api/server.py`** — The main web server (~2900 lines). On startup: initializes SQLite DB, discovers plugins, reconciles downloads, trains checkpoints. Mounts static files from `static/` and from each plugin's `static/` directory.

### Agent loop (`agent/agent.py`)

`OpenAGCAgent` is the core orchestration class:

1. On init: loads skills from `skills/` dir, builds a Chinese system prompt with date/time, instantiates ~12 tool objects, loads tool schemas.
2. `run_turn()`: appends user message → auto-retrieves relevant memories via FTS5 → enters tool-calling loop (max 30 iterations, configurable).
3. **Tool loop detection**: hashes `tool_name:args`, blocks repeated identical calls (≥3 in recent 10).
4. **Context compaction**: truncates tool results over 15K chars.
5. **Auto-memory**: after a final answer, a background thread asks the LLM to extract key facts and saves them via `MemoryStore`.

### LLM client (`core/llm_client.py`)

Wraps `litellm` for multi-provider support. Key behaviors:

- **Model failover**: tries `default_model` → `fallback_models` (from config.json) in order.
- **Ollama monkeypatch**: `PatchedOllamaConfig` replaces LiteLLM's Ollama handler. Adds `thinking` field extraction, tool call rescue from malformed JSON (5 formats), reasoning_content preservation.
- **API key injection**: reads `config.json.api_keys` → sets appropriate env vars (`OPENAI_API_KEY`, `MOONSHOT_API_KEY`, etc.) before LiteLLM sees them.
- **`_sanitize_for_llamacpp()`**: rewrites messages for GGUF chat templates (merges system prompt into first user message, strips orphaned tool calls).
- **`clean_llm_text()`**: strips `<think>`, `<thought>`, and JSON hallucination wrappers from raw model output.

### Tool system (`tools/`)

All tools extend `BaseTool` (a Pydantic model) and implement:
- `get_openai_schema()` → returns the OpenAI function-calling JSON schema
- `execute(**kwargs)` → runs the tool, returns a string result

| Tool | Key dependency |
|------|---------------|
| `shell.py` — `execute_shell` | `subprocess` |
| `python_repl.py` — `execute_python` | `subprocess` (temp .py file) |
| `filesystem.py` — `read_file` / `write_file` | — |
| `computer.py` — `computer_control` | `pyautogui` (FAILSAFE=True) |
| `browser.py` — `browser_automation` | `playwright` (sync, singleton, isolated thread) |
| `memory.py` — `manage_memory` | `MemoryStore` (shared instance) |
| `web_search.py` — `search_web` | `duckduckgo-search` |
| `email_tool.py` — `search_emails` / `send_email` | — |
| `save_skill.py` — `save_learned_skill` | writes to `skills/` dir |
| `system_mac.py` — `mac_system_action` | macOS-specific |

### Memory system (`core/memory_store.py`)

SQLite FTS5 with BM25 ranking. Two tables: `memories` (with FTS5 virtual table) and `conversations`. Supports three memory types: `core` (long-term facts), `working` (current task context), `episode` (learned knowledge). Chinese text is character-separated for FTS5 tokenization. Auto-categorization by keyword matching.

### Plugin system (`core/plugin_manager.py`)

Plugins live in `plugins/<name>/`. Each must have:
- `plugin.json` — manifest with name, version, optional `menu` (section, label, icon, views)
- `__init__.py` — exports `init_plugin(context: PluginContext) → PluginInstance`

The `PluginInstance` carries a FastAPI `APIRouter` and optional `static_dir`. On startup, the server calls `discover_plugins()`, mounts each plugin's router at `/api/plugin/<name>` and static files at `/static/plugins/<name>`. The frontend fetches `/api/plugins` and dynamically renders sidebar menu sections.

The built-in `open-agc-train` plugin provides model training, finetuning, PPL evaluation, and benchmark testing. The core implementation has been decoupled into `plugins/open-agc-train/`. This separation ensures that heavy ML dependencies don't block the main server startup.

### Frontend (`static/`)

Single-page app: `index.html` + `app.js` + `style.css`. Panda theme with glass-morphism panels, bamboo-green accents. Features:
- i18n: auto-detects browser language (zh-CN / en), all UI strings in `t()` function
- WebSocket at `/ws` for real-time agent progress (tool_start, tool_done, thinking events)
- Plugin menu rendering: sidebar sections dynamically created from plugin manifests
- Built-in views: chat, settings, task manager, model training, plugin manager
- Global download progress banner for model downloads

### Data storage (`core/paths.py`)

- **Dev mode**: data in `<repo>/data/`, skills in `<repo>/skills/`, models in `<repo>/models/`
- **Packaged mode** (PyInstaller): data in `~/Library/Application Support/Open-AGC/` (macOS) or `%APPDATA%/Open-AGC/` (Windows), skills/models/bin copied from bundle on first run
- Config stored as `config.json` in the data directory (API keys, model preferences, sandbox settings)

### Key REST API endpoints

- `GET /api/settings` / `POST /api/settings` — read/write config.json
- `GET /api/plugins` — list loaded plugins; `POST /api/plugins/{name}/toggle` — enable/disable
- `POST /api/training/runs` — create training run; `GET /api/training/runs` — list
- `POST /api/tasks` / `GET /api/tasks` — CRUD for scheduled/oneshot agent tasks
- `GET /api/memories` / `GET /api/memories/categories` — memory store access
- `GET /api/skills` / `POST /api/skills/import` / `POST /api/skills/validate` — skill management with security scanning
- `GET /api/files/{path}` — serve files from sandbox workspace
- `POST /api/llamacpp/download-model` — download GGUF models
- `POST /api/llamacpp/search-models` — search HuggingFace for GGUF models
- `WS /ws` — WebSocket for agent progress streaming

### Local model serving

- `LlamaCppManager` (`core/llamacpp_manager.py`): manages `llama-server` binary (auto-download from GitHub), GGUF model downloads from HuggingFace with resume support, HTTP API proxy
- `SGLangManager` (`core/sglang_manager.py`): manages SGLang server process lifecycle
- Both integrate via LiteLLM: models accessed as `llamacpp/<model>` or `sglang/<model>` in config

### Key configuration (config.json)

```json
{
  "default_model": "moonshot/kimi-latest",
  "fallback_models": ["openai/gpt-4o"],
  "api_keys": { "openai": "...", "kimi": "..." },
  "sandbox_mode": true,
  "sandbox_dir": "./workspace",
  "max_iterations": 30,
  "disabled_skills": [],
  "browser_headless": false
}
```

### Dev docs (dev_docs/)

- 重要优化文档：Agent优化方案.md，关于Agent的优化，随时更新到该文档，执行时也要注意本文档已经优化好的功能，不要受到影响。