import json
import re
import time as _time
import threading
import hashlib
from typing import List, Dict, Any, Optional, Callable
import os
import sys
import platform
import shutil
import queue

from core.paths import get_data_path, get_skills_dir

from core.llm_client import LLMClient, build_user_message, extract_screenshot_data
from core.logger import SessionLogger
from core.memory_store import MemoryStore
from core.skill_store import SkillStore
from core.token_budget import TokenBudget, estimate_messages_tokens
from core.reflection import ReflectionEngine
from core.knowledge_graph import KnowledgeGraph
from core.stats_manager import get_stats_manager
from tools.shell import ShellTool
from tools.filesystem import ReadFileTool, WriteFileTool, EditFileTool
from tools.search import GrepSearchTool, GlobTool
from tools.python_repl import PythonREPLTool
from tools.computer import ComputerTool
from tools.memory import MemoryTool
from tools.web_search import WebSearchTool
from tools.system_mac import MacSystemTool
from tools.save_skill import SaveSkillTool
from tools.browser import BrowserAutomationTool
from tools.download import DownloadTool
from tools.email_tool import SearchEmailTool, SendEmailTool
from tools.auto_tool import (DynamicTool, load_all_dynamic_tools,
                              generate_tool_code, validate_tool_code,
                              save_tool_code, init_auto_tools)
from agent.sub_agent import SubAgent, TOOL_SETS
from tools.discovery import ToolDiscoveryTool
from tools.mcp_tool import get_mcp_manager
from tools.interaction import AskUserQuestionTool, PauseAndWaitTool, TaskPaused, SearchHistoryTool, PauseAndWaitTool, TaskPaused
from tools.sandbox import EnterWorktreeTool, ExitWorktreeTool
from tools.self_review import SelfReviewTool


def _detect_system_env() -> str:
    """Detect the current system environment and return a description string for the system prompt."""

    # OS detection
    system = platform.system()  # "Darwin", "Windows", "Linux"
    os_name = system
    if system == "Darwin":
        mac_ver = platform.mac_ver()[0]
        os_name = f"macOS {mac_ver}" if mac_ver else "macOS"
    elif system == "Windows":
        win_ver = platform.version()
        os_name = f"Windows {win_ver}" if win_ver else "Windows"
    elif system == "Linux":
        try:
            import subprocess
            distro = subprocess.run(["lsb_release", "-ds"], capture_output=True, text=True, timeout=3).stdout.strip()
            os_name = distro or "Linux"
        except Exception:
            os_name = "Linux"

    # Architecture
    arch = platform.machine()  # "arm64", "x86_64", etc.

    # Shell
    default_shell = os.environ.get("SHELL", "unknown").split("/")[-1]  # "zsh", "bash", etc.

    # Python
    py_ver = platform.python_version()

    # Package managers
    pkg_managers = []
    if shutil.which("brew"):
        pkg_managers.append("brew")
    if shutil.which("apt"):
        pkg_managers.append("apt")
    if shutil.which("pip3"):
        pkg_managers.append("pip3")
    elif shutil.which("pip"):
        pkg_managers.append("pip")

    pkg_hint = ", ".join(pkg_managers) if pkg_managers else "未检测到常见包管理器"

    # Home directory
    home = os.path.expanduser("~")

    # Sudo check (non-invasive: check if sudo binary exists and user has recent sudo timestamp)
    sudo_available = "sudo" if shutil.which("sudo") else ""
    sudo_hint = ""
    if sudo_available and system != "Windows":
        sudo_hint = (
            "sudo 可用。注意：在子进程中运行时，sudo 没有 TTY 无法交互式输入密码。"
            "需要使用 -S 从 stdin 读密码，或用 -n 跳过密码（需 NOPASSWD 配置），"
            "或使用 `echo password | sudo -S command`"
        )

    parts = [
        f"# 系统环境",
        f"- 操作系统：{os_name}",
        f"- 架构：{arch}",
        f"- 默认 Shell：{default_shell}",
        f"- Python 版本：{py_ver}",
        f"- 包管理器：{pkg_hint}",
        f"- 用户主目录：{home}",
    ]

    if system == "Windows":
        parts.append(
            f"- **Windows 注意事项**：PowerShell 中的 curl 命令是 Invoke-WebRequest 别名，"
            f"需使用 curl.exe。中文乱码可在命令前添加 "
            f"`$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new()`。"
        )
    elif system == "Darwin":
        parts.append(
            f"- **macOS 注意事项**：使用 brew 安装软件。Shell 工具使用 zsh/bash，"
            f"不支持 PowerShell 语法。系统偏好中文界面。"
        )
    elif system == "Linux":
        parts.append(f"- **Linux 注意事项**：使用 apt/yum 安装软件，标准 POSIX shell 环境。")

    if sudo_hint:
        parts.append(f"- **sudo 注意事项**：{sudo_hint}")

    return "\n".join(parts)


class OpenAGCAgent:
    """
    Main Agent Loop handling context, Tool calling, and orchestration.
    Supports real-time progress callbacks for task tracking.
    Features smart memory with TF-IDF semantic retrieval.
    """
    def __init__(self, model: str = "gpt-4o", session_id: Optional[int] = None,
                 logger: Optional[SessionLogger] = None,
                 pre_enabled_tools: Optional[set] = None):
        self.session_id = session_id
        self._consecutive_failures = 0
        self._correction_attempts = 0
        self._max_correction_attempts = 5
        self._in_self_review = False
        self._should_stop = False
        self._self_review_history: list = []
        self.logger = logger
        self.llm = LLMClient(default_model=model)
        self._pre_enabled_tools = pre_enabled_tools or set()
        self._session_sandbox_whitelist: set = set()
        self.pending_messages: list = []
        self._session_sandbox_whitelist: set = set()  # One-time approved paths
        self._session_permission_whitelist: set = set()  # Session-approved command categories
        self._session_network_whitelist: set = set()  # Session-approved network domains
        # Load config to check disabled skills
        disabled_skills = []
        config_path = get_data_path("config.json")
        if os.path.exists(config_path):
            try:
                import json
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    disabled_skills = config.get("disabled_skills", [])
            except Exception: pass

        # Load skill index (progressive — full content is retrieved on demand)
        self.skill_store = SkillStore(skills_dir=get_skills_dir())

        # Inject current date/time so the LLM knows "today"
        from datetime import datetime
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        current_date = datetime.now().strftime("%Y年%m月%d日")

        # Store config for later use
        self.sandbox_dir = None
        self.browser_headless = False
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    if config.get("sandbox_mode", True):
                        self.sandbox_dir = config.get("sandbox_dir", os.path.abspath(os.path.join(os.getcwd(), "workspace")))
                    self.browser_headless = config.get("browser_headless", False)
                    # Initialize token budget from config if available
                    budget_cfg = config.get("context_budget", {})
            except Exception:
                budget_cfg = {}

        self.token_budget = TokenBudget(config=budget_cfg if budget_cfg else None)

        # Initialize smart memory store before reflection/knowledge engines
        self.memory_store = MemoryStore(
            db_path=get_data_path("memory.db"),
            session_id=self.session_id
        )

        self.reflection_engine = ReflectionEngine(
            db_path=get_data_path("agent.db"),
            memory_store=self.memory_store,
            llm_client=self.llm,
            session_id=self.session_id,
        )
        self.knowledge_graph = KnowledgeGraph(
            db_path=get_data_path("agent.db"),
            session_id=self.session_id,
        )

        self.system_prompt_base = (
            f"# 身份与能力\n"
            f"你是 Open-AGC，一个强大的 AI 智能体，能够执行终端命令、运行 Python 代码、"
            f"操作文件系统，以及物理控制电脑的鼠标和键盘。"
            f"始终使用你的工具来明确验证假设，不要凭空猜测。\n"
            f"\n--- 当前日期与时间 ---\n"
            f"当前时间：{current_time}（{current_date}）\n"
            f"你的训练数据有知识截止日期。对于任何关于近期事件、当前新闻、最新动态或"
            f"时效性信息的问题，你必须使用 search_web 工具获取最新信息。"
            f"绝对不要仅依赖训练数据回答时事问题。\n"
            f"\n# 任务执行规范\n"
            f"\n## 1. 复杂任务先规划再执行\n"
            f"处理涉及多个步骤的复杂任务时，先说明你的计划，然后逐步执行。"
            f"如果任务规模较大（如创建新项目、实现多文件功能），必须先设计方案，"
            f"再创建目录结构，然后分步实现。不要一上来就写代码而不做规划。\n"
            f"\n## 2. 工具调用格式\n"
            f"工具调用格式：`{{\"name\": \"tool_name\", \"arguments\": {{\"key\": \"value\"}}}}`。"
            f"仅当你决定使用工具时，才输出 JSON 对象。对于正常对话回复，直接输出纯文本，严禁使用 JSON 格式。\n"
            f"如想在调用工具前表达思考过程，放在 JSON 之前的独立段落中。\n"
            f"\n## 3. 上下文复用\n"
            f"当用户要求\"重试\"\"再下载一遍\"\"再试一次\"等操作时，"
            f"必须先检查对话历史中的 tool_call 记录，复用已有的 URL、参数、文件路径等数据。"
            f"绝对不要重新浏览网页或重新搜索来获取已知信息。\n"
            f"\n## 4. 失败处理\n"
            f"如果某个方法失败，先分析错误原因再换策略。不要盲目重试同样的操作，"
            f"也不要因为一次失败就完全放弃可行的方法。\n"
            f"\n# 工具使用指南\n"
            f"\n## 工具优先级（按推荐顺序）\n"
            f"1. write_file / edit_file — 创建和修改文件（首选文件操作方式）\n"
            f"2. execute_python — 运行 Python 代码进行数据处理、测试等\n"
            f"3. execute_shell — 执行系统命令（仅当无专用工具可用时）\n"
            f"4. search_file_content / find_files — 搜索文件内容与查找文件\n"
            f"5. search_web — 搜索互联网获取最新信息\n"
            f"6. browser_automation — 虚拟浏览器操作网页\n"
            f"7. search_history — 检索当前会话历史（仅在需要回忆之前内容时使用）\n"
            f"8. 其他专用工具根据场景选用\n"
            f"\n## 大文件下载\n"
            f"如果需要下载超过 100MB 的大文件（如模型文件 .gguf/.safetensors/.bin），"
            f"必须使用 queue_download 工具而非 execute_shell。它支持断点续传，"
            f"不会因为超时而失败。下载进度可在下载管理面板查看。\n"
            f"\n## 长时间任务后台化\n"
            f"当执行耗时操作（下载模型/安装依赖/训练等），shell 返回 [Still Running] 时，"
            f"应立即调用 pause_and_wait 工具暂停自己。系统会保存上下文，后台任务完成后自动恢复执行。"
            f"不要让用户干等着，也不要反复重试。\n"
            f"\n{{system_env}}\n"
            f"\n# 项目创建规范\n"
            f"当需要创建新项目或实现多文件功能时，请遵循以下流程：\n"
            f"\n## 第一步：理解需求\n"
            f"明确用户想要什么。如果需求模糊，先向用户确认。\n"
            f"\n## 第二步：设计方案\n"
            f"在写任何代码前，先规划：架构设计、目录结构、技术选型、文件清单。"
            f"向用户简要说明方案后再开始实施。\n"
            f"\n## 第三步：创建目录结构\n"
            f"先使用 execute_shell 创建项目目录结构（mkdir），再逐步填充文件。"
            f"不要在没建好目录的情况下开始写代码。\n"
            f"\n## 第四步：分步实现\n"
            f"按照依赖顺序逐个创建文件。核心/基础模块先实现，UI/上层模块后实现。"
            f"每完成一个文件或功能，记录进度。\n"
            f"\n## 第五步：验证\n"
            f"文件创建完成后，检查是否能正常运行（如 Python 语法检查、依赖安装、启动测试等）。"
            f"如果无法验证，明确告知用户当前状态。\n"
            f"\n# 文件操作规范\n"
            f"\n## 沙箱文件保存\n"
            f"所有生成的文件（脚本、文档、图片等），如果用户没有显式指定绝对路径，"
            f"必须统一保存在沙箱工作目录（Sandbox Directory: {{cwd_dir}}）中，严禁写在 /tmp 下。\n"
            f"如果需要访问沙箱外的路径，直接使用 read_file/write_file 等工具操作即可。"
            f"系统会自动弹出授权窗口让用户批准。"
            f"绝对不要使用 ask_user_question 来请求路径授权——沙箱机制会全自动处理。\n"
            f"\n## 用户上传文件\n"
            f"用户在聊天中上传的文件保存在沙箱目录下的 uploads/ 子目录中（即 {{cwd_dir}}/uploads/）。"
            f"可以使用 read_file 读取，使用 write_file/edit_file 修改。"
            f"修改后的文件用户可通过聊天界面的文件标签下载。"
            f"不要手动创建 uploads 目录——系统会自动管理。\n"
            f"\n## 图片显示\n"
            f"当你生成了一张图片供用户查看时，请在最终回复中使用 Markdown 语法渲染出来："
            f"`![图片描述](/api/files/生成的文件名.png)`。"
            f"这个内部 API 能将沙箱里的图片直接推送到网页前端显示。\n"
            f"\n## 网页文件上传\n"
            f"优先使用 browser_automation（虚拟浏览器）工具的 upload 动作将文件填入网页。"
            f"如果遇到必须通过操作系统原生文件选择框处理的情况，"
            f"可临时使用 computer_control（键鼠控制工具）来操作系统的上传弹窗。\n"
            f"\n# 记忆与技能系统\n"
            f"\n## 记忆系统\n"
            f"你拥有智能记忆系统。每次对话开始时，系统会自动检索并展示过去交互中的相关记忆。"
            f"你也可以使用 manage_memory 工具主动管理记忆："
            f"action='add' 保存重要事实、用户偏好和学到的知识；"
            f"action='search' 搜索过去的特定记忆。\n"
            f"\n## 技能系统\n"
            f"在每次任务开始时，系统会根据任务内容自动检索并注入相关技能供你参考执行。"
            f"你也可以主动使用 manage_memory 工具查询和管理技能。"
            f"如果你成功完成了一项之前未完成过的复杂任务，并且得到了用户的正面反馈，"
            f"必须主动询问用户是否需要将过程保存为新技能。"
            f"如用户同意，请使用 save_learned_skill 工具。\n"
            f"\n## 自我审查机制\n"
            f"当任务接近最大迭代次数或你感觉陷入循环时，可以调用 self_review 工具进行自我审查。"
            f"系统会在达到迭代上限时自动提示你使用此工具。通过审查你可以获得额外的执行机会。"
            f"请诚实评估：如果确实陷入无效循环，及时报告用户比浪费计算资源更好。\n"
        )

        self.messages: List[Dict[str, Any]] = [
            {
                "role": "system",
                "content": self._build_system_prompt()
            }
        ]
        
        self.user_input_queue = queue.Queue()
        self.pending_messages: list = []  # Non-blocking input queue during execution
        self.progress_callback = None
        
        # Instantiate tools (MemoryTool shares the same store)
        memory_tool = MemoryTool(
            db_path=get_data_path("memory.db"),
            session_id=self.session_id
        )
        self.full_available_tools = {
            "execute_shell": ShellTool(),
            "read_file": ReadFileTool(),
            "write_file": WriteFileTool(),
            "edit_file": EditFileTool(),
            "search_file_content": GrepSearchTool(),
            "find_files": GlobTool(),
            "execute_python": PythonREPLTool(),
            "computer_control": ComputerTool(),
            "manage_memory": memory_tool,
            "search_web": WebSearchTool(),
            "mac_system_action": MacSystemTool(),
            "save_learned_skill": SaveSkillTool(),
            "browser_automation": BrowserAutomationTool(headless=self.browser_headless),
            "search_emails": SearchEmailTool(),
            "send_email": SendEmailTool(),
            "queue_download": DownloadTool(),
            "ask_user_question": AskUserQuestionTool(),
            "search_history": SearchHistoryTool(),
            "pause_and_wait": PauseAndWaitTool(),
            "enter_sandbox_mode": EnterWorktreeTool(),
            "exit_sandbox_mode": ExitWorktreeTool(),
            "self_review": SelfReviewTool()
        }

        # Tool display names (Chinese-friendly)
        self.tool_display_names = {
            "execute_shell": "执行终端命令",
            "read_file": "读取文件",
            "write_file": "写入文件",
            "edit_file": "局部修改文件",
            "search_file_content": "搜索文件内容",
            "find_files": "查找文件",
            "execute_python": "运行 Python 代码",
            "computer_control": "操控电脑",
            "manage_memory": "管理记忆",
            "search_web": "搜索网页",
            "mac_system_action": "系统操作",
            "save_learned_skill": "保存技能",
            "browser_automation": "虚拟浏览器控制",
            "search_emails": "搜索邮件",
            "send_email": "发送邮件",
            "queue_download": "下载文件",
            "search_available_tools": "检索扩展工具",
            "ask_user_question": "向用户提问",
            "search_history": "检索会话历史",
            "pause_and_wait": "暂停并等待后台完成",
            "enter_sandbox_mode": "进入沙箱模式",
            "exit_sandbox_mode": "退出沙箱模式",
            "self_review": "自我审查任务进度"
        }

        # Load auto-generated tools (persisted from previous sessions)
        # Store in data/auto_tools/{session_id} to isolate per session
        if self.session_id is not None:
            user_gen_dir = get_data_path(f"auto_tools/{self.session_id}")
        else:
            user_gen_dir = get_data_path("auto_tools")
        init_auto_tools(user_gen_dir)
        loaded = load_all_dynamic_tools(user_gen_dir)
        for tool_name, tool_instance in loaded.items():
            if tool_name not in self.full_available_tools:
                self.full_available_tools[tool_name] = tool_instance
                self.tool_display_names[tool_name] = tool_instance.description[:20]

        # Load MCP tools
        try:
            with open(get_data_path("config.json"), "r", encoding="utf-8") as f:
                config_data = json.load(f)
                mcp_config = config_data.get("mcp_servers", {})
                if mcp_config:
                    mcp_manager = get_mcp_manager()
                    mcp_tools = mcp_manager.load_servers(mcp_config)
                    for name, tool_instance in mcp_tools.items():
                        if name not in self.full_available_tools:
                            self.full_available_tools[name] = tool_instance
                            self.tool_display_names[name] = f"[MCP] {name}"
        except Exception as e:
            print(f"[Agent] Failed to load MCP tools: {e}")

        # Progressive Disclosure Setup
        CORE_TOOL_NAMES = {"execute_shell", "read_file", "write_file", "edit_file",
                           "search_file_content", "find_files", "search_available_tools",
                           "ask_user_question", "search_history", "queue_download", "pause_and_wait",
                           "execute_python", "search_web", "self_review"}
        self.active_tool_names = set(CORE_TOOL_NAMES) | self._pre_enabled_tools

        # Adaptive resident: auto-load frequently used non-core tools
        try:
            from tools.adaptive import get_adaptive_tools
            from core.paths import get_data_path as _get_data_path
            adapt_dir = os.path.dirname(_get_data_path("config.json"))
            adapt_tools = get_adaptive_tools(adapt_dir,
                                             set(self.full_available_tools.keys()),
                                             CORE_TOOL_NAMES)
            for name in adapt_tools:
                if name in self.full_available_tools:
                    self.active_tool_names.add(name)
        except Exception as e:
            print(f"[Agent] Adaptive resident init error: {e}")

        def _enable_tools_callback(tool_names: List[str]):
            added = False
            for name in tool_names:
                if name in self.full_available_tools and name not in self.active_tool_names:
                    self.active_tool_names.add(name)
                    self.available_tools[name] = self.full_available_tools[name]
                    added = True
            if added:
                self.tool_schemas = [tool.get_openai_schema() for tool in self.available_tools.values()]
                
        self.full_available_tools["search_available_tools"] = ToolDiscoveryTool(
            full_tools=self.full_available_tools, 
            enable_callback=_enable_tools_callback
        )
        
        self.available_tools = {name: self.full_available_tools[name] for name in self.active_tool_names if name in self.full_available_tools}

        # Prepare OpenAI format tool schema
        self.tool_schemas = [tool.get_openai_schema() for tool in self.available_tools.values()]

        # Track which skills were injected in the current turn for feedback loop
        self._active_skills: List[str] = []

    def _build_system_prompt(self, memory_context: str = "", skill_context: str = "",
                             experience_context: str = "", kg_context: str = "") -> str:
        # Inject current date/time so the LLM knows "today"
        from datetime import datetime
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        current_date = datetime.now().strftime("%Y年%m月%d日")
        
        prompt = self.system_prompt_base.replace("{current_time}", current_time).replace("{current_date}", current_date)
        prompt = prompt.replace("{cwd_dir}", self.sandbox_dir or os.getcwd())
        prompt = prompt.replace("{system_env}", _detect_system_env())
        
        # Inject Episodic Memory Context
        if memory_context:
            prompt += f"\n--- 历史记忆回溯 (Episodic Memory) ---\n{memory_context}\n"
            
        # Optional: Inject MEMORY.md (Highest priority global rules)
        if self.sandbox_dir:
            memory_file_path = os.path.join(self.sandbox_dir, "MEMORY.md")
            if os.path.exists(memory_file_path):
                try:
                    with open(memory_file_path, "r", encoding="utf-8") as f:
                        content = f.read().strip()
                        if content:
                            prompt += f"\n--- 全局核心设定与事实库 (MEMORY.md) ---\n{content}\n(注意：这是最高优先级的持久化记忆。当用户想传授新规定、修改基础偏好时，请使用 write_file 覆写沙箱目录下的 MEMORY.md)\n"
                except Exception as e:
                    print(f"Failed to read MEMORY.md: {e}")

        # Inject relevant skills (progressive — only what's needed for this turn)
        if skill_context:
            prompt += f"\n{skill_context}"

        # Inject prior experience (reflections + trajectories)
        if experience_context:
            prompt += f"\n\n{experience_context}"

        # Inject knowledge graph context
        if kg_context:
            prompt += f"\n\n{kg_context}"

        return prompt

    def _auto_save_memories(self, user_input: str, assistant_reply: str):
        """
        Automatically extract and save key memories from a conversation turn.
        Runs a lightweight LLM call to determine what's worth remembering.
        """
        # Skip very short or trivial exchanges
        if len(user_input.strip()) < 10 and len(assistant_reply.strip()) < 20:
            return

        extraction_prompt = (
            "你是一个记忆提取助手。根据以下对话内容，判断是否有值得记住的信息以供未来对话使用。\n"
            "值得记住的：用户偏好、项目细节、个人事实、重要指令、过往完成的任务/创作的产出物(如写过的文章、做过的图表、历史分析)、学到的知识。\n"
            "不值得记住的：打招呼、关于通用知识的简单问答、闲聊。\n\n"
            f"用户：{user_input[:500]}\n\n"
            f"助手：{assistant_reply[:500]}\n\n"
            "如果有值得保存的记忆，请用中文回复一个 JSON 数组，每个对象包含：\n"
            "- 'content'：记忆内容（简洁的中文描述）\n"
            "- 'category'：类别（可选值：tech, user_pref, project, knowledge, system, general）\n"
            "- 'memory_type'：记忆类型（可选值：core=长期核心事实如姓名/偏好，"
            "working=近期工作记忆如当前任务，episode=事件记录如学到的知识）\n"
            "如果没有值得保存的内容，回复空数组 []。\n"
            "只回复 JSON，不要有其他文字。"
        )

        try:
            # Use the same model the user has configured (with fallback support)
            response, _ = self.llm.chat(
                messages=[{"role": "user", "content": extraction_prompt}]
            )
            result_text = response.choices[0].message.content.strip()

            # Strip markdown code fences if present
            if result_text.startswith("```"):
                lines = result_text.split("\n")
                result_text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

            memories = json.loads(result_text)
            if isinstance(memories, list):
                for mem in memories:
                    if isinstance(mem, dict) and mem.get("content"):
                        content = mem["content"]
                        category = mem.get("category")
                        memory_type = mem.get("memory_type", "episode")
                        importance = mem.get("importance", 2)

                        # Smart dedup: check for similar existing memory
                        similar = self.memory_store.find_similar(content)
                        if similar:
                            # Merge: update existing memory with richer content
                            merged = f"{similar['content']}；{content}" \
                                if content not in similar['content'] else similar['content']
                            self.memory_store.update_memory(similar["id"], merged)
                        else:
                            self.memory_store.add_memory_vector(
                                content=content,
                                category=category,
                                importance=importance,
                                memory_type=memory_type
                            )

            # Also save a conversation summary
            summary = user_input[:200]
            self.memory_store.save_conversation(
                summary=summary,
                messages=[{"role": "user", "content": user_input},
                          {"role": "assistant", "content": assistant_reply[:500]}]
            )
        except Exception as e:
            print(f"[Agent] Auto-save memories error: {e}")

    @staticmethod
    def _text_keywords(text: str) -> set:
        """Extract meaningful keywords from text for Chinese/English overlap check."""
        import re
        text = text.lower()
        words = set()
        # English words
        for w in text.split():
            w = w.strip('.,;:!?，。；：！？""''、')
            if w and not all(c in ' \t\n\r' for c in w):
                words.add(w)
        # Chinese bigrams (相邻两个字的组合)
        chars = re.findall(r'[一-鿿]', text)
        for i in range(len(chars) - 1):
            words.add(chars[i] + chars[i + 1])
        # Also add single Chinese characters (excluding stopwords)
        stop_chars = set('的了是在有和不就都而及与或个这那他也她它我对')
        for ch in chars:
            if ch not in stop_chars:
                words.add(ch)
        return words

    def _check_pending_messages(self, current_query: str = "") -> str:
        """Poll pending message queue. Returns injected message or empty string."""
        if not self.pending_messages:
            return ""
        msg = self.pending_messages.pop(0)
        if current_query:
            cur_words = self._text_keywords(current_query)
            new_words = self._text_keywords(msg)
            if cur_words and new_words:
                overlap = len(cur_words & new_words) / max(len(cur_words), len(new_words))
                # Chinese text with bigram matching typically gets 0.05-0.2 overlap
                # Even 5% overlap suggests they're related topics
                if overlap > 0.05:
                    return f"[用户追加指令] {msg}"
        # If no current_query to compare against, always accept
        # If word overlap was too low, still accept (don't drop user messages)
        return f"[用户追加指令] {msg}"

    def queue_message(self, text: str):
        """Add a message to the pending queue (non-blocking input)."""
        self.pending_messages.append(text)

    def _handle_sandbox_blocked(self, sb, tool_name, tool_args, progress_callback):
        """Pause agent loop and wait for user to approve/deny sandbox path access."""
        import threading
        import json as _json
        from core.paths import get_data_path

        # Build request
        is_network = (sb.sandbox_dir == "network")
        is_permission = (sb.sandbox_dir == "permission")
        if is_permission:
            block_type = "permission"
            desc_text = sb.description or "敏感操作"
            category_text = sb.category or "unknown"
        else:
            block_type = "network" if is_network else "path"
            desc_text = ""
            category_text = ""
        if progress_callback:
            progress_callback({
                "event": "sandbox_blocked",
                "path": sb.path,
                "tool_name": tool_name,
                "session_id": self.session_id,
                "block_type": block_type,
                "description": desc_text,
                "category": category_text,
            })

        # Wait for user response
        wait_event = threading.Event()
        result_holder = {"action": "timeout"}
        try:
            from api.server import _sandbox_waits
        except Exception as e:
            print(f"[Agent] Failed to import _sandbox_waits: {e}")
            return f"Sandbox authorization failed (internal error): {sb.path}"
        _sandbox_waits[self.session_id] = {"event": wait_event, "result": result_holder}
        print(f"[Agent] Sandbox blocked: {sb.path} — waiting for user response...")
        responded = wait_event.wait(timeout=120)

        if not responded:
            print(f"[Agent] Sandbox wait timeout for {sb.path}")
            _sandbox_waits.pop(self.session_id, None)
            from tools.interaction import TaskPaused
            raise TaskPaused(f"等待权限授权超时，转入后台挂起状态，请确认权限后恢复执行。路径: {sb.path}")

        action = result_holder.get("action", "deny_once")
        try:
            _sandbox_waits.pop(self.session_id, None)
        except Exception:
            pass

        if is_network:
            # Network domain authorization
            from urllib.parse import urlparse
            domain = urlparse(sb.path).hostname or sb.path
            if action in ("approve_dir", "approve_always"):
                try:
                    config_path = get_data_path("config.json")
                    config = {}
                    if os.path.exists(config_path):
                        with open(config_path, "r", encoding="utf-8") as f:
                            config = _json.load(f)
                    perms = config.get("tool_permissions", {})
                    if isinstance(perms, str):
                        perms = _json.loads(perms)
                    net = perms.get("network", {})
                    net[domain] = "allow"
                    perms["network"] = net
                    config["tool_permissions"] = perms
                    with open(config_path, "w", encoding="utf-8") as f:
                        _json.dump(config, f, ensure_ascii=False, indent=2)
                    print(f"[Agent] Network approved (always): {domain}")
                except Exception as e:
                    print(f"[Agent] Network persist error: {e}")
                return None  # Retry
            elif action == "approve_once":
                if not hasattr(self, '_session_network_whitelist'):
                    self._session_network_whitelist = set()
                self._session_network_whitelist.add(domain)
                print(f"[Agent] Network approved (once): {domain}")
                return None  # Retry
            elif action == "deny_always":
                try:
                    config_path = get_data_path("config.json")
                    config = {}
                    if os.path.exists(config_path):
                        with open(config_path, "r", encoding="utf-8") as f:
                            config = _json.load(f)
                    perms = config.get("tool_permissions", {})
                    net = perms.get("network", {})
                    net[domain] = "permanent_deny"
                    perms["network"] = net
                    config["tool_permissions"] = perms
                    with open(config_path, "w", encoding="utf-8") as f:
                        _json.dump(config, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass
            return f"Network access denied by user: {sb.path}"

        # ── Permission (sensitive command) authorization ──
        is_permission = (sb.sandbox_dir == "permission")
        if is_permission:
            category = sb.category or "unknown"
            desc = sb.description or "敏感操作"
            if action == "deny_always":
                try:
                    config_path = get_data_path("config.json")
                    config = {}
                    if os.path.exists(config_path):
                        with open(config_path, "r", encoding="utf-8") as f:
                            config = _json.load(f)
                    perms = config.get("tool_permissions", {})
                    if isinstance(perms, str):
                        perms = _json.loads(perms)
                    if category not in perms:
                        perms[category] = {}
                    perms[category]["permanent_deny"] = "deny"
                    config["tool_permissions"] = perms
                    with open(config_path, "w", encoding="utf-8") as f:
                        _json.dump(config, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass
                return f"Operation denied by user: {desc} (permanent)"
            elif action == "deny_once":
                return f"Operation denied by user: {desc}"
            elif action in ("approve_once", "approve_session"):
                self._session_permission_whitelist.add(category)
                print(f"[Agent] Permission approved (session): {category}")
                return None  # Retry
            elif action == "approve_always":
                try:
                    config_path = get_data_path("config.json")
                    config = {}
                    if os.path.exists(config_path):
                        with open(config_path, "r", encoding="utf-8") as f:
                            config = _json.load(f)
                    perms = config.get("tool_permissions", {})
                    if isinstance(perms, str):
                        perms = _json.loads(perms)
                    if category not in perms:
                        perms[category] = {}
                    perms[category]["allow"] = "allow"
                    config["tool_permissions"] = perms
                    with open(config_path, "w", encoding="utf-8") as f:
                        _json.dump(config, f, ensure_ascii=False, indent=2)
                    print(f"[Agent] Permission approved (always): {category}")
                except Exception as e:
                    print(f"[Agent] Permission persist error: {e}")
                return None  # Retry
            return f"Operation denied by user: {desc}"

        if action == "approve_dir":
            dirpath = os.path.dirname(os.path.abspath(sb.path))
            self._session_sandbox_whitelist.add(dirpath)
            # Persist the directory to config
            try:
                config_path = get_data_path("config.json")
                config = {}
                if os.path.exists(config_path):
                    with open(config_path, "r", encoding="utf-8") as f:
                        config = _json.load(f)
                allowed = config.get("allowed_paths", [])
                if isinstance(allowed, str):
                    allowed = _json.loads(allowed)
                if dirpath not in allowed:
                    allowed.append(dirpath)
                config["allowed_paths"] = allowed
                with open(config_path, "w", encoding="utf-8") as f:
                    _json.dump(config, f, ensure_ascii=False, indent=2)
                print(f"[Agent] Sandbox approved (dir): {dirpath}")
            except Exception as e:
                print(f"[Agent] Failed to persist allowed_path: {e}")
            return None  # Signal to retry
        elif action == "approve_once":
            # Add parent directory to whitelist so other files in same dir work too
            dirpath = os.path.dirname(os.path.abspath(sb.path))
            self._session_sandbox_whitelist.add(dirpath)
            self._session_sandbox_whitelist.add(sb.path)
            print(f"[Agent] Sandbox approved (once): {sb.path} (dir: {dirpath})")
            return None  # Signal to retry
        elif action == "approve_always":
            # Store the directory for permanent multi-file access
            dirpath = os.path.dirname(os.path.abspath(sb.path))
            self._session_sandbox_whitelist.add(dirpath)
            # Persist directory to config for future sessions
            try:
                config_path = get_data_path("config.json")
                config = {}
                if os.path.exists(config_path):
                    with open(config_path, "r", encoding="utf-8") as f:
                        config = _json.load(f)
                allowed = config.get("allowed_paths", [])
                if isinstance(allowed, str):
                    allowed = _json.loads(allowed)
                if dirpath not in allowed:
                    allowed.append(dirpath)
                config["allowed_paths"] = allowed
                with open(config_path, "w", encoding="utf-8") as f:
                    _json.dump(config, f, ensure_ascii=False, indent=2)
                print(f"[Agent] Sandbox approved (always): {sb.path}")
            except Exception as e:
                print(f"[Agent] Failed to persist allowed_path: {e}")
            return None  # Signal to retry
        elif action == "deny_always":
            # Add to denied_paths
            try:
                config_path = get_data_path("config.json")
                config = {}
                if os.path.exists(config_path):
                    with open(config_path, "r", encoding="utf-8") as f:
                        config = _json.load(f)
                denied = config.get("denied_paths", [])
                if isinstance(denied, str):
                    denied = _json.loads(denied)
                if sb.path not in denied:
                    denied.append(sb.path)
                config["denied_paths"] = denied
                with open(config_path, "w", encoding="utf-8") as f:
                    _json.dump(config, f, ensure_ascii=False, indent=2)
                print(f"[Agent] Sandbox denied (always): {sb.path}")
            except Exception as e:
                print(f"[Agent] Failed to persist denied_path: {e}")
        # deny_once or deny_always or timeout → return error
        return f"Sandbox access denied by user: {sb.path}"

    def _record_skill_feedback(self, success: bool, task_input: str = "",
                                duration: float = 0):
        """Update skill usage stats and generate reflection based on task outcome."""
        if self._active_skills:
            for filename in self._active_skills:
                try:
                    self.skill_store.record_usage(filename, success=success)
                except Exception as e:
                    print(f"[Agent] Skill feedback error for {filename}: {e}")
        # Generate reflection in background thread
        if task_input and self.reflection_engine:
            try:
                self.reflection_engine.generate_reflection(
                    task_input=task_input,
                    messages=self.messages,
                    success=success,
                    duration_seconds=duration,
                )
            except Exception as e:
                print(f"[Agent] Reflection error: {e}")

    # Task categories for adaptive config
    TASK_CATEGORIES = {
        "code": {
            "keywords": ["写代码", "编程", "实现", "开发", "python", "javascript",
                         "create", "implement", "coding", "programming"],
            "config": {"max_iterations": 40, "temperature": 0.1}
        },
        "deploy": {
            "keywords": ["部署", "上线", "发布", "deploy", "release", "publish",
                         "启动服务", "安装"],
            "config": {"max_iterations": 50, "temperature": 0.2}
        },
        "analysis": {
            "keywords": ["分析", "检查", "审查", "review", "analyze", "audit",
                         "统计", "报告"],
            "config": {"max_iterations": 20, "temperature": 0.3}
        },
        "research": {
            "keywords": ["搜索", "查找", "研究", "调查", "search", "research",
                         "find", "what is", "how to"],
            "config": {"max_iterations": 30, "temperature": 0.5}
        },
        "creative": {
            "keywords": ["写文章", "设计", "创作", "write", "design", "create content",
                         "生成图片"],
            "config": {"max_iterations": 20, "temperature": 0.7}
        },
        "filesystem": {
            "keywords": ["整理文件", "重命名", "移动", "复制", "organize", "rename",
                         "move", "copy", "clean"],
            "config": {"max_iterations": 15, "temperature": 0.1}
        },
    }

    def _classify_task(self, user_input: str) -> dict:
        """Classify user input into a task category and return adaptive config
        with runtime stats tuning."""
        text = user_input.lower()
        matched_category = None
        for category, rules in self.TASK_CATEGORIES.items():
            if any(kw in text for kw in rules["keywords"]):
                matched_category = category
                break

        base_config = (self.TASK_CATEGORIES[matched_category]["config"]
                       if matched_category
                       else {"max_iterations": 30, "temperature": 0.3})

        # Apply runtime stats tuning if available
        if matched_category:
            stats = self._load_task_stats().get(matched_category, {})
            sample_count = stats.get("count", 0)
            if sample_count >= 5:
                avg_iters = stats.get("avg_iterations", base_config["max_iterations"])
                success_rate = stats.get("success_rate", 1.0)
                # Use the higher of (average * 1.3) or minimum 8
                tuned = max(int(avg_iters * 1.3), 8)
                # Allow growth beyond default, but ensure it's at least the default
                tuned = max(tuned, base_config["max_iterations"])
                base_config = {**base_config, "max_iterations": tuned}
                if success_rate < 0.5 and sample_count >= 3:
                    base_config["max_iterations"] = max(base_config["max_iterations"], 15)

        return base_config

    def _load_task_stats(self) -> dict:
        try:
            import json as _j
            from core.paths import get_data_path
            path = get_data_path("task_stats.json")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return _j.load(f)
        except Exception:
            pass
        return {}

    def _save_task_stats(self, category: str, iterations: int, success: bool):
        try:
            import json as _j
            from core.paths import get_data_path
            path = get_data_path("task_stats.json")
            stats = self._load_task_stats()
            entry = stats.get(category, {"count": 0, "total_iterations": 0,
                                           "successes": 0, "avg_iterations": 0,
                                           "success_rate": 1.0})
            entry["count"] += 1
            entry["total_iterations"] += iterations
            entry["avg_iterations"] = entry["total_iterations"] / entry["count"]
            if success:
                entry["successes"] += 1
            entry["success_rate"] = entry["successes"] / entry["count"]
            stats[category] = entry
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                _j.dump(stats, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Agent] Task stats save error: {e}")

    def _register_dynamic_tool(self, tool_name: str, tool_instance) -> bool:
        """Register a dynamically created tool so the LLM can call it."""
        if tool_name in self.available_tools:
            return False
        self.available_tools[tool_name] = tool_instance
        self.tool_display_names[tool_name] = tool_instance.description[:20]
        self.tool_schemas = [t.get_openai_schema() for t in self.available_tools.values()]
        return True

    def _auto_generate_tool(self, task_input: str, trajectory, llm_client) -> Optional[str]:
        """Try to generate a reusable tool from a successful trajectory."""
        tool_sequence = trajectory.get("tool_sequence", "")
        tool_count = tool_sequence.count("\n→ ")
        if tool_count < 5:
            return None

        code = generate_tool_code(task_input, tool_sequence,
                                   "Success", llm_client)
        if not code or not validate_tool_code(code):
            return None

        # Extract name from TOOL_SCHEMA
        import ast
        try:
            tree = ast.parse(code)
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "TOOL_SCHEMA":
                            if isinstance(node.value, ast.Dict):
                                for k, v in zip(node.value.keys, node.value.values):
                                    if isinstance(k, ast.Str) and k.s == "name" and isinstance(v, ast.Str):
                                        tool_name = v.s
                                        break
        except Exception:
            return None

        if not tool_name:
            return None

        filepath = save_tool_code(code, tool_name)
        if not filepath:
            return None

        from tools.auto_tool import load_dynamic_tool
        tool_instance = load_dynamic_tool(filepath)
        if not tool_instance:
            return None

        self._register_dynamic_tool(tool_name, tool_instance)
        return tool_name

    def _should_delegate(self, user_input: str) -> bool:
        """Assess whether a task is complex enough to warrant sub-agent delegation."""
        text = user_input.lower()
        # Check for decomposition keywords
        complexity_keywords = [
            "分别", "同时", "多个", "所有", "each", "all", "every",
            "first.*then", "先.*再", "先.*然后",
            "部署", "deploy", "migrate", "迁移",
            "深度研究", "全面分析", "架构梳理", "长期任务"
        ]
        match_count = 0
        for kw in complexity_keywords:
            if re.search(kw, text):
                match_count += 1

        # Check if it spans multiple areas
        area_count = 0
        for area_kws in TOOL_SETS.values():
            if any(kw in text for kw in area_kws):
                area_count += 1

        # Delegate if high complexity or multi-domain
        # Note: Do not use len(text) > 200 because users often paste long error logs for simple one-shot fixes.
        return match_count >= 2 or area_count >= 3

    def _decompose_task(self, task_input: str) -> List[Dict]:
        """Use LLM to decompose a complex task into sub-tasks."""
        prompt = f"""将以下任务分解为可执行子任务。

任务：{task_input}

要求：
- 每个子任务应独立、可完成
- 为每个子任务标注需要的工具类型（可选：filesystem, code, web, analysis, deploy, research）
- 标注子任务间的依赖关系（depends_on 为依赖的子任务 id 列表）

输出 JSON 数组，格式：
[{{"id": 1, "task": "子任务描述", "tools": ["filesystem"], "depends_on": [], "max_iterations": 10}}]

只输出 JSON 数组，不要额外说明。"""

        try:
            response, _ = self.llm.chat(
                messages=[{"role": "user", "content": prompt}]
            )
            text = response.choices[0].message.content.strip()

            # Extract JSON from markdown if present
            json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
            if json_match:
                text = json_match.group(1).strip()

            plans = json.loads(text)
            if isinstance(plans, list):
                # Resolve tool set names to actual tool names
                for plan in plans:
                    tool_names = plan.get("tools", ["filesystem"])
                    resolved = []
                    for t in tool_names:
                        resolved.extend(TOOL_SETS.get(t, [t]))
                    plan["tools"] = list(set(resolved))
                return plans
        except Exception as e:
            print(f"[Agent] Task decomposition failed: {e}")

        return []

    def _synthesize_results(self, user_input: str,
                            sub_results: List[Dict]) -> str:
        """Combine sub-agent results into a coherent structured report."""
        total_tasks = len(sub_results)
        success_count = sum(1 for r in sub_results if r.get("success"))
        fail_count = total_tasks - success_count
        total_duration = sum(r.get("duration", 0) for r in sub_results)
        total_tool_calls = sum(r.get("tool_calls", 0) for r in sub_results)

        status_emoji = "✅" if fail_count == 0 else "⚠️" if success_count > 0 else "❌"
        parts = [
            f"## {status_emoji} 子代理任务执行报告\n",
            f"**原始任务**：{user_input}\n",
            f"**执行摘要**：{total_tasks} 个子任务 "
            f"({success_count} 成功, {fail_count} 失败) · "
            f"总耗时 {total_duration:.1f}s · "
            f"工具调用 {total_tool_calls} 次\n",
            "---\n",
        ]

        for i, result in enumerate(sub_results, 1):
            status = "✅" if result.get("success") else "❌"
            summary = result.get("summary", "无输出")[:800]
            duration = result.get("duration", 0)
            tc = result.get("tool_calls", 0)
            files = result.get("output_files", [])
            steps = result.get("steps", [])
            parts.append(
                f"### 子任务 {i} [{status}] （{duration:.1f}s, {tc} 步）\n"
                f"{summary}\n"
            )
            if steps:
                step_lines = ["\n**执行步骤：**"]
                for si, step in enumerate(steps, 1):
                    s_status = "✅" if step.get("success") else "❌"
                    tool_name = step.get("tool", "?")
                    args = step.get("args", "")[:120]
                    step_lines.append(
                        f"- {s_status} `{tool_name}` {args}"
                    )
                parts.append("\n".join(step_lines) + "\n")
            if files:
                parts.append(f"📄 产出文件: {', '.join(files)}\n")

        if fail_count > 0:
            parts.append("---\n### ⚠️ 失败分析\n")
            for i, r in enumerate(sub_results, 1):
                if not r.get("success"):
                    parts.append(f"- **子任务 {i}**：{r.get('summary', '未知错误')[:300]}\n")
            parts.append("\n建议：检查失败子任务的输入数据或增加 max_iterations 后重试。\n")

        return "\n".join(parts)

    # ── Tool-specific compression strategies ──

    @staticmethod
    def _compress_search_results(result: str) -> str:
        """Compress search results: keep all entries, truncate snippets per-entry.

        Search results are structured as:
          [From Engine]
          1. Title
             URL: xxx
             Snippet: yyy
          2. Title
             ...
        """
        MAX_ENTRY_CHARS = 400  # max chars per result entry (title+url+snippet)
        MAX_TOTAL = 4000

        lines = result.split("\n")
        compressed = []
        current_entry = []

        for line in lines:
            # Detect numbered entry start: "N. Title"
            if re.match(r'^\d+\.\s', line):
                # Flush previous entry
                if current_entry:
                    entry_text = "\n".join(current_entry)
                    if len(entry_text) > MAX_ENTRY_CHARS:
                        entry_text = entry_text[:MAX_ENTRY_CHARS] + "..."
                    compressed.append(entry_text)
                current_entry = [line]
            else:
                current_entry.append(line)

        # Flush last entry
        if current_entry:
            entry_text = "\n".join(current_entry)
            if len(entry_text) > MAX_ENTRY_CHARS:
                entry_text = entry_text[:MAX_ENTRY_CHARS] + "..."
            compressed.append(entry_text)

        result_text = "\n".join(compressed)
        if len(result_text) > MAX_TOTAL:
            # Truncate from the end (lose lowest-ranked results)
            result_text = result_text[:MAX_TOTAL] + "\n...(truncated)"

        return result_text

    @staticmethod
    def _compress_file_content(result: str) -> str:
        """Compress file content: keep head + tail with omitted-line annotation."""
        HEAD_LINES = 30
        TAIL_LINES = 20
        lines = result.split("\n")
        if len(lines) <= HEAD_LINES + TAIL_LINES + 5:
            return result
        head = lines[:HEAD_LINES]
        tail = lines[-TAIL_LINES:]
        omitted = len(lines) - HEAD_LINES - TAIL_LINES
        return "\n".join(head + [f"─── {omitted} lines omitted ───"] + tail)

    @staticmethod
    def _compress_shell_output(result: str, tool_name: str) -> str:
        """Compress shell/Python output: keep head + scored middle lines + tail."""
        COMPRESS_THRESHOLD = 3000
        EXTRACTIVE_TARGET = 8000

        if len(result) <= COMPRESS_THRESHOLD:
            return result

        lines = result.split("\n")

        def _line_score(line: str) -> int:
            low = line.lower()
            score = 0
            if any(kw in low for kw in ("error", "exception", "traceback", "fail", "trace ")):
                score += 5
            if any(kw in low for kw in ("exit code", "returncode", "status", "result")):
                score += 3
            if any(kw in low for kw in ("file", "path", "dir", "found", "missing")):
                score += 2
            if any(c.isdigit() for c in line):
                score += 1
            if len(line) > 300:
                score -= 2
            return score

        head = lines[:15]
        tail = lines[-5:]
        middle = lines[15:-5] if len(lines) > 20 else []

        if not middle:
            compressed = "\n".join(head + tail)
            return (f"[Compressed: {len(result)} chars → {len(compressed)} chars | "
                    f"original tool: {tool_name}]\n{compressed}")

        scored_lines = [(i, _line_score(l), l) for i, l in enumerate(middle, start=15)]
        scored_lines.sort(key=lambda x: -x[1])

        # Keep lines scoring >= 2 (lowered from 3), ensure at least 20% of middle
        important = [(i, l) for i, s, l in scored_lines if s >= 2]
        min_keep = max(1, len(middle) // 5)
        if len(important) < min_keep:
            extra = scored_lines[:min_keep - len(important)]
            existing_ids = {idx for idx, _ in important}
            for idx, s, l in extra:
                if idx not in existing_ids:
                    important.append((idx, l))
                    existing_ids.add(idx)

        # Section markers
        section_lines = []
        existing_ids = {idx for idx, _ in important}
        for i, l in enumerate(middle):
            idx = i + 15
            if idx not in existing_ids and re.search(r'^[-|=+|]{5,}|^#{1,3}\s', l):
                section_lines.append((idx, l))
                existing_ids.add(idx)

        # Build compressed
        compressed_lines = list(head)

        if important or section_lines:
            compressed_lines.append(f"─── key output ({len(important)} important lines) ───")
            seen = set()
            for idx, l in sorted(important + section_lines):
                if l not in seen:
                    compressed_lines.append(l)
                    seen.add(l)
            omitted = len(lines) - len(head) - len(tail) - len(seen)
            if omitted > 0:
                compressed_lines.append(f"─── {omitted} lines omitted ───")

        compressed_lines.extend(tail)
        compressed = "\n".join(compressed_lines)

        if len(compressed) > EXTRACTIVE_TARGET:
            compressed = "\n".join(head + [f"─── {len(lines) - len(head) - len(tail)} lines omitted ───"] + tail)

        if len(compressed) > COMPRESS_THRESHOLD * 2:
            half = COMPRESS_THRESHOLD
            compressed = (compressed[:half] +
                          f"\n...[truncated {len(result)} chars to {half * 2}]...\n" +
                          compressed[-half:])

        return (f"[Compressed: {len(result)} chars → {len(compressed)} chars | "
                f"original tool: {tool_name}]\n{compressed}")

    def _compress_tool_result(self, result: str, tool_name: str) -> str:
        """Dispatch to tool-specific compression strategy."""
        if tool_name == "search_web":
            return self._compress_search_results(result)
        if tool_name in ("read_file", "write_file", "edit_file", "browser_automation"):
            return self._compress_file_content(result)
        return self._compress_shell_output(result, tool_name)

    def _fold_tool_calls(self, messages: List[Dict]) -> List[Dict]:
        """Fold consecutive tool-call rounds exceeding the threshold into a summary.

        Long chains of tool_call → tool_result → tool_call → tool_result consume
        context window without adding decision value.  Older rounds are replaced
        by a concise execution log.
        """
        FOLD_AFTER_N = 8       # total rounds before folding kicks in
        KEEP_LAST_N = 4        # always keep this many most-recent rounds intact

        # Identify tool-call round boundaries [(start, end), ...]
        bounds: List[tuple] = []
        i = 0
        while i < len(messages):
            m = messages[i]
            if m.get("role") == "assistant" and m.get("tool_calls"):
                start = i
                i += 1
                while i < len(messages) and messages[i].get("role") == "tool":
                    i += 1
                bounds.append((start, i))
            else:
                i += 1

        if len(bounds) <= FOLD_AFTER_N:
            return messages

        fold_bounds = bounds[:-KEEP_LAST_N]   # rounds to summarise
        keep_bounds = bounds[-KEEP_LAST_N:]   # rounds to preserve verbatim

        # Build summary lines from the rounds being folded
        summary_lines = []
        for idx, (start, end) in enumerate(fold_bounds, 1):
            msg = messages[start]
            for tc in (msg.get("tool_calls") or []):
                if not isinstance(tc, dict):
                    continue
                name = tc.get("function", {}).get("name", "?")
                args_raw = tc.get("function", {}).get("arguments", "{}")
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                except Exception:
                    args = {}
                preview = ", ".join(
                    f"{k}={str(v)[:40]}" for k, v in list(args.items())[:2]
                )
                summary_lines.append(f"  {idx}. {name}({preview})")
            # Check tool results in this round for errors
            for j in range(start + 1, end):
                content = str(messages[j].get("content", ""))
                if content.startswith("Error") or "traceback" in content.lower()[:300]:
                    if summary_lines:
                        summary_lines[-1] += " ⚠️"

        cut_idx = fold_bounds[0][0]          # first message of the oldest folded round
        keep_idx = keep_bounds[0][0]          # first message of the first kept round

        summary_text = (
            f"[以下 {len(fold_bounds)} 轮工具调用已折叠为摘要，保留最近 {len(keep_bounds)} 轮]\n"
            + "\n".join(summary_lines)
        )

        pruned = messages[:cut_idx]
        pruned.append({"role": "assistant", "content": summary_text})
        pruned.extend(messages[keep_idx:])
        return pruned

    def run_turn(self, user_input: str, verbose: bool = False,
                 progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
                 images: Optional[List[str]] = None,
                 task_id: Optional[int] = None,
                 skip_rag: bool = False) -> str:
        """
        Execute a single turn of reasoning and action.

        Args:
            skip_rag: If True, skip memory/skill/experience/knowledge-graph retrieval
                      and system prompt rebuild. Used when resuming interrupted tasks
                      where the full conversation context is already loaded in messages.
        """
        self.is_interrupted = False
        self.task_id = task_id
        self._consecutive_failures = 0
        self.progress_callback = progress_callback
        
        # Only append user message if it's not None (None means we are resuming from ask_user_question)
        if user_input is not None:
            self.messages.append(build_user_message(user_input, images))
            if self.logger:
                self.logger.log_user_query(user_input)
                
        _task_start = _time.time()

        memory_context = ""
        skill_context = ""
        experience_context = ""
        kg_context = ""

        if not skip_rag:
            # Auto-retrieve relevant memories for this query
            def _msg_text(m):
                c = m.get("content", "")
                if isinstance(c, list):
                    return " ".join(p.get("text", "") for p in c if p.get("type") == "text")
                return c

            recent_context = "\n".join([_msg_text(m) for m in self.messages[-3:] if m["role"] == "user"])
            try:
                # Dual search: semantic (ChromaDB) → FTS5 fallback
                results = self.memory_store.search_semantic(recent_context, top_k=3)
                if not results:
                    results = self.memory_store.search_memories(recent_context, top_k=3)
                if results:
                    memory_context = "\n".join([f"- {r['content']} (Type: {r['memory_type']})" for r in results])
            except Exception as e:
                if verbose: print(f"Memory retrieval error: {e}")

            # Auto-retrieve relevant skills for this query
            self._active_skills = []
            try:
                self.skill_store.refresh()
                matched_skills = self.skill_store.retrieve(recent_context, top_k=3)
                if matched_skills:
                    self._active_skills = [s["filename"] for s in matched_skills]
                    skill_context = self.skill_store.format_skills_for_prompt(matched_skills)
            except Exception as e:
                if verbose: print(f"Skill retrieval error: {e}")

            # Retrieve relevant past experience (reflections + trajectories)
            try:
                experience = self.reflection_engine.retrieve_experience(recent_context, top_k=2)
                if experience.get("reflections") or experience.get("trajectories"):
                    experience_context = self.reflection_engine.format_experience_for_prompt(experience)
            except Exception as e:
                if verbose: print(f"Experience retrieval error: {e}")

            # Retrieve knowledge graph context
            try:
                kg_results = self.knowledge_graph.retrieve_context(recent_context, top_k=5)
                if kg_results:
                    kg_context = self.knowledge_graph.format_context(kg_results)
            except Exception as e:
                if verbose: print(f"Knowledge graph retrieval error: {e}")

        # Ensure System Prompt is always fresh and has the latest MEMORY.md and episodic context
        if self.messages and self.messages[0]["role"] == "system":
            self.messages[0]["content"] = self._build_system_prompt(
                memory_context=memory_context,
                skill_context=skill_context,
                experience_context=experience_context,
                kg_context=kg_context,
            )

        # Sub-agent delegation for complex tasks
        if self._should_delegate(user_input):
            if verbose: print(f"[Agent] Delegating to sub-agents...")
            plans = self._decompose_task(user_input)
            if plans:
                sub_results = []
                completed = set()
                # Execute sub-agents respecting dependency order
                remaining = list(plans)
                max_rounds = len(plans) * 2
                for _ in range(max_rounds):
                    if not remaining:
                        break
                    batch = [p for p in remaining if all(d in completed for d in p.get("depends_on", []))]
                    if not batch:
                        break  # Circular dependency or unresolvable
                    # Remove from remaining
                    for plan in batch:
                        remaining.remove(plan)

                    # Run independent sub-agents in parallel
                    import concurrent.futures
                    batch_futures = {}
                    with concurrent.futures.ThreadPoolExecutor(
                            max_workers=min(len(batch), 4)) as executor:
                        for plan in batch:
                            sub = SubAgent(
                                task=plan["task"],
                                tools=plan.get("tools", ["execute_shell"]),
                                parent_tools=self.full_available_tools,
                                max_iterations=plan.get("max_iterations", 10),
                                progress_callback=progress_callback,
                                llm_client=self.llm,
                                agent_context=self,
                                session_whitelist=self._session_sandbox_whitelist,
                                network_whitelist=self._session_network_whitelist,
                                permission_whitelist=self._session_permission_whitelist,
                                session_id=self.session_id,
                            )
                            batch_futures[executor.submit(sub.run)] = plan
                        for future in concurrent.futures.as_completed(batch_futures):
                            plan = batch_futures[future]
                            try:
                                result = future.result()
                            except Exception as e:
                                result = {"success": False, "summary": str(e)}
                            sub_results.append(result)
                            if result.get("success"):
                                completed.add(plan["id"])
                            else:
                                dep_ids = {plan["id"]}
                                remaining = [p for p in remaining
                                    if not (dep_ids & set(p.get("depends_on", [])))]
                result_text = self._synthesize_results(user_input, sub_results)
                self.messages.append({"role": "assistant", "content": result_text})

                # Let the main agent reflect on sub-agent results for a natural final response
                try:
                    reflection, _ = self.llm.chat(messages=self.messages, tools=None)
                    final = reflection.choices[0].message.content or result_text
                    self.messages.append({"role": "assistant", "content": final})
                    return final
                except Exception:
                    return result_text

        step = 1

        # Adaptive config: task-category-based tuning (overridable in config.json)
        adaptive = self._classify_task(user_input)
        max_iterations = adaptive.get("max_iterations", 30)
        config_path = get_data_path("config.json")
        try:
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    # Explicit config value always wins
                    if "max_iterations" in config:
                        max_iterations = config["max_iterations"]
                    if "max_correction_attempts" in config:
                        self._max_correction_attempts = config["max_correction_attempts"]
        except Exception:
            pass

        current_iter = 0
        step_counter = 0
        self._correction_attempts = 0
        self._should_stop = False
        self._self_review_history = []

        # Tool loop detection state
        self._recent_tool_calls: list = []
        MAX_REPEATED_TOOL_CALLS = 3
        effective_max = max_iterations + (self._max_correction_attempts if self._max_correction_attempts > 0 else 0)

        while current_iter < effective_max:
            if self.is_interrupted:
                self._record_skill_feedback(success=False, task_input=user_input,
                                            duration=_time.time() - _task_start)
                cat = self._classify_task_category(user_input)
                self._save_task_stats(cat, current_iter, False)
                return "Task interrupted by user."

            # Check for pending messages from non-blocking input
            injected = self._check_pending_messages(user_input)
            if injected:
                self.messages.append({"role": "user", "content": injected})
                if verbose:
                    print(f"[Agent] Injected pending message: {injected[:80]}")

            current_iter += 1
            if verbose:
                print(f"[Agent Loop Iteration {current_iter}/{max_iterations}] Calling LLM...")

            # Check if self-review decided to stop — inject final answer request (before review prompt)
            if self._should_stop and not self._in_self_review:
                if verbose:
                    print("[Agent] Self-review recommended stop, requesting final answer.")
                self.messages.append({
                    "role": "user",
                    "content": "根据你的自我审查结果，请立即给出最终答复告知用户当前进展。"
                })
                # Only do this once; reset flag so we don't re-inject
                self._should_stop = False

            # Max iterations reached — inject self-review prompt
            if current_iter > max_iterations and self._max_correction_attempts > 0 and not self._in_self_review:
                remaining = self._max_correction_attempts - self._correction_attempts
                if remaining > 0:
                    self._in_self_review = True
                    review_msg = (
                        f"[系统提示] 你已经达到了最大迭代次数（{max_iterations}），但你还有 {remaining} 次自我审查纠偏机会。"
                        f"请调用 self_review 工具评估当前进度，判断是否陷入循环。"
                        f"如果还有必要继续，系统会允许额外执行。"
                        f"如果确实已无进展，请回复最终答案告知用户。"
                    )
                    self.messages.append({"role": "user", "content": review_msg})
                    if verbose:
                        print(f"[Agent] ⚠️ Max iterations reached, injected self-review prompt (attempt {self._correction_attempts + 1}/{self._max_correction_attempts})")

            # Notify: thinking
            if progress_callback:
                progress_callback({"event": "thinking", "iteration": current_iter})
            
            response, actual_model = self.llm.chat(messages=self.messages, tools=self.tool_schemas)
            message = response.choices[0].message
            
            # Update logger with the actual model used for this turn
            if self.logger:
                self.logger.model = actual_model
            
            # Record Token Usage
            usage = getattr(response, 'usage', None)
            if usage:
                prompt_tokens = getattr(usage, 'prompt_tokens', 0)
                completion_tokens = getattr(usage, 'completion_tokens', 0)
                
                # Smart Provider Detection
                model_lower = actual_model.lower()
                if 'deepseek' in model_lower: provider = 'deepseek'
                elif 'gpt' in model_lower or 'openai' in model_lower: provider = 'openai'
                elif 'claude' in model_lower or 'anthropic' in model_lower: provider = 'anthropic'
                elif 'gemini' in model_lower or 'google' in model_lower: provider = 'gemini'
                elif 'kimi' in model_lower or 'moonshot' in model_lower: provider = 'kimi'
                elif 'glm' in model_lower or 'zhipu' in model_lower or 'zai' in model_lower: provider = 'glm'
                elif 'qwen' in model_lower or 'alibaba' in model_lower: provider = 'qwen'
                elif 'llama' in model_lower or 'meta' in model_lower: provider = 'llama'
                elif '/' in actual_model:
                    provider = actual_model.split('/')[0]
                else:
                    provider = 'unknown'
                
                get_stats_manager().record_usage(
                    provider=provider,
                    model=actual_model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    session_id=self.session_id,
                    task_id=self.task_id
                )
                if progress_callback:
                    progress_callback({
                        "event": "usage",
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens
                    })
            
            # Detect empty response (no content and no tool calls)
            # This can happen with some models like Ollama's Qwen when they malfunction or refuse to answer.
            if not message.content and not message.tool_calls:
                error_msg = f"[Agent] Model {actual_model} returned an empty response (no content and no tool calls). Breaking loop."
                if verbose:
                    print(error_msg)
                # Save as a system message to history to explain why it stopped
                self.messages.append({"role": "assistant", "content": "Error: Empty response from model. Please check the model logs or try a different model."})
                return "Agent stopped: Received an empty response from the model. This usually indicates a model failure or refusal."
            
            # Notify if model was switched
            if progress_callback:
                if actual_model != self.llm.default_model:
                    progress_callback({
                        "event": "model_switched",
                        "from": self.llm.default_model,
                        "to": actual_model
                    })
                
                # Check for reasoning_content (Thinking process)
                reasoning = getattr(message, 'reasoning_content', None)
                if reasoning:
                    progress_callback({
                        "event": "thinking",
                        "iteration": current_iter,
                        "content": reasoning
                    })
            
            # Append model's response to history
            message_dict = message.model_dump()
            self.messages.append(message_dict)
            _tool_call_insertion_idx = len(self.messages) - 1  # track for cleanup on early return
            
            # 1. Check if model decided to use tools
            tool_calls = message.tool_calls
            
            # --- Tool Call Rescue Logic ---
            # If the model failed to use the API correctly and returned raw JSON text instead
            if not tool_calls and message.content:
                try:
                    # Look for JSON block wrapped in markdown or raw JSON
                    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', message.content, re.DOTALL)
                    json_str = match.group(1) if match else message.content.strip()
                    
                    if json_str.startswith('{') and json_str.endswith('}'):
                        parsed = json.loads(json_str)
                        # Check if it looks like a tool call signature
                        if "name" in parsed and "arguments" in parsed:
                            import uuid
                            
                            class MockFunction:
                                def __init__(self, name, arguments):
                                    self.name = name
                                    # LiteLLM expects arguments as stringified JSON
                                    self.arguments = json.dumps(arguments) if isinstance(arguments, dict) else str(arguments)
                            
                            class MockToolCall:
                                def __init__(self, id, function):
                                    self.id = id
                                    self.function = function
                                    self.type = "function"
                                    
                            call_id = f"call_{uuid.uuid4().hex[:10]}"
                            mock_call = MockToolCall(id=call_id, function=MockFunction(parsed["name"], parsed["arguments"]))
                            tool_calls = [mock_call]
                            message.tool_calls = tool_calls  # Patch the original message so history is consistent
                            if verbose:
                                print(f"[Agent] 🚨 Rescued raw JSON into tool call: {parsed['name']}")
                except Exception as e:
                    pass
            # ------------------------------
            if tool_calls:
                screenshot_urls = []
                for tool_call in tool_calls:
                    function_name = tool_call.function.name
                    function_args = json.loads(tool_call.function.arguments)
                    
                    step_counter += 1
                    
                    # Create a short preview of args for the UI
                    args_preview = ""
                    if "command" in function_args:
                        args_preview = function_args["command"][:80]
                    elif "path" in function_args:
                        args_preview = function_args["path"][:80]
                    elif "code" in function_args:
                        args_preview = function_args["code"][:80]
                    elif "query" in function_args:
                        args_preview = function_args["query"][:80]
                    
                    tool_label = self.tool_display_names.get(function_name, function_name)
                    
                    # Notify: tool starting
                    if progress_callback:
                        progress_callback({
                            "event": "tool_start",
                            "step": step_counter,
                            "tool": function_name,
                            "tool_label": tool_label,
                            "args_preview": args_preview,
                            "tool_call_id": tool_call.id,
                            "tool_args": json.dumps(function_args, ensure_ascii=False)
                        })
                    
                    if verbose:
                        print(f"\n[Tool Execution] {function_name}({function_args})")
                    
                    tool_instance = self.available_tools.get(function_name)
                    
                    # Tool Loop Detection Check
                    call_signature = f"{function_name}:{function_args}"
                    call_hash = hashlib.md5(call_signature.encode('utf-8')).hexdigest()
                    self._recent_tool_calls.append(call_hash)

                    # Keep only the last 10 calls in the memory window
                    if len(self._recent_tool_calls) > 10:
                        self._recent_tool_calls.pop(0)

                    # Check if the exact same tool with the exact same args was called too many times recently
                    # This often happens when the agent gets stuck in an error loop
                    loop_count = self._recent_tool_calls.count(call_hash)
                    
                    if loop_count >= MAX_REPEATED_TOOL_CALLS:
                        result = (f"System Guard: Blocked due to critical loop. "
                                  f"You have called `{function_name}` with these exact arguments {loop_count} times recently. "
                                  f"You are likely stuck in a loop. YOU MUST change your approach or use different parameters.")
                        if verbose:
                            print(f"[Tool Loop Detected] Blocked {function_name}")
                    else:
                        if tool_instance:
                            from tools.base import SandboxBlocked
                            tool_success = True
                            attempt = 0
                            while True:
                                attempt += 1
                                try:
                                    import inspect
                                    sig = inspect.signature(tool_instance.execute)
                                    extra_kwargs = {
                                        "_session_whitelist": self._session_sandbox_whitelist,
                                        "_progress_cb": progress_callback,
                                        "_task_id": self.task_id,
                                        "_network_whitelist": self._session_network_whitelist,
                                        "_permission_whitelist": self._session_permission_whitelist,
                                        "_session_id": self.session_id,
                                    }
                                    if 'interrupt_check' in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                                        result = tool_instance.execute(
                                            interrupt_check=lambda: self.is_interrupted,
                                            _agent_context=self,
                                            **extra_kwargs,
                                            **function_args
                                        )
                                    else:
                                        result = tool_instance.execute(**function_args, **extra_kwargs)
                                    # Auto-background: shell returning [Still Running] means process
                                    # is still running after timeout — system takes over automatically
                                    # instead of relying on the LLM to call pause_and_wait
                                    # Exception: if [SERVER_PROCESS] detected, don't background — let agent see
                                    is_server = "[SERVER_PROCESS]" in result if isinstance(result, str) else False
                                    if (function_name == "execute_shell"
                                            and isinstance(result, str)
                                            and result.startswith("[Still Running]")
                                            and not is_server):
                                        if progress_callback:
                                            progress_callback({
                                                "event": "task_backgrounded",
                                                "reason": "命令超时仍在运行，自动进入后台",
                                            })
                                        # Remove orphaned tool_calls message so API doesn't reject on next call
                                        del self.messages[_tool_call_insertion_idx:]
                                        return f"[TASK_BACKGROUNDED] 命令仍在后台运行，自动转入后台。进程继续执行，完成后将自动恢复。"
                                    break  # Success — exit retry loop
                                except TaskPaused as tp:
                                    # Agent voluntarily paused for background task
                                    if progress_callback:
                                        progress_callback({
                                            "event": "task_backgrounded",
                                            "reason": str(tp),
                                            "pid": tp.pid,
                                            "output_file": tp.output_file,
                                        })
                                    # Remove orphaned tool_calls message so API doesn't reject on next call
                                    del self.messages[_tool_call_insertion_idx:]
                                    return f"[TASK_BACKGROUNDED] {tp}"
                                except SandboxBlocked as sb:
                                    if attempt > 2:
                                        result = f"Sandbox blocked: {sb.path} (max retries exceeded)"
                                        tool_success = False
                                        break
                                    result = self._handle_sandbox_blocked(
                                        sb, function_name, function_args, progress_callback)
                                    if result is not None:
                                        # User denied or timeout — return error
                                        tool_success = False
                                        break
                                    # User approved — retry the tool call
                                    continue
                                except Exception as e:
                                    result = f"Error executing tool: {str(e)}"
                                    tool_success = False
                                    break
                        else:
                            result = f"Error: Tool {function_name} not found."
                            tool_success = False

                    if self.logger:
                        self.logger.log_tool_call(function_name, function_args)
                        self.logger.log_tool_result(function_name, str(result), tool_success)

                    # Track adaptive tool usage + auto-tool graduation
                    tool_obj = self.full_available_tools.get(function_name)
                    is_dynamic = tool_obj and hasattr(tool_obj, 'fn')
                    tool_type = "auto_tool" if is_dynamic else "builtin"
                    try:
                        from tools.adaptive import record_tool_call
                        from core.paths import get_data_path as _gdp
                        _adapt_dir = os.path.dirname(_gdp("config.json"))
                        record_tool_call(_adapt_dir, function_name,
                                         self.session_id or 0, tool_success,
                                         tool_type=tool_type)
                    except Exception:
                        pass
                    if is_dynamic:
                        try:
                            from tools.auto_tool import check_graduation, graduate_tool
                            _tools_dir = _gdp(f"auto_tools/{self.session_id or '1'}")
                            if check_graduation(_tools_dir, function_name):
                                print(f"[Agent] Auto-tool {function_name} ready for graduation!")
                                if graduate_tool(_tools_dir, function_name):
                                    self.skill_store.refresh()
                        except Exception:
                            pass

                    result_str = str(result)

                    # Context Compaction: compress long tool results to preserve context window
                    # result_str = self._compress_tool_result(result_str, function_name)

                    # Notify: tool done
                    if progress_callback:
                        # Longer preview for shell output so it survives page refresh
                        preview_limit = 500 if function_name == "execute_shell" else 120
                        preview = result_str[:preview_limit]
                        if len(result_str) > preview_limit:
                            preview += "..."
                        evt = {
                            "event": "tool_done",
                            "step": step_counter,
                            "tool": function_name,
                            "tool_label": tool_label,
                            "tool_call_id": tool_call.id,
                            "result_preview": preview,
                            "success": not result_str.startswith("Error") and not result_str.startswith("System Guard"),
                        }
                        # Send full result for all tools (persisted to task_steps for resume)
                        _full_cap = {
                            "execute_shell": 5000,
                            "execute_python": 5000,
                            "search_web": 3000,
                            "read_file": 3000,
                            "write_file": 2000,
                            "edit_file": 2000,
                            "browser_automation": 2000,
                        }.get(function_name, 1000)
                        if len(result_str) > _full_cap:
                            evt["full_result"] = result_str[:_full_cap]
                        progress_callback(evt)
                    
                    if verbose:
                        print(f"[Tool Result]\n{result_str}\n")
                        
                    # Append tool result to messages
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": function_name,
                        "content": result_str
                    })

                    # Handle self_review results
                    if function_name == "self_review":
                        self._in_self_review = False
                        self._correction_attempts += 1  # Count only actual self-review calls
                        self._self_review_history.append(result_str)
                        # Parse JSON from result to extract continue_processing
                        try:
                            import re as _re
                            json_match = _re.search(r'原始 JSON：(\{.*\})', result_str, _re.DOTALL)
                            if json_match:
                                review_data = json.loads(json_match.group(1))
                                if not review_data.get("continue_processing", True):
                                    self._should_stop = True
                                    if verbose:
                                        print("[Agent] Self-review recommended stop, will exit after this iteration.")
                        except Exception:
                            pass
                        # Reset consecutive failures counter after self-review
                        self._consecutive_failures = 0

                    # Context preservation: detect consecutive failures and remind model of original task
                    is_failure = not tool_success or result_str.startswith("Error") or result_str.startswith("System Guard") or result_str.startswith("Sandbox")
                    if is_failure:
                        self._consecutive_failures += 1
                    else:
                        self._consecutive_failures = 0

                    if self._consecutive_failures == 3 and current_iter < max_iterations:
                        reminder = (
                            f"[系统提醒] 你已连续 {self._consecutive_failures} 次工具调用失败。"
                            f"请暂停当前操作，回顾原始用户需求：「{user_input}」。"
                            f"如果当前方法不可行，请尝试完全不同的策略，或向用户报告当前进展并询问下一步指令。"
                            f"不要无意义地重复搜索或浏览——如果目标网站无法访问，直接告知用户。"
                        )
                        self.messages.append({"role": "user", "content": reminder})
                        if verbose:
                            print(f"[Agent] ⚠️ Context preservation: injected reminder after {self._consecutive_failures} consecutive failures")

                    # Collect screenshot data for vision injection
                    url = extract_screenshot_data(result_str)
                    if url:
                        screenshot_urls.append(url)

                # After all tool results in this iteration, inject screenshot vision observations
                for url in screenshot_urls:
                    self.messages.append({
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "[工具执行截图 — 请根据此截图内容继续后续操作]"},
                            {"type": "image_url", "image_url": {"url": url}}
                        ]
                    })

                # Context budget: prune messages if approaching token limit
                try:
                    pruned = self.token_budget.prune_messages(self.messages)
                    if len(pruned) < len(self.messages):
                        pruned_count = len(self.messages) - len(pruned)
                        if verbose:
                            print(f"[Agent] Budget pruned {pruned_count} messages")
                        self.messages = pruned
                except Exception as e:
                    if verbose:
                        print(f"[Agent] Budget pruning error: {e}")

                # Fold excessive tool call rounds into a summary
                try:
                    folded = self._fold_tool_calls(self.messages)
                    if len(folded) < len(self.messages):
                        folded_count = len(self.messages) - len(folded)
                        if verbose:
                            print(f"[Agent] Folded {folded_count} tool-call messages")
                        self.messages = folded
                except Exception as e:
                    if verbose:
                        print(f"[Agent] Tool folding error: {e}")

                # After appending all tool results, the loop continues to send them back to LLM
                continue
                
            # 2. Check if model provided a text response (final answer)
            if message.content:
                # If self-review prompt was injected but LLM skipped the tool call,
                # reset and retry — don't exit the loop prematurely
                if self._in_self_review:
                    self._in_self_review = False
                    if verbose:
                        print("[Agent] LLM skipped self_review tool call, retrying review cycle")
                    continue

                final_answer = message.content
                if self.logger:
                    self.logger.log_agent_response(final_answer)
                # Auto-extract & save memories in background thread
                thread = threading.Thread(
                    target=self._auto_save_memories,
                    args=(user_input, final_answer),
                    daemon=True
                )
                thread.start()
                # Extract knowledge graph entities from this turn's messages
                try:
                    self.knowledge_graph.extract_from_messages(self.messages)
                except Exception as e:
                    print(f"[Agent] KG extraction error: {e}")
                self._record_skill_feedback(success=True, task_input=user_input,
                                            duration=_time.time() - _task_start)
                # Save runtime stats for adaptive tuning
                cat = self._classify_task_category(user_input)
                self._save_task_stats(cat, current_iter, True)
                # Auto-generate tool from successful complex trajectory
                try:
                    tool_seq = self.reflection_engine._extract_tool_sequence(self.messages)
                    traj = {"tool_sequence": tool_seq}
                    tool_name = self._auto_generate_tool(user_input, traj, self.llm)
                    if tool_name:
                        print(f"[Agent] Auto-generated tool: {tool_name}")
                except Exception as e:
                    print(f"[Agent] Auto-tool generation error: {e}")
                return final_answer

        # Extract knowledge graph entities even on failure
        try:
            self.knowledge_graph.extract_from_messages(self.messages)
        except Exception as e:
            print(f"[Agent] KG extraction error: {e}")
        self._record_skill_feedback(success=False, task_input=user_input,
                                    duration=_time.time() - _task_start)
        cat = self._classify_task_category(user_input)
        self._save_task_stats(cat, current_iter, False)
        if self._self_review_history:
            summaries = "; ".join(
                h.split("进度总结：")[1].split("\n")[0][:80] if "进度总结：" in h else "N/A"
                for h in self._self_review_history[-3:]
            )
            return (
                f"[MAX_ITERATIONS_REACHED] Agent stopped after {self._correction_attempts} correction attempts. "
                f"Review history: {summaries}"
            )
        return "[MAX_ITERATIONS_REACHED] Agent stopped: Reached maximum iterations without a final answer. The task may be incomplete."

    def _classify_task_category(self, user_input: str) -> str:
        """Return the category name for a user input (for stats tracking)."""
        text = user_input.lower()
        for category, rules in self.TASK_CATEGORIES.items():
            if any(kw in text for kw in rules["keywords"]):
                return category
        return "general"
        
    def wait_for_user_input(self, question: str, options: Optional[List[str]] = None) -> str:
        """
        Block the agent thread and wait for user input from the frontend.
        """
        if self.progress_callback:
            self.progress_callback({
                "event": "ask_user",
                "question": question,
                "options": options
            })
        
        # Clear queue of any stale responses
        while not self.user_input_queue.empty():
            self.user_input_queue.get_nowait()
            
        # Block until the websocket sends a response
        answer = self.user_input_queue.get(block=True)
        return answer
