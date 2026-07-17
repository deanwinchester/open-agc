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
from tools.interaction import AskUserQuestionTool, PauseAndWaitTool, TaskPaused, SearchHistoryTool, UserInterjectionResponseTool
from tools.shell_interact import ShellSendTool
from tools.sandbox import EnterWorktreeTool, ExitWorktreeTool
from tools.self_review import SelfReviewTool
from tools.task_plan import TaskPlanTool, format_plan_for_prompt, load_plan
from tools.task_manager import TaskManagerTool
from tools.system_config import ConfigureSystemTool
from tools.plugin_dev import DevelopPluginTool
from tools.reader_lm import ReaderLMTool
from tools.compact_context import CompactContextTool


from agent.context_manager import (
    compress_search_results, compress_file_content, compress_shell_output,
    compress_tool_result, fold_tool_calls, compact_messages,
)
from prompt_builder import detect_system_env, PromptBuilderMixin

def _detect_system_env() -> str:
    """Delegate to prompt_builder module."""
    return detect_system_env()
class OpenAGCAgent(PromptBuilderMixin):
    """
    Main Agent Loop handling context, Tool calling, and orchestration.
    Supports real-time progress callbacks for task tracking.
    Features smart memory with TF-IDF semantic retrieval.
    """
    def __init__(self, model: str = "gpt-4o", session_id: Optional[int] = None,
                 logger: Optional[SessionLogger] = None,
                 pre_enabled_tools: Optional[set] = None):
        self.session_id = session_id
        self.failed_attempts = []
        self._consecutive_failures = 0
        self._correction_attempts = 0
        self._max_correction_attempts = 5
        self._in_self_review = False
        self._should_stop = False
        self._pending_final_answer = False
        self._final_answer_requested = False
        self._self_review_history: list = []
        self.logger = logger
        self.llm = LLMClient(default_model=model)
        self._pre_enabled_tools = pre_enabled_tools or set()
        self._session_sandbox_whitelist: set = set()
        self.pending_messages: list = []
        self._processing_interjection: bool = False
        self._rejected_interjection: Optional[dict] = None
        self._interjection_stuck_count: int = 0
        self._session_sandbox_whitelist: set = set()  # One-time approved paths
        self._session_permission_whitelist: set = set()  # Session-approved command categories
        self._session_network_whitelist: set = set()  # Session-approved network domains
        self._session_sudo_password: str = ""  # Cached sudo password for session (never sent to LLM)
        self._pending_sudo_password: str = ""  # One-shot password for next tool call
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
        budget_cfg = {}
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
            f"7. parse_html — 使用 Reader-lm 将 HTML 源码转为 Markdown（浏览器获取的页面过大时使用）\n"
            f"8. search_history — 检索当前会话历史（需要回忆之前内容时使用）\n"
            f"9. 其他专用工具根据场景选用\n"
            f"\n## 大文件下载\n"
            f"如果需要下载超过 100MB 的大文件（如模型文件 .gguf/.safetensors/.bin），"
            f"必须使用 queue_download 工具而非 execute_shell。它支持断点续传，"
            f"不会因为超时而失败。下载进度可在下载管理面板查看。\n"
            f"\n## 长时间任务后台化\n"
            f"当执行耗时操作（下载模型/安装依赖/训练等），shell 返回 [Still Running] 时，"
            f"应立即调用 pause_and_wait 工具暂停自己。系统会保存上下文，后台任务完成后自动恢复执行。"
            f"不要让用户干等着，也不要反复重试。\n"
            f"\n## Python 后台进程\n"
            f"如果使用 execute_python 启动长期运行的进程（如 ffmpeg 录屏、服务器等），"
            f"必须将 stdout/stderr 重定向到 subprocess.DEVNULL 或文件，否则父进程会"
            f"因管道阻塞而超时：\n"
            f"```python\n"
            f"subprocess.Popen([\"ffmpeg\", \"...\"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            f"```\n"
            f"启动后**不要 pause_and_wait**，继续执行后续任务。需要停止时再用 shell 命令杀掉进程。\n"
            f"\n## 交互式命令\n"
            f"如果 shell 返回 [Interactive] PID xxx，说明该命令已进入交互模式（如 python、mysql 等）。"
            f"此时进程并未超时，而是等待你的输入。你可以：\n"
            f"1. 使用 shell_send(pid=xxx, input=\"...\") 向进程发送输入并读取响应\n"
            f"2. 发送 exit 或 quit 退出交互模式\n"
            f"3. 或调用 pause_and_wait 保持进程运行\n"
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
            f"\n## 任务管理\n"
            f"使用 manage_task 工具查看现有任务、搜索历史任务、查看详情和交付物:\n"
            f"- list → 列出最近任务（可按状态筛选）\n"
            f"- search → 按关键词搜索历史任务\n"
            f"- get → 查看任务详情、步骤和交付物\n"
            f"- record_deliverable → 记录任务交付物\n"
            f"\n## 任务计划与大目标管理\n"
            f"对于多步骤的复杂任务，使用 manage_task_plan 工具管理：\n"
            f"\n"
            f"### Plan（执行计划）\n"
            f"- plan.create(goal, steps) → 创建任务执行计划，steps 是细分步骤\n"
            f"- plan.update(step_id, step_status, step_result) → 标记步骤完成\n"
            f"- plan.show() → 查看当前进度\n"
            f"- plan.check() → 确认是否所有步骤都已完成\n"
            f"- 一个 task 只有一个 plan，多次 create 会复用同一个\n"
            f"- 中断恢复后先 plan.show() 了解进度\n"
            f"\n"
            f"### Goal（大目标）\n"
            f"- goal_add(desc) → 只添加大的目标（如\"完成翻译模型集成\"）\n"
            f"- 不要将细分步骤加到 goal 中——细分步骤放在 plan 的 steps 里\n"
            f"- goal_start(id) / goal_done(id) 标记大目标进展\n"
            f"- 完成一个大目标关联的所有 plan 步骤后再标记 goal_done\n"
            f"\n"
            f"### 规则\n"
            f"- 先 plan.create 规划步骤，再按步骤执行\n"
            f"- 每完成一个重要步骤调用 plan.update 记录\n"
            f"- 认为自己完成时先 plan.check()，通过后才能结束\n"
            f"- 禁止擅自结束未完成的任务\n"
            f"- 简单的一次性任务不需要创建计划\n"
            f"\n# 记忆与技能系统\n"
            f"\n## 记忆系统\n"
            f"你可以用 manage_memory 工具管理记忆，用 search_history 工具搜索记忆。\n\n"
            f"记住信息：manage_memory(action='add', topic='话题', content='内容')\n"
            f"  - topic 必填，用短名词指定话题（如 '车票'、'偏好'、'项目配置'）\n"
            f"  - 同话题的记忆会按时效性自动排序，最新最相关的优先\n\n"
            f"回忆信息：search_history(query='关键词', topic='可选限定话题')\n"
            f"  - 搜到线索后可用 expand_id='mem:N' 查看完整内容\n"
            f"  - 一年未使用的记忆会自动归档，搜不到时可尝试 include_archived=True\n\n"
            f"查看全部记忆：manage_memory(action='read')\n"
            f"修改记忆：manage_memory(action='update', query=ID, content='新内容')\n"
            f"  - 先用 search_history 找到 ID，再用 update 修改\n"
            f"\n## 技能系统\n"
            f"在每次任务开始时，系统会根据任务内容自动检索并注入相关技能供你参考执行。"
            f"如果你成功完成了一项之前未完成过的复杂任务，并且得到了用户的正面反馈，"
            f"必须主动询问用户是否需要将过程保存为新技能。"
            f"如用户同意，请使用 save_learned_skill 工具。\n"
            f"\n## 自我审查机制\n"
            f"当任务接近最大迭代次数或你感觉陷入循环时，可以调用 self_review 工具进行自我审查。"
            f"系统会在达到迭代上限时自动提示你使用此工具。通过审查你可以获得额外的执行机会。"
            f"请诚实评估：如果确实陷入无效循环，及时报告用户比浪费计算资源更好。\n"
            f"\n## 扩展工具系统\n"
            f"当前可用的工具是核心工具子集。如果你的任务需要以下能力，但它们不在当前工具列表中，"
            f"请使用 search_available_tools 工具搜索并启用：\n"
            f"- 系统配置管理（查看/修改配置、管理 API 密钥、MCP 服务器）——搜索「配置」「设置」「API」\n"
            f"- 插件开发（生成新插件脚手架、安装插件）——搜索「插件」\n"
            f"- 以及其他未默认启用的专用工具\n"
            f"搜索成功后，工具将在你的下一轮回复中可用。\n"
            f"\n## 持久化事实 (MEMORY.md) 与 人格设定 (soul.md)\n"
            f"### MEMORY.md\n"
            f"`data/MEMORY.md` 是**最高优先级的持久化事实库**，每次任务开头系统会自动注入其内容。\n"
            f"当你发现以下类型的信息时，**必须使用 write_file 写入 data/MEMORY.md**，以便后续任务复用：\n"
            f"- 重要工具/软件的安装路径（如 ComfyUI、Python、Node.js 的准确位置）\n"
            f"- 常用服务端口号（如 ComfyUI 127.0.0.1:8188、API 服务端口）\n"
            f"- 用户的工作目录偏好和项目位置\n"
            f"- 项目中不需要重复搜索确认的固定配置\n"
            f"- 需要跨任务记住的路径、配置、命令模板\n\n"
            f"写入格式示例（markdown 列表）：\n"
            f"```markdown\n"
            f"- ComfyUI 路径: D:\\ComfyUI_windows_portable_v0220\\ComfyUI\n"
            f"- ComfyUI 端口: 127.0.0.1:8188\n"
            f"- Python 路径: D:\\Apps\\Python312\\python.exe\n"
            f"```\n"
            f"⚠️ 每个 key 只写一次，覆盖更新即可。不要重复添加相同内容。\n"
            f"\n### soul.md\n"
            f"`data/soul.md` 是你的**人格设定文件**，系统自动注入。你可以在这里定义你的回复风格、行为偏好、"
            f"角色定位等。例如：\n"
            f"```markdown\n"
            f"- 回复风格：简洁专业，用中文回复\n"
            f"- 优先使用 Python 而非 Shell 命令\n"
            f"- 每次执行重要操作前先向用户说明计划\n"
            f"```\n"
            f"你可以用 write_file 修改 data/soul.md 来实时调整风格。\n"
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
            "user_interjection_response": UserInterjectionResponseTool(),
            "search_history": SearchHistoryTool(),
            "pause_and_wait": PauseAndWaitTool(),
            "enter_sandbox_mode": EnterWorktreeTool(),
            "exit_sandbox_mode": ExitWorktreeTool(),
            "self_review": SelfReviewTool(),
            "configure_system": ConfigureSystemTool(),
            "develop_plugin": DevelopPluginTool(),
            "shell_send": ShellSendTool(),
            "manage_task_plan": TaskPlanTool(),
            "manage_task": TaskManagerTool(),
            "parse_html": ReaderLMTool() if ReaderLMTool.is_available() else None,
            "compact_context": CompactContextTool(),
        }

        # Add to core tool names so it's always available

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
            "compact_context": "清理上下文历史",
            "ask_user_question": "向用户提问",
            "user_interjection_response": "响应中断消息",
            "search_history": "检索会话历史",
            "pause_and_wait": "暂停并等待后台完成",
            "enter_sandbox_mode": "进入沙箱模式",
            "exit_sandbox_mode": "退出沙箱模式",
            "self_review": "自我审查任务进度",
            "configure_system": "系统配置管理",
            "develop_plugin": "插件开发",
            "shell_send": "交互命令输入",
            "manage_task_plan": "管理任务计划",
            "manage_task": "查看和管理任务",
            "parse_html": "HTML 转 Markdown",
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
                self.tool_display_names[tool_name] = (tool_instance.description or self.name)[:20]

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
        CORE_TOOL_NAMES = {"execute_shell", "manage_memory", "read_file", "write_file", "edit_file",
                           "search_file_content", "find_files", "search_available_tools",
                           "ask_user_question", "user_interjection_response", "search_history", "queue_download", "pause_and_wait",
                           "execute_python", "search_web", "self_review", "configure_system",
                           "manage_task_plan", "parse_html", "shell_send"}
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
                self.tool_schemas = [tool.get_openai_schema() for tool in self.available_tools.values() if tool is not None]
                
        self.full_available_tools["search_available_tools"] = ToolDiscoveryTool(
            full_tools=self.full_available_tools, 
            enable_callback=_enable_tools_callback
        )
        
        self.available_tools = {name: self.full_available_tools[name] for name in self.active_tool_names if name in self.full_available_tools}

        # Prepare OpenAI format tool schema
        self.tool_schemas = [tool.get_openai_schema() for tool in self.available_tools.values() if tool is not None]

        # Refresh system prompt with full tool list now that full_available_tools is ready
        self.messages[0]["content"] = self._build_system_prompt()

        # Track which skills were injected in the current turn for feedback loop
        self._active_skills: List[str] = []

    def _build_tool_list_section(self) -> str:
        """Build a markdown section listing all available tools (core + extended)."""
        lines = ["\n## 全量工具列表\n",
                 "以下是你可用的所有工具"
                 "（核心工具已加载完整用法，其余工具通过 search_available_tools 加载完整用法）：\n"]
        CORE_NAMES = {"execute_shell", "read_file", "write_file", "edit_file",
                       "search_file_content", "find_files", "execute_python",
                       "search_web", "manage_memory", "ask_user_question",
                       "search_history", "queue_download", "pause_and_wait",
                       "self_review", "search_available_tools", "configure_system", "compact_context"}
        core_items = []
        ext_items = []
        for name, tool in sorted(self.full_available_tools.items()):
            is_core = name in CORE_NAMES
            if is_core:
                desc = getattr(tool, 'description', '')[:60]
                item = f"  ✅ `{name}`"
                if desc:
                    item += f" — {desc}"
                core_items.append(item)
            else:
                ext_items.append(f"  🔧 `{name}`")
        if core_items:
            lines.append("核心工具（已就绪）：")
            lines.extend(core_items)
        if ext_items:
            lines.append("扩展工具（需通过 search_available_tools 唤醒）：")
            lines.extend(ext_items)
        return "\n".join(lines)

    def _build_system_prompt(self, memory_context: str = "", skill_context: str = "",
                             experience_context: str = "", kg_context: str = "") -> str:
        # Inject current date/time so the LLM knows "today"
        from datetime import datetime
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        current_date = datetime.now().strftime("%Y年%m月%d日")
        
        prompt = self.system_prompt_base.replace("{current_time}", current_time).replace("{current_date}", current_date)
        prompt = prompt.replace("{cwd_dir}", self.sandbox_dir or os.getcwd())
        prompt = prompt.replace("{system_env}", _detect_system_env())

        # Inject all available tool names (built after full_available_tools is populated)
        if hasattr(self, 'full_available_tools'):
            prompt += self._build_tool_list_section()

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
        """Poll pending message queue.

        New protocol: Returns injected message with `[用户插入: msg]` prefix and
        system instruction to use `user_interjection_response` tool for judgment.

        Returns:
            Empty string if nothing to inject, or the interjection message.
        """
        if not self.pending_messages:
            return ""

        # If already processing an interjection, check for stuck timeout
        if self._processing_interjection:
            self._interjection_stuck_count += 1
            if self._interjection_stuck_count > 12:  # ~12 iterations = ~30s timeout
                self._processing_interjection = False
                self._interjection_stuck_count = 0
                # Auto-pop and accept on timeout
                msg = self.pending_messages.pop(0)
                print(f"[Agent] Interjection timeout, auto-accepting: {msg[:60]}")
                return ""
            return ""

        # New interjection: inject with protocol
        msg = self.pending_messages[0]  # peek, don't pop
        self._processing_interjection = True
        self._interjection_stuck_count = 0
        print(f"[Agent] Injected interjection for LLM judgment: {msg[:80]}")
        return (
            f"[用户插入: {msg}] "
            f"【系统指令：请使用 user_interjection_response 工具判断是否处理此插入消息。"
            f"如果与你当前任务相关（约束/反馈/补充信息），选择 accept；"
            f"如果完全是新话题或无关内容，选择 reject（系统将自动为其创建新任务）；"
            f"如果不确定，选择 ask 向用户提问。】"
        )

    def queue_message(self, text: str):
        """Add a message to the pending queue (non-blocking input).
        Also unblocks any wait_for_user_input by putting into user_input_queue."""
        self.pending_messages.append(text)
        # If agent is blocked on ask_user_question, unblock with the message
        try:
            self.user_input_queue.put_nowait(f"[用户消息] {text}")
        except Exception:
            pass

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
                if category == "sudo":
                    _sudo_pw = result_holder.get("password", "")
                    if not _sudo_pw:
                        return "Operation denied: sudo requires a password but none was provided."
                    self._session_sudo_password = _sudo_pw
                    self._pending_sudo_password = _sudo_pw
                return None  # Retry
            elif action == "approve_always":
                if category != "sudo":
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
                else:
                    # Sudo: don't persist to config (password is per-session only).
                    # Use session whitelist so subsequent sudo calls don't re-trigger popup.
                    self._session_permission_whitelist.add(category)
                    print(f"[Agent] Sudo approved (session, password cached): {category}")
                if category == "sudo":
                    _sudo_pw = result_holder.get("password", "")
                    if not _sudo_pw:
                        return "Operation denied: sudo requires a password but none was provided."
                    self._session_sudo_password = _sudo_pw
                    self._pending_sudo_password = _sudo_pw
                return None  # Retry
            return f"Operation denied by user: {desc}"

        if action == "approve_dir":
            abs_path_sb = os.path.abspath(sb.path)
            # Use the path itself if it's a directory; otherwise use its parent
            if os.path.isdir(abs_path_sb):
                dirpath = abs_path_sb
            else:
                dirpath = os.path.dirname(abs_path_sb)
            self._session_sandbox_whitelist.add(dirpath)
            self._session_sandbox_whitelist.add(abs_path_sb)
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
                print(f"[Agent] Sandbox approved (dir): {dirpath}, whitelist now: {list(self._session_sandbox_whitelist)}")
            except Exception as e:
                print(f"[Agent] Failed to persist allowed_path: {e}")
            return None  # Signal to retry
        elif action == "approve_once":
            # Add parent directory to whitelist so other files in same dir work too
            dirpath = os.path.dirname(os.path.abspath(sb.path))
            self._session_sandbox_whitelist.add(dirpath)
            self._session_sandbox_whitelist.add(sb.path)
            print(f"[Agent] Sandbox approved (once): {sb.path} (dir: {dirpath}), whitelist now: {list(self._session_sandbox_whitelist)}")
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

    def _background_post_process(self, task_input: str, duration: float, success: bool):
        """Run reflection + auto-tool in background thread after run_turn returns."""
        try:
            self._record_skill_feedback(success=success, task_input=task_input, duration=duration)
        except Exception as e:
            print(f"[Agent] BG post-process error: {e}")
        if success:
            try:
                tool_name = self._auto_generate_tool(
                    task_input,
                    {"tool_sequence": self.reflection_engine._extract_tool_sequence(self.messages)},
                    self.llm
                )
                if tool_name:
                    print(f"[Agent] Auto-generated tool: {tool_name}")
            except Exception as e:
                print(f"[Agent] BG auto-tool error: {e}")

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
        self.tool_schemas = [t.get_openai_schema() for t in self.available_tools.values() if t is not None]
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
        return compress_search_results(result)
    @staticmethod
    def _compress_file_content(result: str) -> str:
        return compress_file_content(result)
    @staticmethod
    def _compress_shell_output(result: str, tool_name: str) -> str:
        return compress_shell_output(result, tool_name)
    def _compress_tool_result(self, result: str, tool_name: str) -> str:
        return compress_tool_result(result, tool_name)
    def _fold_tool_calls(self, messages: List[Dict], force: bool = False) -> List[Dict]:
        return fold_tool_calls(messages, force=force)
    def _llm_compact_messages(self, messages, target_token_savings=None):
        """Use LLM to produce a structured conversation summary (Claude Code style).

        Identifies the oldest portion of the conversation by round-boundary,
        sends it to the LLM with the compact prompt, and replaces it with the
        structured summary. The most recent N rounds are kept intact.

        Returns (new_messages, did_compact) where new_messages has the old
        prefix replaced with a summary user-message, or (messages, False) if
        compaction was not possible.
        """
        try:
            # Identify user-assistant round boundaries (skip system prompt at [0])
            rounds = []
            i = 1
            while i < len(messages):
                m = messages[i]
                if m.get("role") == "user":
                    start = i
                    i += 1
                    while i < len(messages):
                        sub = messages[i]
                        if sub.get("role") == "user":
                            break
                        if sub.get("role") == "assistant" and not sub.get("tool_calls"):
                            i += 1
                            rounds.append((start, i))
                            break
                        i += 1
                    else:
                        rounds.append((start, i))
                else:
                    i += 1

            if len(rounds) <= 2:
                print(f"[Agent] _llm_compact: only {len(rounds)} rounds, skipping")
                return messages, False

            # Keep the last 5 rounds intact for recent tool call context; summarize everything older
            keep_count = min(5, len(rounds) - 1)
            n = len(rounds) - keep_count  # number of rounds to summarize
            old = rounds[:n]
            rest = rounds[n:]

            # Build the conversation text for the LLM (truncate long tool results)
            parts = []
            for s, e in old:
                for rm in messages[s:e]:
                    role = rm.get("role", "")
                    content = str(rm.get("content", ""))
                    tc = rm.get("tool_calls")
                    if tc:
                        for t in tc:
                            fn = t.get("function", {})
                            name = fn.get("name", "?")
                            args_raw = str(fn.get("arguments", ""))[:200]
                            parts.append(f"[Tool: {name}({args_raw})]")
                    elif content:
                        # Truncate very long tool results
                        if role == "tool" and len(content) > 500:
                            content = content[:500] + f"\n... (truncated {len(content)-500} chars)"
                        parts.append(f"[{role}]: {content[:1000]}")

            text = "\n".join(parts)
            if not text.strip():
                # Nothing to summarize — fall back to folding
                folded = self._fold_tool_calls(messages, force=True)
                return folded, True

            # Build the compact prompt
            compact_prompt = (
                self._COMPACT_NO_TOOLS
                + self._COMPACT_PROMPT_BASE
                + f"\n\nHere is the conversation portion to summarize:\n\n{text[:12000]}"
            )

            print(f"[Agent] _llm_compact: summarizing {n} rounds ({len(text)} chars)...")

            resp, _ = self.llm.chat(
                messages=[{"role": "user", "content": compact_prompt}],
                # No tools — force text-only response
            )
            reply = (resp.choices[0].message.content or "").strip()
            if not reply:
                print("[Agent] _llm_compact: empty response, falling back to fold")
                folded = self._fold_tool_calls(messages, force=True)
                return folded, True

            # Extract <summary> block, strip <analysis> scratchpad
            import re as _re_compact
            summary_only = reply
            # Remove analysis scratchpad
            summary_only = _re_compact.sub(r'<analysis>.*?</analysis>', '', summary_only, flags=_re_compact.DOTALL)
            # Extract summary content from tags, or use whole response
            sm = _re_compact.search(r'<summary>(.*?)</summary>', summary_only, _re_compact.DOTALL)
            if sm:
                summary_text = sm.group(1).strip()
            else:
                # No tags — use entire response (strip analysis if present)
                summary_text = _re_compact.sub(r'<analysis>.*?</analysis>', '', reply, flags=_re_compact.DOTALL).strip()

            # Build the new messages:
            # [system, compact_summary, ...kept_rounds...]
            pruned = messages[:1]  # system prompt
            pruned.append({
                "role": "user",
                "content": (
                    "[会话摘要 — 以下是对较早对话的结构化总结，保留了最近的消息原文]\n\n"
                    f"{summary_text}"
                )
            })
            for s, e in rest:
                pruned.extend(messages[s:e])

            print(f"[Agent] LLM compact: {len(messages)} -> {len(pruned)} msgs (summarized {n} rounds)")
            return pruned, True

        except Exception as e:
            print(f"[Agent] _llm_compact error: {e}")
            try:
                folded = self._fold_tool_calls(messages, force=True)
                return folded, True
            except Exception:
                return messages, False

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
        # Check if user marked this task as completed while agent was idle
        if getattr(self, '_completed_by_user', False):
            return "用户已将任务标记为已完成。"

        # Only append user message if it's not None (None means we are resuming from ask_user_question)
        if user_input is not None:
            self.messages.append(build_user_message(user_input, images))
            if self.logger:
                self.logger.log_user_query(user_input)

        # Normalize: downstream classification/delegation helpers expect a string
        # (resume paths pass None).
        user_input = user_input or ""

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
            system_content = self._build_system_prompt(
                memory_context=memory_context,
                skill_context=skill_context,
                experience_context=experience_context,
                kg_context=kg_context,
            )
            
            if hasattr(self, 'failed_attempts') and self.failed_attempts:
                attempts_str = "\n".join([f"- {attempt}" for attempt in self.failed_attempts])
                system_content += f"\n\n## 历史失败尝试记录 (避坑指南)\n你过去曾尝试过以下操作但失败了，**请仔细分析原因，绝对不要原样重复**：\n{attempts_str}\n"

            # Inject task plan if task_id is set
            if self.task_id:
                try:
                    from tools.task_plan import load_plan as _load_plan, format_plan_for_prompt as _fmt_plan
                    # Prefer DB plan_id (O(1) lookup), fallback to scanning JSON files
                    _plan = None
                    try:
                        import sqlite3 as _sq3
                        from core.paths import get_data_path as _gdp
                        _conn = _sq3.connect(_gdp("chat_history.db"))
                        _row = _conn.execute("SELECT plan_id FROM tasks WHERE id=?", (self.task_id,)).fetchone()
                        _conn.close()
                        if _row and _row[0]:
                            _plan = _load_plan(plan_id=_row[0])
                    except Exception:
                        pass
                    if not _plan:
                        _plan = _load_plan(plan_id=None, task_id=self.task_id)
                    if _plan:
                        system_content += "\n\n" + _fmt_plan(_plan)
                except Exception:
                    pass
            # Inject goal list
            try:
                from tools.task_plan import load_goals as _load_goals, format_goal_list_for_prompt as _fmt_goals
                _goals = _load_goals()
                _goal_text = _fmt_goals(_goals)
                if _goal_text:
                    system_content += "\n\n" + _goal_text
            except Exception:
                pass
            # Inject task title as goal reminder so agent stays focused
            if self.task_id:
                try:
                    import sqlite3 as _sq3
                    from core.paths import get_data_path as _gdp
                    _conn = _sq3.connect(_gdp("chat_history.db"))
                    _row = _conn.execute("SELECT title FROM tasks WHERE id=?", (self.task_id,)).fetchone()
                    _conn.close()
                    if _row and _row[0]:
                        _goal = _row[0].strip()
                        # Only inject when title is a meaningful goal (> 5 chars)
                        if len(_goal) > 5 and _goal not in system_content:
                            system_content += f"\n\n## 当前任务目标\n始终聚焦于此目标，不要偏离：\n\n{_goal[:200]}\n"
                except Exception:
                    pass
            self.messages[0]["content"] = system_content

        # Always inject task plan, even with skip_rag=True (for resume scenarios)
        if self.messages and self.messages[0]["role"] == "system" and self.task_id:
            try:
                from tools.task_plan import load_plan as _load_plan, format_plan_for_prompt as _fmt_plan
                _plan = None
                try:
                    import sqlite3 as _sq3
                    from core.paths import get_data_path as _gdp
                    _conn = _sq3.connect(_gdp("chat_history.db"))
                    _row = _conn.execute("SELECT plan_id FROM tasks WHERE id=?", (self.task_id,)).fetchone()
                    _conn.close()
                    if _row and _row[0]:
                        _plan = _load_plan(plan_id=_row[0])
                except Exception:
                    pass
                if not _plan:
                    _plan = _load_plan(plan_id=None, task_id=self.task_id)
                if _plan:
                    _plan_text = "\n\n## 当前计划进度\n" + _fmt_plan(_plan)
                    if _plan_text not in self.messages[0]["content"]:
                        self.messages[0]["content"] += _plan_text
            except Exception:
                pass

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
        self._pending_final_answer = False
        self._final_answer_requested = False
        self._self_review_history = []

        # Tool loop detection state
        self._recent_tool_calls: list = []
        MAX_REPEATED_TOOL_CALLS = 3
        effective_max = max_iterations  # self-review has separate budget via _correction_attempts

        while (current_iter < max_iterations or
               self._pending_final_answer or
               self._correction_attempts < self._max_correction_attempts):
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

            if not self._in_self_review:
                current_iter += 1
            if verbose:
                label = f"{current_iter}/{max_iterations}" if not self._in_self_review else f"自纠{self._correction_attempts}/{self._max_correction_attempts}"
                print(f"[Agent Loop Iteration {label}] Calling LLM...")

            # Check if self-review decided to stop — inject final answer request
            if self._pending_final_answer:
                self._pending_final_answer = False
                self._final_answer_requested = True
                if verbose:
                    print("[Agent] Self-review recommended stop, requesting final answer.")
                self.messages.append({
                    "role": "user",
                    "content": "根据你的自我审查结果，请立即给出最终答复告知用户当前进展。"
                })
                # NOTE: deliberately NOT resetting _in_self_review here.
                # It should have been reset in the tool handler, so the
                # elif below won't re-inject the self-review prompt.

            # Max iterations reached — inject self-review prompt
            # (only when we're NOT in a final-answer-requested cycle)
            elif current_iter >= max_iterations and self._max_correction_attempts > 0 and not self._in_self_review and not self._final_answer_requested:
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

                # Detect cached tokens
                cached_tokens = 0
                try:
                    details = getattr(usage, 'prompt_tokens_details', None)
                    if details:
                        cached_tokens = getattr(details, 'cached_tokens', 0) or 0
                except Exception:
                    pass

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
                    task_id=self.task_id,
                    cached_tokens=cached_tokens
                )
                if progress_callback:
                    progress_callback({
                        "event": "usage",
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                        "cached_tokens": cached_tokens,
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
            message_dict = {"role": message.role, "content": message.content or ""}
            if message.tool_calls:
                message_dict["tool_calls"] = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in message.tool_calls
                ]
            _rc = getattr(message, "reasoning_content", None)
            if _rc:
                message_dict["reasoning_content"] = _rc
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
            # Send LLM response text as a progress event before tool calls
            if message.content and message.content.strip() and progress_callback:
                progress_callback({
                    "event": "response",
                    "content": message.content.strip()[:500],
                })

            if tool_calls:
                screenshot_urls = []
                for tool_call in tool_calls:
                    function_name = tool_call.function.name
                    raw_args = tool_call.function.arguments
                    try:
                        function_args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        # LLM sometimes returns malformed JSON (trailing comma, single quotes, etc.)
                        # Attempt simple repair before giving up
                        import re as _re
                        repaired = raw_args.strip()
                        # Try wrapping bare keys in quotes (common issue: `{key: value}`)
                        if not repaired.startswith('{'):
                            repaired = '{' + repaired
                        if not repaired.endswith('}'):
                            repaired = repaired + '}'
                        # Replace single quotes with double quotes (but preserve escaped ones)
                        repaired = _re.sub(r"(?<!\\)'", '"', repaired)
                        # Remove trailing commas before }
                        repaired = _re.sub(r',\s*}', '}', repaired)
                        try:
                            function_args = json.loads(repaired)
                        except json.JSONDecodeError:
                            print(f"[Agent] Failed to parse tool_call args for {function_name}: {str(raw_args)[:200]}")
                            continue
                    
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
                    import re as _re
                    normalized_args = _re.sub(r'\s+', ' ', str(function_args)).strip()
                    call_signature = f"{function_name}:{normalized_args}"
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
                                    # Pass sudo password to the shell tool only (never logged, never sent to LLM,
                                    # never exposed to other tools)
                                    _sudo_pw = self._pending_sudo_password or self._session_sudo_password
                                    if _sudo_pw and function_name == "execute_shell":
                                        extra_kwargs["_sudo_password"] = _sudo_pw
                                        self._pending_sudo_password = ""  # Clear one-shot after passing
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
                                            "wake_in_minutes": tp.wake_in_minutes,
                                        })
                                    # Remove orphaned tool_calls message so API doesn't reject on next call
                                    del self.messages[_tool_call_insertion_idx:]
                                    wake_tag = f"WAKE_IN={tp.wake_in_minutes} " if tp.wake_in_minutes else ""
                                    return f"[TASK_BACKGROUNDED] {wake_tag}{tp}"
                                except SandboxBlocked as sb:
                                    if attempt > 2:
                                        result = f"Sandbox blocked: {sb.path} (max retries exceeded)"
                                        tool_success = False
                                        break
                                    try:
                                        result = self._handle_sandbox_blocked(
                                            sb, function_name, function_args, progress_callback)
                                    except TaskPaused as _sb_tp:
                                        # TaskPaused raised from inside except SandboxBlocked handler
                                        # is NOT caught by the outer except TaskPaused (sibling handler).
                                        # Catch it here and convert to [TASK_BACKGROUNDED] properly.
                                        if progress_callback:
                                            progress_callback({
                                                "event": "task_backgrounded",
                                                "reason": str(_sb_tp),
                                            })
                                        del self.messages[_tool_call_insertion_idx:]
                                        return f"[TASK_BACKGROUNDED] {_sb_tp}"
                                    if result is not None:
                                        # User denied or timeout — return error
                                        tool_success = False
                                        break
                                    # User approved — notify server to persist this approval
                                    # so it survives agent recreation on task resume
                                    if progress_callback:
                                        progress_callback({
                                            "event": "sandbox_approved",
                                            "path": sb.path,
                                            "session_id": self.session_id,
                                        })
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

                    # Handle user_interjection_response results
                    if function_name == "user_interjection_response":
                        self._processing_interjection = False
                        self._interjection_stuck_count = 0
                        try:
                            import json as _jj
                            jr = _jj.loads(result_str)
                            action = jr.get("action", "accept")
                            if action == "accept":
                                self.pending_messages.pop(0)
                                # Re-inject as clean user message so LLM processes it naturally
                                clean_msg = jr.get("response", "") or "已收到"
                                self.messages[-2]["content"] = f"[用户插入已接受] {clean_msg}"
                                if verbose:
                                    print(f"[Agent] ✅ Interjection accepted: {clean_msg[:60]}")
                            elif action == "reject":
                                self.pending_messages.pop(0)
                                reason = jr.get("reason", "")
                                self._rejected_interjection = {
                                    "message": self.messages[-2].get("content", ""),
                                    "reason": reason,
                                    "response": jr.get("response", ""),
                                }
                                # Remove injected interjection messages from context
                                self.messages.pop()  # tool result
                                self.messages.pop()  # assistant tool_call
                                self.messages.pop()  # user interjection
                                if verbose:
                                    print(f"[Agent] ❌ Interjection rejected: {reason[:60]}")
                                continue  # Skip rest of loop, go back to LLM
                            elif action == "ask":
                                self.pending_messages.pop(0)
                                question = jr.get("question", "请澄清您的需求")
                                if verbose:
                                    print(f"[Agent] ❓ Interjection needs clarification: {question[:60]}")
                                # Use ask_user_question to get clarification
                                from tools.interaction import AskUserQuestionTool
                                aqt = AskUserQuestionTool()
                                # Create a fake context that stores the answer
                                _fake_ctx = type('obj', (object,), {
                                    'wait_for_user_input': lambda self, q, opts: None
                                })()
                                try:
                                    answer = aqt.execute(
                                        question_text=question,
                                        _agent_context=self  # This triggers user_input_queue.wait
                                    )
                                    if answer:
                                        clean_msg = jr.get("response", "") or ""
                                        self.messages.append({
                                            "role": "user",
                                            "content": f"[用户澄清] {answer}"
                                        })
                                        if verbose:
                                            print(f"[Agent] Got clarification: {answer[:60]}")
                                except Exception:
                                    pass
                                continue
                        except json.JSONDecodeError:
                            self._processing_interjection = False
                            print(f"[Agent] Failed to parse interjection response")

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
                                    self._pending_final_answer = True
                                    if verbose:
                                        print("[Agent] Self-review recommended stop, will request final answer.")
                        except Exception:
                            pass
                        # Reset consecutive failures counter after self-review
                        self._consecutive_failures = 0

                    # Context preservation: detect consecutive failures and remind model of original task
                    is_failure = not tool_success or result_str.startswith("Error") or result_str.startswith("System Guard") or result_str.startswith("Sandbox")
                    if is_failure:
                        self._consecutive_failures += 1
                        
                        if not hasattr(self, 'failed_attempts'):
                            self.failed_attempts = []
                        attempt_desc = f"`{function_name}`({args_preview}) => {result_str.split(chr(10))[0][:150]}"
                        if not self.failed_attempts or self.failed_attempts[-1] != attempt_desc:
                            self.failed_attempts.append(attempt_desc)
                            if len(self.failed_attempts) > 15:
                                self.failed_attempts.pop(0)
                    else:
                        self._consecutive_failures = 0

                    if self._consecutive_failures == 3 and current_iter < max_iterations:
                        reminder = (
                            f"[系统提醒] 你已连续 {self._consecutive_failures} 次工具调用失败。\n"
                            f"请暂停当前操作，回顾原始用户需求：「{user_input}」。\n"
                            f"**在执行下一个工具动作之前，你必须先输出一段文字，反思为什么之前的尝试会失败，并说明接下来的新策略与之前的有何不同。**\n"
                            f"如果当前方法不可行，请尝试完全不同的策略，或向用户报告当前进展并询问下一步指令。"
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

                # Autocompact: Context window management
                try:
                    from core.token_budget import estimate_messages_tokens
                    import time as _ctime
                    current_tokens = estimate_messages_tokens(self.messages)
                    budget_threshold = self.token_budget.max_tokens * 0.9
                    now = _ctime.time()

                    # --- Time-Based Microcompact ---
                    try:
                        _cfg_path = get_data_path("config.json")
                        _ttl = 3600
                        if os.path.exists(_cfg_path):
                            with open(_cfg_path, encoding="utf-8") as _f:
                                _cfg = json.load(_f)
                            _ttl = int(_cfg.get("cold_cache_ttl", 3600))
                    except Exception:
                        _ttl = 3600
                    compacted = self.token_budget.time_based_microcompact(self.messages, ttl=_ttl)
                    if compacted is not None and compacted is not self.messages:
                        if any("[Old tool result" in (m.get("content") or "") for m in compacted):
                            self.messages = compacted
                            if verbose:
                                print(f"[Agent] Time-based microcompact (ttl={_ttl}s)")

                    # --- Token Budget Compression ---
                    import sys as _dbg_sys
                    if current_tokens > budget_threshold * 0.7:  # log at 70% too
                        print(f"[DBG] Budget: {current_tokens}/{self.token_budget.max_tokens} ({current_tokens/self.token_budget.max_tokens*100:.0f}%) threshold={budget_threshold}", file=_dbg_sys.stderr, flush=True)
                    if current_tokens > budget_threshold:
                        if verbose:
                            print(f"[Agent] Token warning ({current_tokens}/{self.token_budget.max_tokens}), triggering autocompact...")

                        pruned = self.token_budget.prune_messages(self.messages)

                        if estimate_messages_tokens(pruned) > self.token_budget.max_tokens * 0.8:
                            llm_pruned, did = self._llm_compact_messages(pruned)
                            if did and len(llm_pruned) < len(pruned):
                                import sys as _dbg5
                                print(f"[DBG] LLM compact SUCCESS: {len(pruned)} -> {len(llm_pruned)} msgs", file=_dbg5.stderr, flush=True)
                                if verbose:
                                    print(f"[Agent] LLM compact: {len(pruned)} -> {len(llm_pruned)} msgs")
                                pruned = llm_pruned
                            else:
                                folded = self._fold_tool_calls(pruned, force=True)
                                if len(folded) < len(pruned):
                                    import sys as _dbg4
                                    print(f"[DBG] Fallback fold: {len(pruned)} -> {len(folded)} msgs", file=_dbg4.stderr, flush=True)
                                    pruned = folded

                        self.messages = pruned
                except Exception as e:
                    if verbose:
                        print(f"[Agent] Autocompact error: {e}")
                # After appending all tool results, the loop continues to send them back to LLM
                continue
                
            # 2. Check if model provided a text response (final answer)
            if message.content:
                # If self-review prompt was injected but LLM skipped the tool call,
                # accept the text as the final answer (the self-review prompt explicitly
                # says "如果确实已无进展，请回复最终答案告知用户").
                if self._in_self_review:
                    self._in_self_review = False
                    if verbose:
                        print("[Agent] LLM skipped self_review tool, accepting text as final answer.")
                    # Fall through to normal final answer handling below

                # Check for pending user messages that arrived during the LLM call.
                # If any exist, inject them and continue the loop instead of exiting.
                injected = self._check_pending_messages(user_input)
                if injected:
                    self.messages.append({"role": "user", "content": injected})
                    if verbose:
                        print(f"[Agent] Injected pending message before final answer, continuing loop")
                    continue

                final_answer = message.content
                if self.logger:
                    self.logger.log_agent_response(final_answer)
                # [Removed] Auto-extract & save memories — was unreliable (no topic, noisy).
                # Agent should use manage_memory(add) explicitly when needed.
                # Extract knowledge graph entities from this turn's messages
                try:
                    self.knowledge_graph.extract_from_messages(self.messages)
                except Exception as e:
                    print(f"[Agent] KG extraction error: {e}")
                # Save runtime stats (fast, synchronous)
                cat = self._classify_task_category(user_input)
                self._save_task_stats(cat, current_iter, True)
                # Defer reflection + auto-tool to background thread
                if self.reflection_engine:
                    import threading as _post_thr
                    _post_thr.Thread(
                        target=self._background_post_process,
                        args=(user_input, _time.time() - _task_start, True),
                        daemon=True,
                    ).start()
                # If there are rejected interjections, attach them to the response
                if self._rejected_interjection:
                    import json as _rj
                    reject_data = self._rejected_interjection
                    self._rejected_interjection = None
                    return f"[INTERJECTION_REJECTED] {_rj.dumps(reject_data, ensure_ascii=False)}\n{final_answer}"
                return final_answer

        # Extract knowledge graph entities even on failure
        try:
            self.knowledge_graph.extract_from_messages(self.messages)
        except Exception as e:
            print(f"[Agent] KG extraction error: {e}")
        cat = self._classify_task_category(user_input)
        self._save_task_stats(cat, current_iter, False)
        if self.reflection_engine:
            import threading as _post_thr3
            _post_thr3.Thread(
                target=self._background_post_process,
                args=(user_input, _time.time() - _task_start, False),
                daemon=True,
            ).start()
        if self._self_review_history:
            summaries = "; ".join(
                h.split("进度总结：")[1].split("\n")[0][:80] if "进度总结：" in h else "N/A"
                for h in self._self_review_history[-3:]
            )
            return (
                f"[MAX_ITERATIONS_REACHED] Agent stopped after {current_iter} iterations and {self._correction_attempts} correction attempts. "
                f"Review history: {summaries}"
            )
        return f"[MAX_ITERATIONS_REACHED] Agent stopped: Reached maximum iterations ({current_iter}) without a final answer ({self._correction_attempts}/{self._max_correction_attempts} corrections used). The task may be incomplete."

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
