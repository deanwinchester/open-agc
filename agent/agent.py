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

from core.llm_client import LLMClient, build_user_message, extract_screenshot_data, extract_image_data, replace_image_markers
from core.logger import SessionLogger
from core.memory_store import MemoryStore
from core.skill_store import SkillStore
from core.token_budget import TokenBudget, estimate_messages_tokens
from core.reflection import ReflectionEngine
from core.knowledge_graph import KnowledgeGraph
from core.stats_manager import get_stats_manager
from tools.shell import ShellTool
from tools.filesystem import ReadFileTool, WriteFileTool, EditFileTool, ListDirTool, ApplyPatchTool
from tools.search import GrepSearchTool, GlobTool
from tools.python_repl import PythonREPLTool
from tools.computer import ComputerTool
from tools.memory import MemoryTool
from tools.web_search import WebSearchTool
from tools.fetch_url import FetchURLTool
from tools.image_view import ImageViewTool
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
from tools.subagent_dispatch import DispatchSubagentTool
from tools.request_secret import RequestSecretTool


from agent.context_manager import (
    compress_search_results, compress_file_content, compress_shell_output,
    compress_tool_result, fold_tool_calls,
)
from prompt_builder import detect_system_env

def _detect_system_env() -> str:
    """Delegate to prompt_builder module."""
    return detect_system_env()


# ── Delegation context-isolation fix (debugging-continuation gate) ──
# Absolute paths mentioned in the conversation (Windows D:\... / D:/... and
# common POSIX roots). Used to build the context brief handed to sub-agents
# and to detect topic overlap between a pasted error and recent turns.
# Segment charset excludes whitespace and sentence punctuation but keeps CJK
# (paths like D:\中新社\...). Space-containing paths (D:\My Documents\proj)
# are matched via continuation segments that must contain a separator and
# must not themselves be a new drive path (so "D:\a D:\b" stays two paths).
# The drive branch requires a non-letter left boundary and rejects "://" so
# URLs like https://cdn.example.com/x.png are never matched.
_PATH_SEG = r"[^\s\"'“”‘’，。；：、:)\]}<>|*,!?]"
_PATH_CONT = (
    r"(?: +(?![A-Za-z]:[\\/])(?=[A-Za-z0-9_.])"
    + _PATH_SEG + r"*[\\/]" + _PATH_SEG + r"*)*"
)
_SESSION_PATH_RE = re.compile(
    r"[\"'“]([A-Za-z]:[\\/][^\"'”]+)[\"'”]"                      # quoted path
    + r"|(?<![A-Za-z])[A-Za-z]:[\\/](?!/)" + _PATH_SEG + r"*" + _PATH_CONT
    + r"|(?<![A-Za-z0-9:/])/(?:home|Users|opt|srv|mnt|data|var)/"
    + _PATH_SEG + r"*" + _PATH_CONT
)
# Inputs that look like a pasted error log / failure report.
_ERROR_LOG_RE = re.compile(
    r"\b(error|exception|traceback|failed|failure)\b|报错|错误|异常|启动失败",
    re.IGNORECASE,
)
# Generic words excluded from topic-overlap matching (log noise).
_TOPIC_STOPWORDS = {
    "error", "errors", "exception", "traceback", "failed", "failure",
    "warning", "caused", "java", "spring", "boot", "http", "https",
    "main", "test", "file", "data", "info", "null", "none", "true", "false",
}
_DRIVE_COMPONENT_RE = re.compile(r"[a-z]:")


def _message_text(msg: Dict) -> str:
    """Plain text of a message (content may be a multimodal list)."""
    c = msg.get("content") or ""
    if isinstance(c, list):
        return " ".join(str(i.get("text", "")) for i in c if isinstance(i, dict))
    return str(c)


def _session_paths(text: str) -> List[str]:
    """Extract absolute paths from text, cleaned of trailing punctuation."""
    out = []
    for m in _SESSION_PATH_RE.finditer(text or ""):
        p = (m.group(1) or m.group(0)).strip().rstrip("\\/.")
        if "://" in p:  # belt-and-braces: never treat a URL as a path
            continue
        if len(p) >= 4:
            out.append(p)
    return out


def _topic_tokens(text: str) -> set:
    """Topic tokens: path components (project/file names) + identifiers."""
    tokens = set()
    for p in _session_paths(text):
        tokens.add(p.lower())
        for comp in re.split(r"[\\/]", p):
            comp = comp.strip().lower()
            if _DRIVE_COMPONENT_RE.fullmatch(comp):
                continue  # bare drive letter ("d:") carries no topic signal
            if len(comp) >= 2:
                tokens.add(comp)
                if "." in comp:
                    stem = comp.rsplit(".", 1)[0]
                    if len(stem) >= 2:
                        tokens.add(stem)
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{3,}", text or ""):
        t = tok.lower()
        if t not in _TOPIC_STOPWORDS:
            tokens.add(t)
    return tokens


# ── Shared post-process worker (module-level singleton) ──
# One queue + one daemon worker thread per process, NOT per agent instance:
# api/ws.py creates a new agent per chat message, so a per-instance worker
# thread (whose bound-method target strongly references the agent) would leak
# one thread + one resident agent per message forever.
_post_process_queue: queue.Queue = queue.Queue()
_post_process_worker_lock = threading.Lock()
_post_process_worker_started = False


def _ensure_post_process_worker():
    """Start the shared post-process worker thread (once per process)."""
    global _post_process_worker_started
    if _post_process_worker_started:
        return
    with _post_process_worker_lock:
        if _post_process_worker_started:  # double-checked locking
            return
        _post_process_worker_started = True
        threading.Thread(
            target=_post_process_loop,
            name="agent-post-process",
            daemon=True,
        ).start()


def _post_process_loop():
    """Serial worker: runs queued post-process jobs one at a time.

    Each job carries its own agent reference (session isolation — the job
    only ever touches that agent's stores/engines); the reference is dropped
    as soon as the job finishes, so an idle queue holds no agents.
    """
    while True:
        try:
            job = _post_process_queue.get()
        except Exception:
            continue
        try:
            agent, task_input, duration, success, messages = job
            agent._background_post_process(task_input, duration, success,
                                           messages=messages)
        except Exception as e:
            print(f"[Agent] Post-process worker error: {e}")
        finally:
            agent = None  # drop the agent reference promptly
            job = None
            try:
                _post_process_queue.task_done()
            except ValueError:
                pass


class OpenAGCAgent:
    """
    Main Agent Loop handling context, Tool calling, and orchestration.
    Supports real-time progress callbacks for task tracking.
    Features smart memory with TF-IDF semantic retrieval.
    """
    def __init__(self, model: str = "gpt-4o", session_id: Optional[int] = None,
                 logger: Optional[SessionLogger] = None,
                 pre_enabled_tools: Optional[set] = None,
                 memory_db_path: Optional[str] = None):
        # memory_db_path: optional override for the memory DB (eval probes
        # inject an isolated temp DB). None = production default memory.db.
        self.model = model
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
        self._processing_interjection: bool = False
        self._rejected_interjection: Optional[dict] = None
        self._interjection_stuck_count: int = 0
        self._session_sandbox_whitelist: set = set()  # One-time approved paths
        self._session_permission_whitelist: set = set()  # Session-approved command categories
        self._session_network_whitelist: set = set()  # Session-approved network domains
        self._session_sudo_password: str = ""  # Cached sudo password for session (never sent to LLM)
        self._pending_sudo_password: str = ""  # One-shot password for next tool call
        # Hydrate session-scoped sudo password / permission whitelist from the
        # shared store (api.state): a new agent instance is created per message,
        # so instance-level caches alone lose prior sudo authorization, making
        # the password popup appear (or fail to appear) unpredictably.
        self._hydrate_session_shared()
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
        # Tiered tool exposure: only a small resident core in tool_schemas,
        # everything else discovered+injected via search_available_tools.
        # Set tool_tiered_exposure=false in config.json for full residency.
        self.tool_tiered_exposure = True
        budget_cfg = {}
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    if config.get("sandbox_mode", True):
                        self.sandbox_dir = config.get("sandbox_dir", os.path.abspath(os.path.join(os.getcwd(), "workspace")))
                    self.browser_headless = config.get("browser_headless", False)
                    self.tool_tiered_exposure = config.get("tool_tiered_exposure", True)
                    # Initialize token budget from config if available
                    budget_cfg = config.get("context_budget", {})
            except Exception:
                budget_cfg = {}

        # TokenBudget 优先级：config.json 的 context_budget 显式配置优先；
        # 否则按模型上下文窗口（llm_client 初始化时从 litellm model_cost /
        # llamacpp_ctx_size 解析）设置 max_total_tokens；再退化为内置默认。
        if budget_cfg:
            self.token_budget = TokenBudget(config=budget_cfg)
        else:
            _model_window = getattr(self.llm, "model_context_window", 0) or 0
            self.token_budget = TokenBudget(
                config={"max_total_tokens": _model_window} if _model_window > 0 else None)

        # Initialize smart memory store before reflection/knowledge engines
        self.memory_store = MemoryStore(
            db_path=memory_db_path or get_data_path("memory.db"),
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

        # 原生工具调用模型（litellm tools= 传递）必须走原生通道：
        # 在系统提示里教 JSON 文本格式会误导强指令遵循模型（如 kimi code 系）
        # 绕过原生 tool_calls，导致 agent 永远进不了步骤循环。
        # 仅本地 GGUF 模型（无原生工具调用能力）保留 JSON 文本格式教学。
        _native_tools = not str(model or "").startswith(("llamacpp/", "sglang/"))
        if _native_tools:
            _tool_call_rule = (
                f"\n## 2. 工具调用方式\n"
                f"始终通过系统提供的原生工具调用机制（tool calls）发起调用，"
                f"严禁把工具调用写成 JSON 文本或 markdown 代码块输出在回复正文中。\n"
                f"需要验证、执行或获取信息时必须实际调用工具，"
                f"不要仅凭记忆或上下文中的信息直接回答。\n"
            )
        else:
            _tool_call_rule = (
                f"\n## 2. 工具调用格式\n"
                f"工具调用格式：`{{\"name\": \"tool_name\", \"arguments\": {{\"key\": \"value\"}}}}`。"
                f"仅当你决定使用工具时，才输出 JSON 对象。对于正常对话回复，直接输出纯文本，严禁使用 JSON 格式。\n"
                f"如想在调用工具前表达思考过程，放在 JSON 之前的独立段落中。\n"
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
            f"{_tool_call_rule}"
            f"\n## 3. 上下文复用\n"
            f"当用户要求\"重试\"\"再下载一遍\"\"再试一次\"等操作时，"
            f"必须先检查对话历史中的 tool_call 记录，复用已有的 URL、参数、文件路径等数据。"
            f"绝对不要重新浏览网页或重新搜索来获取已知信息。\n"
            f"\n## 4. 失败处理\n"
            f"如果某个方法失败，先分析错误原因再换策略。不要盲目重试同样的操作，"
            f"也不要因为一次失败就完全放弃可行的方法。\n"
            f"修复配置或代码后，必须用 execute_shell 或 execute_python 实际验证"
            f"（编译、运行或检查输出），确认问题真正解决；"
            f"严禁只做未经验证的参数调整（如反复横跳引号、格式）就再次提交结果。\n"
            f"\n# 工具使用指南\n"
            f"\n## 工具优先级（按推荐顺序）\n"
            f"1. write_file / edit_file / apply_patch — 创建和修改文件（首选文件操作方式；多文件多处批量修改用 apply_patch）\n"
            f"2. execute_python — 运行 Python 代码进行数据处理、测试等\n"
            f"3. execute_shell — 执行系统命令（仅当无专用工具可用时）\n"
            f"4. search_file_content / find_files — 搜索文件内容、查找文件（查看目录结构用 list_dir，扩展工具需先启用）\n"
            f"5. search_web / fetch_url — 搜索互联网获取最新信息 / 抓取已知 URL 的网页正文\n"
            f"6. search_history — 检索当前会话历史（需要回忆之前内容时使用）\n"
            f"7. browser_automation — 虚拟浏览器操作网页（扩展工具，先用 search_available_tools 启用）\n"
            f"8. parse_html — 使用 Reader-lm 将 HTML 源码转为 Markdown（扩展工具，需先启用；浏览器获取的页面过大时使用）\n"
            f"9. 其他专用工具（下载、邮件、任务计划、电脑控制等）先通过 search_available_tools 搜索启用，再根据场景选用\n"
            f"\n## 大文件下载\n"
            f"如果需要下载超过 100MB 的大文件（如模型文件 .gguf/.safetensors/.bin），"
            f"必须使用 queue_download 工具（扩展工具，需先通过 search_available_tools 启用）而非 execute_shell。它支持断点续传，"
            f"不会因为超时而失败。下载进度可在下载管理面板查看。\n"
            f"下载完成或失败时，系统会推送【系统通知】告知结果。向用户汇报下载或后台任务进展前，"
            f"必须严格依据系统通知与下载管理器中的实际记录：只有确认成功才可报喜，"
            f"失败必须如实说明失败原因，严禁凭推断谎称下载成功。\n"
            f"\n## 长时间任务后台化\n"
            f"当执行耗时操作（下载模型/安装依赖/训练等），shell 返回 [Still Running] 时，"
            f"应立即调用 pause_and_wait 工具（扩展工具，未启用时先 search_available_tools）暂停自己。系统会保存上下文，后台任务完成后自动恢复执行。"
            f"不要让用户干等着，也不要反复重试。\n"
            f"\n## 大任务检查点\n"
            f"执行大批量/长耗时任务（大规模数据导出、批量处理、分批抓取等）时，"
            f"必须在沙箱工作目录下维护进度检查点文件（确切路径见「当前任务检查点」段，"
            f"格式为 .checkpoints/task_<任务ID>.json），每处理完一批就立即更新。字段：\n"
            f"- task: 任务描述\n"
            f"- total: 总条数/总量\n"
            f"- done: 已处理条数\n"
            f"- last_cursor: 最后处理位置的游标/主键/偏移量（断点续跑的关键，必须精确）\n"
            f"- phase: 当前阶段\n"
            f"- files_dir: 交付物目录\n"
            f"- updated_at: 更新时间（ISO 格式）\n"
            f"任务被中断后恢复时，系统会把该检查点内容注入上下文；届时必须从 last_cursor 断点继续，"
            f"严禁清理现场从头重跑、严禁重复处理已完成部分。\n"
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
            f"1. 使用 shell_send(pid=xxx, input=\"...\") 向进程发送输入并读取响应（扩展工具，未启用时先 search_available_tools 启用）\n"
            f"2. 发送 exit 或 quit 退出交互模式\n"
            f"3. 或调用 pause_and_wait 保持进程运行\n"
            f"\n## 凭据与凭证库\n"
            f"任务需要密码、API Key、数据库账号等凭据时：\n"
            f"1. 先查看系统提示中的「已保存凭据」列表（如有）；已有合适的凭据就直接用 "
            f"{{{{secret:名称.字段}}}} 引用（username/password/host/uri/note），"
            f"执行 shell/python 时系统会自动替换为真实值。\n"
            f"2. 没有合适的凭据时，调用 request_secret 工具（扩展工具，需先 search_available_tools 启用）"
            f"向用户收集——系统会弹出表单让用户填写并自动存入本地凭证库。\n"
            f"3. 严禁直接向用户索要明文凭据，也严禁把凭据明文写进命令、代码、文件或回复中。\n"
            f"4. 如果对话中出现了明文凭据，提醒用户改用凭证库保存。\n"
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
            f"优先使用 browser_automation（虚拟浏览器，扩展工具，需先 search_available_tools 启用）工具的 upload 动作将文件填入网页。"
            f"如果遇到必须通过操作系统原生文件选择框处理的情况，"
            f"可临时使用 computer_control（键鼠控制，扩展工具）来操作系统的上传弹窗。\n"
            f"\n## 任务管理\n"
            f"使用 manage_task 工具（扩展工具，需先 search_available_tools 启用）查看现有任务、搜索历史任务、查看详情和交付物:\n"
            f"- list → 列出最近任务（可按状态筛选）\n"
            f"- search → 按关键词搜索历史任务\n"
            f"- get → 查看任务详情、步骤和交付物\n"
            f"- record_deliverable → 记录任务交付物\n"
            f"\n## 任务计划与大目标管理\n"
            f"对于多步骤的复杂任务，使用 manage_task_plan 工具管理"
            f"（扩展工具，首次使用前需先通过 search_available_tools 搜索「任务计划」启用）：\n"
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
            f"如用户同意，请使用 save_learned_skill 工具（扩展工具，需先 search_available_tools 启用）。\n"
            f"\n## 自我审查机制\n"
            f"当任务接近最大迭代次数或你感觉陷入循环时，可以调用 self_review 工具进行自我审查。"
            f"系统会在达到迭代上限时自动提示你使用此工具。通过审查你可以获得额外的执行机会。"
            f"请诚实评估：如果确实陷入无效循环，及时报告用户比浪费计算资源更好。\n"
            f"\n## 扩展工具系统\n"
            f"当前可用的工具是核心工具子集。其余工具默认未启用；需要某种能力时，"
            f"请使用 search_available_tools 工具搜索并启用（支持中文关键词），"
            f"搜索成功后工具将在你的下一轮回复中可用。常见扩展能力：\n"
            f"- 浏览器/网页自动化——搜索「浏览器」\n"
            f"- 大文件下载——搜索「下载」\n"
            f"- 任务计划与任务管理——搜索「任务」\n"
            f"- 邮件收发——搜索「邮件」\n"
            f"- 电脑键鼠控制——搜索「电脑」\n"
            f"- 系统配置管理（查看/修改配置、管理 API 密钥、MCP 服务器）——搜索「配置」「设置」「API」\n"
            f"- 插件开发（生成新插件脚手架、安装插件）——搜索「插件」\n"
            f"- 以及其他未默认启用的专用工具\n"
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

        # Post-process (reflection/auto-tool) runs on the module-level shared
        # worker — see _ensure_post_process_worker; nothing to start here.
        
        # Instantiate tools (MemoryTool shares the same store)
        memory_tool = MemoryTool(
            db_path=memory_db_path or get_data_path("memory.db"),
            session_id=self.session_id
        )
        self.full_available_tools = {
            "execute_shell": ShellTool(),
            "read_file": ReadFileTool(),
            "write_file": WriteFileTool(),
            "edit_file": EditFileTool(),
            "apply_patch": ApplyPatchTool(),
            "search_file_content": GrepSearchTool(),
            "find_files": GlobTool(),
            "list_dir": ListDirTool(),
            "execute_python": PythonREPLTool(),
            "computer_control": ComputerTool(),
            "manage_memory": memory_tool,
            "search_web": WebSearchTool(),
            "fetch_url": FetchURLTool(),
            "image_view": ImageViewTool(),
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
            "dispatch_subagent": DispatchSubagentTool(),
            "request_secret": RequestSecretTool(),
        }

        # Add to core tool names so it's always available

        # Tool display names (Chinese-friendly)
        self.tool_display_names = {
            "execute_shell": "执行终端命令",
            "read_file": "读取文件",
            "write_file": "写入文件",
            "edit_file": "局部修改文件",
            "apply_patch": "批量应用多处编辑",
            "search_file_content": "搜索文件内容",
            "find_files": "查找文件",
            "list_dir": "列出目录结构",
            "execute_python": "运行 Python 代码",
            "computer_control": "操控电脑",
            "manage_memory": "管理记忆",
            "search_web": "搜索网页",
            "fetch_url": "抓取网页正文",
            "image_view": "查看图片",
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
            "dispatch_subagent": "分派子代理",
            "request_secret": "向用户收集凭据",
        }

        # Load auto-generated tools (persisted from previous sessions)
        # Store in data/auto_tools/{session_id} to isolate per session
        if self.session_id is not None:
            user_gen_dir = get_data_path(f"auto_tools/{self.session_id}")
        else:
            user_gen_dir = get_data_path("auto_tools")
        init_auto_tools(user_gen_dir)
        self._auto_tools_dir = user_gen_dir
        # Archive stale, never-used auto-tools (loading skips _archive)
        try:
            from tools.auto_tool import prune_auto_tools
            _pruned = prune_auto_tools(user_gen_dir)
            if _pruned["archived"]:
                print(f"[Agent] Auto-tools pruned: {len(_pruned['archived'])} archived, "
                      f"{len(_pruned['kept'])} kept")
        except Exception as e:
            print(f"[Agent] Auto-tool prune error: {e}")
        # Track each dynamic tool's home directory — its trust file lives
        # beside the tool (usage recording / graduation need it).
        self._dynamic_tool_dirs: Dict[str, str] = {}
        loaded = load_all_dynamic_tools(user_gen_dir)
        for tool_name, tool_instance in loaded.items():
            if tool_name not in self.full_available_tools:
                self.full_available_tools[tool_name] = tool_instance
                self.tool_display_names[tool_name] = (tool_instance.description or self.name)[:20]
                self._dynamic_tool_dirs[tool_name] = user_gen_dir

        # Graduated tools live in skills/permanent and load in EVERY session
        _permanent_dir = os.path.join(get_skills_dir(), "permanent")
        for tool_name, tool_instance in load_all_dynamic_tools(_permanent_dir).items():
            if tool_name not in self.full_available_tools:
                self.full_available_tools[tool_name] = tool_instance
                self.tool_display_names[tool_name] = (tool_instance.description or self.name)[:20]
                self._dynamic_tool_dirs[tool_name] = _permanent_dir

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
        # Tiered exposure (default): only a minimal resident core goes into the
        # initial tool_schemas; every other tool stays discoverable via
        # search_available_tools and is injected into the schema on demand
        # (same path as adaptive auto-resident below).
        # Resident-core rationale:
        # - read/write/edit/shell/python/grep/glob/web: brief's base toolkit
        # - search_available_tools: the discovery path itself, must be resident
        # - user_interjection_response / self_review: the system injects
        #   instructions telling the model to call these directly
        # - manage_memory / search_history: the 记忆系统 prompt section teaches
        #   their exact usage every session; memory ops happen in normal chat
        TIERED_CORE_TOOL_NAMES = {"read_file", "write_file", "edit_file", "apply_patch", "execute_shell",
                                  "execute_python", "search_file_content", "find_files",
                                  "search_web", "fetch_url", "ask_user_question", "self_review",
                                  "user_interjection_response", "manage_memory",
                                  "search_history", "search_available_tools"}
        # Legacy full-resident set (tool_tiered_exposure=false)
        FULL_CORE_TOOL_NAMES = {"execute_shell", "manage_memory", "read_file", "write_file", "edit_file", "apply_patch",
                                "search_file_content", "find_files", "list_dir", "search_available_tools",
                                "ask_user_question", "user_interjection_response", "search_history", "queue_download", "pause_and_wait",
                                "execute_python", "search_web", "fetch_url", "self_review", "configure_system",
                                "manage_task_plan", "parse_html", "shell_send"}
        CORE_TOOL_NAMES = TIERED_CORE_TOOL_NAMES if self.tool_tiered_exposure else FULL_CORE_TOOL_NAMES
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
        # Mark tools that are actually resident right now (core + adaptive +
        # pre-enabled) — matches what's really in tool_schemas this session.
        resident = getattr(self, 'active_tool_names', set())
        core_items = []
        ext_items = []
        for name, tool in sorted(self.full_available_tools.items()):
            is_core = name in resident
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

        # Inject secrets vault view (for-llm): masked metadata only — the
        # password value NEVER enters the system prompt. Same data as
        # GET /api/secrets/for-llm (core.secrets.list_secrets).
        try:
            from core.secrets import list_secrets as _list_vault_secrets
            _vault = _list_vault_secrets()
            if _vault:
                _sec_lines = [
                    f"- {_s['name']} ({_s.get('type', 'generic')}@{_s.get('host') or '-'},"
                    f" 用户 {_s.get('username_masked') or '-'})"
                    for _s in _vault
                ]
                prompt += (
                    "\n--- 已保存凭据（本地凭证库，仅元信息，绝不含明文）---\n"
                    + "\n".join(_sec_lines)
                    + "\n引用方式：{{secret:名称.username}} / {{secret:名称.password}} / "
                    "{{secret:名称.host}} / {{secret:名称.uri}}（执行 shell/python 时自动替换为真实值，"
                    "不要索要或输出明文）。需要新凭据时用 request_secret 工具向用户收集。\n"
                )
        except Exception:
            pass

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

    def _resolve_reflection_input(self, user_input: str) -> str:
        """恢复执行时，反思应关联原始任务而非合成恢复提示。"""
        if not user_input.startswith("【系统提示】任务已恢复") or not self.task_id:
            return user_input
        try:
            from api.db import db_connect
            row = db_connect().execute(
                "SELECT user_query FROM tasks WHERE id=?", (self.task_id,)).fetchone()
            if row and row[0]:
                return row[0]
        except Exception:
            pass
        return user_input

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
                # Timeout: don't silently drop the user's message — inject it
                # as a normal user message so the current loop processes it.
                msg = self.pending_messages.pop(0)
                print(f"[Agent] Interjection timeout, injecting as normal user message: {msg[:60]}")
                return (
                    f"[用户消息: {msg}] "
                    f"【系统提示：该插入消息等待判断超时，现按普通用户消息注入当前任务。"
                    f"请在回复中明确告知用户该消息的处理方式（纳入当前任务继续处理，"
                    f"或说明它与当前任务无关、建议稍后单独处理）。】"
                )
            return ""

        # System notices (download results etc.) are factual system events, not
        # user interjections — inject directly and deterministically, bypassing
        # the accept/reject/ask LLM judgment so a 'reject' can never drop them.
        msg = self.pending_messages[0]
        if msg.lstrip().startswith("【系统通知】"):
            self.pending_messages.pop(0)
            print(f"[Agent] Injected system notice directly (no judgment): {msg[:80]}")
            return (
                f"{msg}\n"
                f"【系统指令：以上为系统事件通知（非用户插入），已确定送达，无需判断。"
                f"请结合当前任务继续执行；向用户汇报相关进展时必须基于该通知如实说明。】"
            )

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

    def _hydrate_session_shared(self):
        """Load session-scoped sudo password / permission whitelist from the
        shared store (api.state) into this instance.

        The instance whitelist becomes the *same* set object as the shared one,
        so later ``.add()`` calls propagate to other instances of this session.
        """
        if self.session_id is None:
            return
        try:
            from api.state import _session_sudo_passwords, _session_permission_whitelists
            shared_wl = _session_permission_whitelists.get(self.session_id)
            if shared_wl is not None:
                self._session_permission_whitelist = shared_wl
            self._session_sudo_password = _session_sudo_passwords.get(self.session_id, "")
        except Exception:
            pass

    def _get_shared_sudo_password(self) -> str:
        """Read the session-level shared sudo password (authoritative source;
        also refreshes the instance-level cache)."""
        if self.session_id is None:
            return ""
        try:
            from api.state import _session_sudo_passwords
            pw = _session_sudo_passwords.get(self.session_id, "") or ""
            if pw:
                self._session_sudo_password = pw
            return pw
        except Exception:
            return ""

    def _sync_permission_shared(self, category: str, sudo_pw: str = ""):
        """Write an approved permission category (and sudo password, if any)
        into the session-level shared store so they survive agent re-creation."""
        if self.session_id is None:
            return
        try:
            from api.state import _session_sudo_passwords, _session_permission_whitelists
            if category:
                _session_permission_whitelists.setdefault(self.session_id, set()).add(category)
            if sudo_pw:
                _session_sudo_passwords[self.session_id] = sudo_pw
        except Exception:
            pass

    def _handle_sandbox_blocked(self, sb, tool_name, tool_args, progress_callback):
        """Pause agent loop and wait for user to approve/deny sandbox path access."""
        import threading
        import json as _json
        import uuid as _uuid
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
        # Unique id for this wait: two concurrent sandbox prompts in one session
        # must not clobber each other in _sandbox_waits (previously keyed by
        # session_id, so the second wait overwrote the first, which then always
        # timed out). The id travels to the frontend in the sandbox_blocked
        # event and comes back in the sandbox_response message.
        request_id = _uuid.uuid4().hex
        if progress_callback:
            progress_callback({
                "event": "sandbox_blocked",
                "path": sb.path,
                "tool_name": tool_name,
                "session_id": self.session_id,
                "request_id": request_id,
                "block_type": block_type,
                "description": desc_text,
                "category": category_text,
            })

        # Wait for user response
        wait_event = threading.Event()
        result_holder = {"action": "timeout"}
        # api.state is the home of _sandbox_waits (api.server merely re-exports
        # it) — importing the light state module avoids pulling the whole
        # server (and its import-time DB init) into the agent loop.
        try:
            from api.state import _sandbox_waits
        except Exception as e:
            print(f"[Agent] Failed to import _sandbox_waits: {e}")
            return f"Sandbox authorization failed (internal error): {sb.path}"
        entry = {"event": wait_event, "result": result_holder,
                 "session_id": self.session_id, "request_id": request_id,
                 # Payload for re-broadcast: a client that missed the original
                 # event (disconnect / other session) gets the modal on connect.
                 "payload": {"path": sb.path, "tool_name": tool_name,
                             "block_type": block_type, "description": desc_text,
                             "category": category_text}}
        _sandbox_waits[request_id] = entry
        # Legacy fallback key for clients that reply without a request_id
        # (matched by session_id in ws.py / /api/sandbox/approve).
        _sandbox_waits[self.session_id] = entry

        def _clear_wait_entry():
            _sandbox_waits.pop(request_id, None)
            # Only drop the session fallback key if it still points to OUR
            # entry — a concurrent wait may have overwritten it.
            if _sandbox_waits.get(self.session_id) is entry:
                _sandbox_waits.pop(self.session_id, None)

        print(f"[Agent] Sandbox blocked: {sb.path} — waiting for user response...")
        # Segmented wait: 1s slices so a user interrupt is honored promptly
        # instead of sleeping through the full 120s timeout.
        responded = False
        deadline = _time.time() + 120
        while _time.time() < deadline:
            if wait_event.wait(timeout=1):
                responded = True
                break
            if self.is_interrupted:
                break

        if not responded:
            _clear_wait_entry()
            if self.is_interrupted:
                print(f"[Agent] Sandbox wait interrupted for {sb.path}")
                return f"Sandbox authorization interrupted by user: {sb.path}"
            print(f"[Agent] Sandbox wait timeout for {sb.path}")
            from tools.interaction import TaskPaused
            raise TaskPaused(f"等待权限授权超时，转入后台挂起状态，请确认权限后恢复执行。路径: {sb.path}")

        action = result_holder.get("action", "deny_once")
        try:
            _clear_wait_entry()
        except Exception:
            pass

        # ── Secret collection (request_secret popup → credential vault) ──
        # The popup form fields were passed through by ws.py into result_holder
        # (in-memory only). The vault write happens here, agent-side.
        if getattr(sb, "category", "") == "secret":
            return self._handle_secret_collection(sb, action, result_holder)

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
                _sudo_pw = ""
                if category == "sudo":
                    _sudo_pw = result_holder.get("password", "")
                    if not _sudo_pw:
                        return "Operation denied: sudo requires a password but none was provided."
                    self._session_sudo_password = _sudo_pw
                    self._pending_sudo_password = _sudo_pw
                self._sync_permission_shared(category, _sudo_pw)
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
                self._sync_permission_shared(
                    category, self._session_sudo_password if category == "sudo" else "")
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

    def _handle_secret_collection(self, sb, action: str, result_holder: dict):
        """Vault-write side of request_secret: upsert the popup form fields into
        core.secrets. Plaintext exists only in result_holder (in-memory) until
        persisted to the local vault — it is never logged or sent to the LLM.

        Returns None on success so the caller retries the tool: the retried
        RequestSecretTool sees ``_last_saved_secret`` and returns the
        confirmation text ({{secret:name}} reference, no plaintext) with
        tool_success=True. A non-None return means collection failed or was
        denied and becomes the tool result directly.
        """
        if not str(action or "").startswith("approve"):
            return ("用户取消了凭据收集。不要向用户索要明文凭据；"
                    "如确需凭据，可稍后再次调用 request_secret，或改用其他方式。")
        try:
            from core.secrets import upsert_secret, get_secret
            name = (result_holder.get("secret_name") or "").strip() \
                   or (sb.path or "").strip()
            if not name:
                name = f"secret_{int(_time.time())}"
            stype = (result_holder.get("secret_type") or "").strip() or "generic"
            host = (result_holder.get("host") or "").strip()
            username = result_holder.get("username") or ""
            password = result_holder.get("password") or ""
            note = result_holder.get("note") or ""
            if not password:
                return ("凭据未保存：密码/密钥为必填项但本次提交为空。"
                        "如仍需凭据，请再次调用 request_secret。")
            # 同名覆盖：授权弹窗的提交本身就是用户确认，直接覆盖但记录日志。
            existed = get_secret(name) is not None
            entry = upsert_secret(name=name, type=stype, host=host,
                                  username=username, password=password, note=note)
            print(f"[Agent] Secret {'overwritten' if existed else 'saved'} via "
                  f"request_secret popup: {name} ({entry.get('type')}@{entry.get('host') or '-'})")
            # Hand confirmation metadata (no plaintext) to the retried tool call.
            self._last_saved_secret = {"name": name, "type": entry.get("type") or stype,
                                       "host": entry.get("host") or ""}
            return None
        except ValueError as e:
            return (f"凭据未保存：{e}。如需重试，请用合法名称"
                    f"（仅字母/数字/_/-）再次调用 request_secret。")
        except Exception as e:
            return f"凭据保存失败：{e}"

    def _handle_subagent_sandbox_blocked(self, sb, plan, progress_callback):
        """Route a SandboxBlocked re-raised by a sub-agent into the main auth flow.

        Returns a result dict shaped like SubAgent.run()'s failure result so the
        delegation collector treats it uniformly. The sub-agent itself has
        already aborted; on approval the shared session whitelist is updated so
        a retried or later tool call goes through.
        """
        from tools.interaction import TaskPaused
        _task_desc = plan.get("task", "?") if isinstance(plan, dict) else "?"
        try:
            auth_result = self._handle_sandbox_blocked(
                sb, sb.tool_name or "sub_agent", {}, progress_callback)
        except TaskPaused as tp:
            return {"success": False,
                    "summary": f"子任务「{_task_desc}」等待沙箱授权超时: {tp}"}
        if auth_result is None:
            # Approved — whitelist updated, but the sub-agent already aborted.
            return {"success": False,
                    "summary": (f"子任务「{_task_desc}」因沙箱限制中断；"
                                f"路径 {sb.path} 已获用户授权，可在后续重试该操作。")}
        return {"success": False, "summary": str(auth_result)}

    def _record_skill_feedback(self, success: bool, task_input: str = "",
                                duration: float = 0, messages: list = None):
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
                    messages=messages if messages is not None else self.messages,
                    success=success,
                    duration_seconds=duration,
                )
            except Exception as e:
                print(f"[Agent] Reflection error: {e}")

    def _background_post_process(self, task_input: str, duration: float, success: bool,
                                 messages: list = None):
        """Run reflection + auto-tool in background after run_turn returns.

        ``messages`` must be a snapshot taken at enqueue time so the worker
        never races with the next turn mutating ``self.messages``.
        """
        msgs = messages if messages is not None else self.messages
        try:
            self._record_skill_feedback(success=success, task_input=task_input,
                                        duration=duration, messages=msgs)
        except Exception as e:
            print(f"[Agent] BG post-process error: {e}")
        if success:
            try:
                tool_name = self._auto_generate_tool(
                    task_input,
                    {"tool_sequence": self.reflection_engine._extract_tool_sequence(msgs)},
                    self.llm
                )
                if tool_name:
                    print(f"[Agent] Auto-generated tool: {tool_name}")
            except Exception as e:
                print(f"[Agent] BG auto-tool error: {e}")

    def _enqueue_post_process(self, task_input: str, duration: float, success: bool):
        """Enqueue reflection + auto-tool on the shared post-process worker.

        A snapshot of the conversation is taken here so the worker never
        races with the next turn mutating ``self.messages``. The job carries
        this agent's reference only until processed — an idle queue holds no
        agents, so short-lived agents stay garbage-collectable.
        """
        if not self.reflection_engine:
            return
        try:
            _ensure_post_process_worker()
            _post_process_queue.put(
                (self, task_input, duration, success, list(self.messages)))
        except Exception as e:
            print(f"[Agent] Post-process enqueue error: {e}")

    def _finalize_failed_turn(self, user_input: str, current_iter: int, duration: float):
        """Cleanup shared by abnormal turn exits (max iterations, LLM failure).

        Extracts KG entities, saves task stats and enqueues background
        post-process so no finalization step is ever skipped.
        """
        try:
            self.knowledge_graph.extract_from_messages(self.messages)
        except Exception as e:
            print(f"[Agent] KG extraction error: {e}")
        cat = self._classify_task_category(user_input)
        self._save_task_stats(cat, current_iter, False)
        self._enqueue_post_process(self._reflection_task_input, duration, False)

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
        # Mirror into full_available_tools — the tool_done path resolves dynamic
        # tools there (usage recording / graduation), and the dedup gate checks it.
        self.full_available_tools[tool_name] = tool_instance
        self.tool_display_names[tool_name] = tool_instance.description[:20]
        self.tool_schemas = [t.get_openai_schema() for t in self.available_tools.values() if t is not None]
        return True

    def _session_auto_tools_dir(self) -> str:
        """Directory holding this session's auto-generated tools."""
        d = getattr(self, "_auto_tools_dir", None)
        if d:
            return d
        return get_data_path(f"auto_tools/{self.session_id or '1'}")

    def _reinforce_existing_tool(self, tool_name: str):
        """Record a dedup/reinforce signal for an existing auto-tool.

        Uses record_tool_reinforce: bumps total/reinforced only — never pushes
        the consecutive-success streak, so reinforcement alone cannot graduate
        a tool that was never actually executed.
        """
        try:
            from tools.auto_tool import record_tool_reinforce
            tools_dir = getattr(self, "_dynamic_tool_dirs", {}).get(
                tool_name, self._session_auto_tools_dir())
            record_tool_reinforce(tools_dir, tool_name)
            print(f"[Agent] Auto-tool reinforced existing tool: {tool_name}")
        except Exception as e:
            print(f"[Agent] Auto-tool reinforce error: {e}")

    def _auto_generate_tool(self, task_input: str, trajectory, llm_client) -> Optional[str]:
        """Try to generate a reusable tool from a successful trajectory.

        Gate (plan_tool_generation): ≥5 tool calls, deterministic trajectory
        (execute_shell/execute_python dominant — exploratory read/search
        trajectories are skipped), plus a lightweight LLM reusability verdict.
        Trajectories overlapping an existing auto-tool reinforce that tool's
        trust record instead of generating a duplicate.
        """
        from tools.auto_tool import plan_tool_generation
        tool_sequence = trajectory.get("tool_sequence", "")
        existing = {
            name: (self.full_available_tools[name].description or "")
            for name in getattr(self, "_dynamic_tool_dirs", {})
            if name in self.full_available_tools
        }
        plan = plan_tool_generation(task_input, tool_sequence, existing, llm_client)
        if plan["action"] == "skip":
            print(f"[Agent] Auto-tool generation skipped: {plan['reason']}")
            return None
        if plan["action"] == "reinforce":
            self._reinforce_existing_tool(plan["overlap_with"])
            return None

        code = generate_tool_code(task_input, tool_sequence,
                                   "Success", llm_client)
        if not code:
            self._notify_tool_gen_failed("LLM 未返回有效代码")
            return None
        if not validate_tool_code(code):
            from tools.auto_tool import get_last_reject_reason
            self._notify_tool_gen_failed(f"安全校验拒绝：{get_last_reject_reason()}")
            return None

        # Extract name from TOOL_SCHEMA
        import ast
        tool_name = None
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

        # Dedup: never overwrite an existing tool — reinforce it instead
        if tool_name in getattr(self, "_dynamic_tool_dirs", {}) or \
                tool_name in self.full_available_tools:
            print(f"[Agent] Auto-tool generation deduped: {tool_name} already exists")
            self._reinforce_existing_tool(tool_name)
            return None

        filepath = save_tool_code(code, tool_name, self._session_auto_tools_dir())
        if not filepath:
            self._notify_tool_gen_failed("保存工具文件失败")
            return None

        from tools.auto_tool import load_dynamic_tool
        tool_instance = load_dynamic_tool(filepath)
        if not tool_instance:
            self._notify_tool_gen_failed("生成的代码加载失败（语法或运行错误）")
            return None

        if self._register_dynamic_tool(tool_name, tool_instance):
            _dyn_dirs = getattr(self, "_dynamic_tool_dirs", None)
            if _dyn_dirs is not None:
                _dyn_dirs[tool_name] = self._session_auto_tools_dir()
            return tool_name
        return None

    def _notify_tool_gen_failed(self, reason: str):
        """自动工具生成失败时通知用户（此前静默丢弃，用户找不到工具也不知道原因）。"""
        print(f"[Agent] Auto-tool generation failed: {reason}")
        try:
            from api.ws import save_message
            from api.state import _broadcast_to_websockets
            text = f"⚠️ 自动工具生成未通过：{reason}。本次不会生成可复用工具。"
            save_message("system", text, self.session_id)
            _broadcast_to_websockets({
                "type": "system_message", "content": text,
                "session_id": self.session_id,
            })
        except Exception as e:
            print(f"[Agent] Tool-gen notify error: {e}")

    def _build_context_brief(self) -> str:
        """Build a short delegation brief from conversation history.

        Sub-agents run with an isolated context and cannot see this session;
        the brief carries the essentials: current task goal (latest user
        message), absolute paths mentioned in the session (deduped, max 5),
        and summaries of the last few user messages (100 chars each).
        Returns at most 500 chars.

        Budget order matters: goal and paths are written first, then message
        summaries fill whatever budget remains — in long debugging sessions
        the path line must survive the 500-char cap (it is what keeps the
        sub-agent from blind-scanning for the repository).
        """
        msgs = getattr(self, "messages", None) or []
        user_texts = [
            _message_text(m).strip()
            for m in msgs if m.get("role") == "user"
        ]
        user_texts = [t for t in user_texts if t]

        lines = []
        if user_texts:
            goal = user_texts[-1].replace("\n", " ")[:100]
            lines.append(f"当前任务目标：{goal}")

        paths = []
        for m in msgs:
            if m.get("role") not in ("user", "assistant"):
                continue
            for p in _session_paths(_message_text(m)):
                if p not in paths:
                    paths.append(p)
        if paths:
            lines.append("会话涉及路径：" + "；".join(paths[:5]))

        # Fill remaining budget with recent user-message summaries.
        brief = "\n".join(lines)
        for t in user_texts[-5:]:
            line = "- " + t.replace("\n", " ")[:100]
            if len(brief) + 1 + len(line) > 500:
                break
            lines.append(line)
            brief = "\n".join(lines)

        return brief[:500]

    def _is_debugging_continuation(self, user_input: str) -> bool:
        """True when input is a pasted error/log and the recent conversation
        is already working the same topic (shared path or project name).

        Such input is a debugging continuation — the fix is usually obvious
        from conversation context (e.g. create a missing table), so it must
        stay in the main loop instead of being delegated to sub-agents that
        cannot see this session.
        """
        if not _ERROR_LOG_RE.search(user_input or ""):
            return False
        msgs = getattr(self, "messages", None) or []
        assistant_texts = []
        for m in reversed(msgs):
            if m.get("role") != "assistant":
                continue
            c = _message_text(m).strip()
            if c:
                assistant_texts.append(c)
            if len(assistant_texts) >= 2:
                break
        if not assistant_texts:
            return False
        input_tokens = _topic_tokens(user_input)
        if not input_tokens:
            # Pure-Chinese error pastes yield no path/identifier tokens at
            # all. With a session in progress, conservatively treat it as a
            # continuation: the accident this gate fixes was over-delegation,
            # and a false "stay in main loop" costs far less than delegating
            # a context-free sub-agent that blind-scans for the repository.
            return True
        # The last (up to) 2 assistant turns must both touch the same topic.
        return all(_topic_tokens(t) & input_tokens for t in assistant_texts)

    def _should_delegate(self, user_input: str) -> bool:
        """Assess whether a task is complex enough to warrant sub-agent delegation."""
        # 本轮已委派过一次：子代理结果已在上下文中，剩余工作由主代理完成，
        # 防止"委派→继续→再委派"循环。
        if getattr(self, "_delegated_this_turn", False):
            return False
        text = user_input.lower()
        # Highest-priority gate: a pasted error log that continues the current
        # debugging thread is fixed in the main loop, never delegated.
        _gate = getattr(self, "_is_debugging_continuation", None)
        if _gate and _gate(user_input):
            return False
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

        # Check if it spans multiple areas. Keywords are domain-semantic words
        # kept separate from tool names, so merely mentioning a tool
        # (e.g. "用 read_file 读一下 X") no longer hits every domain at once.
        area_count = 0
        for entry in TOOL_SETS.values():
            if any(kw in text for kw in entry["keywords"]):
                area_count += 1

        # Delegate if high complexity or truly multi-domain. The combined
        # branch requires area_count >= 2 (a real cross-domain task like
        # "部署并监控这个服务" = deploy + monitor) so that high-frequency
        # words alone ("列出所有文件" — 所有 + single filesystem domain)
        # cannot force delegation for a one-step request.
        # Note: Do not use len(text) > 200 because users often paste long error logs for simple one-shot fixes.
        return match_count >= 2 or area_count >= 3 or (match_count >= 1 and area_count >= 2)

    def _decompose_task(self, task_input: str) -> List[Dict]:
        """Use LLM to decompose a complex task into sub-tasks."""
        _brief_fn = getattr(self, "_build_context_brief", None)
        brief = _brief_fn() if _brief_fn else ""
        context_section = (
            f"\n会话上下文（子代理看不到主对话，分解时必须以此为依据）：\n{brief}\n"
            if brief else ""
        )
        prompt = f"""将以下任务分解为可执行子任务。
{context_section}
任务：{task_input}

要求：
- 每个子任务应独立、可完成
- 子任务必须基于以上会话上下文中提到的项目路径直接执行，不得安排"寻找/定位代码仓库"之类的子任务（除非上下文中确实没有可用路径）
- 为每个子任务标注需要的工具类型（可选：filesystem, code, web, analysis, deploy, monitor, research）
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
                        entry = TOOL_SETS.get(t)
                        resolved.extend(entry["tools"] if entry else [t])
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
            task_goal = result.get("task", "")
            goal_line = f"**目标**：{task_goal}\n\n" if task_goal else ""
            parts.append(
                f"### 子任务 {i} [{status}] （{duration:.1f}s, {tc} 步）\n"
                f"{goal_line}{summary}\n"
            )
            if steps:
                step_lines = ["<details><summary>执行步骤（点击展开）</summary>\n"]
                for si, step in enumerate(steps, 1):
                    s_status = "✅" if step.get("success") else "❌"
                    tool_name = step.get("tool", "?")
                    args = step.get("args", "")[:120]
                    step_lines.append(
                        f"- {s_status} `{tool_name}` {args}"
                    )
                step_lines.append("</details>")
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

    # 写入侧截断上限（字符数，按工具类型）：写入 self.messages 的单条工具
    # 结果不得超过该 cap，保证上下文窗口不被单条巨型结果挤爆。
    _TOOL_RESULT_WRITE_CAPS = {
        "read_file": 8000,
        "fetch_url": 8000,
        "execute_shell": 12000,
        "execute_python": 12000,
    }
    _TOOL_RESULT_WRITE_CAP_DEFAULT = 4000

    def _truncate_tool_result_for_context(self, result: str, tool_name: str) -> str:
        """工具结果写入 messages 前的上下文截断（写入侧防线）。

        超出按工具类型的 cap 时先走 compress_tool_result（头 + 评分中段 + 尾），
        仍超 cap 时硬截断兜底，保证写入结果不超 cap。

        与进度事件里的 _full_cap 分工：本方法只裁写入 self.messages 的上下文
        内容；_full_cap 只裁 full_result（前端展示 / 落库 task_steps），不影响
        上下文。
        """
        cap = self._TOOL_RESULT_WRITE_CAPS.get(
            tool_name, self._TOOL_RESULT_WRITE_CAP_DEFAULT)
        if len(result) <= cap:
            return result
        compressed = self._compress_tool_result(result, tool_name)
        if len(compressed) <= cap:
            return compressed
        # 硬截断兜底（压缩器对单行超长等极端输入可能不缩反胀）：
        # 后缀计入 cap，保证总长度不超 cap。
        suffix = "\n...(truncated)"
        return compressed[:cap - len(suffix)] + suffix

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
        self._delegated_this_turn = False
        # 失败尝试记录同样随新任务清空——否则上一任务的避坑清单会泄漏进
        # 本任务的 system prompt（跨任务污染）。
        self.failed_attempts = []
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

        # 反思/统计用的任务描述：恢复路径的 user_input 是合成提示
        # ("【系统提示】任务已恢复…")，反思应关联原始任务而非恢复动作本身
        self._reflection_task_input = self._resolve_reflection_input(user_input)

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
                    # 告知数字任务 ID 与检查点文件路径（「大任务检查点」约定的
                    # 落盘位置）——任务 ID 只在运行期经 run_turn 传入，静态提示
                    # 里无法写死；恢复时服务端读取同一文件把断点注入上下文。
                    _ckpt_path = os.path.join(self.sandbox_dir or os.getcwd(),
                                              ".checkpoints", f"task_{self.task_id}.json")
                    system_content += (
                        f"\n\n## 当前任务检查点\n当前任务 ID: {self.task_id}。"
                        f"执行大批量/长耗时任务时，必须把进度检查点维护到 `{_ckpt_path}`"
                        f"（字段与规则见「大任务检查点」约定），每处理完一批就更新。\n")
                except Exception:
                    pass
            self.messages[0]["content"] = system_content

        # 任务计划注入（唯一注入点，带标题段 + 去重守卫；skip_rag=True 的
        # resume 场景也覆盖）。原先 system prompt 重建段里的第一段无标题注入
        # 已删除——两段并存会导致计划文本在系统提示里出现两次。
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
                # Brief carried into every sub-agent (isolated context fix):
                # they cannot see this conversation, so hand them the goal,
                # recent user messages and session paths up front.
                context_brief = self._build_context_brief()
                sub_results = []
                completed = set()
                # Normalize plans: guarantee id/task keys so malformed LLM
                # output cannot crash the delegation loop with KeyError.
                plans = [p for p in plans if isinstance(p, dict)]
                for i, _p in enumerate(plans, 1):
                    _p.setdefault("id", i)
                    _p.setdefault("task", f"子任务 {i}")
                # Execute sub-agents respecting dependency order
                remaining = list(plans)
                skipped = []  # Subtasks dropped because a dependency failed
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
                    from tools.base import SandboxBlocked
                    batch_futures = {}
                    with concurrent.futures.ThreadPoolExecutor(
                            max_workers=min(len(batch), 4)) as executor:
                        for plan in batch:
                            sub = SubAgent(
                                task=plan.get("task", f"子任务 {plan.get('id', '?')}"),
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
                                context_brief=context_brief,
                            )
                            batch_futures[executor.submit(sub.run)] = plan
                        for future in concurrent.futures.as_completed(batch_futures):
                            plan = batch_futures[future]
                            try:
                                result = future.result()
                            except SandboxBlocked as sb:
                                # Sub-agent re-raised a sandbox block (it has no
                                # auth channel of its own) — route it through the
                                # main agent's authorization flow.
                                result = self._handle_subagent_sandbox_blocked(
                                    sb, plan, progress_callback)
                            except Exception as e:
                                result = {"success": False, "summary": str(e)}
                            # 带上子任务目标，报告才能看出每个子代理在做什么
                            result["task"] = plan.get("task", "")
                            sub_results.append(result)
                            if result.get("success"):
                                completed.add(plan.get("id"))
                            else:
                                dep_ids = {plan.get("id")}
                                skipped.extend(
                                    p for p in remaining
                                    if dep_ids & set(p.get("depends_on", [])))
                                remaining = [p for p in remaining
                                    if not (dep_ids & set(p.get("depends_on", [])))]
                # Surface subtasks that never executed (failed or circular
                # dependencies) so they are not silently dropped.
                unexecuted = skipped + remaining
                if unexecuted:
                    skipped_lines = "\n".join(
                        f"- 子任务 {p.get('id', '?')}「{p.get('task', '(无描述)')}」"
                        for p in unexecuted
                    )
                    print(f"[Agent] {len(unexecuted)} subtask(s) not executed:\n{skipped_lines}")
                    sub_results.append({
                        "success": False,
                        "summary": (
                            f"以下 {len(unexecuted)} 个子任务未执行"
                            f"（依赖失败或循环依赖）：\n{skipped_lines}"
                        ),
                    })
                result_text = self._synthesize_results(user_input, sub_results)
                self.messages.append({"role": "assistant", "content": result_text})

                # 委派结果只是中间产物：子任务可能只是探测/准备工作。
                # 注入继续指引并落入主循环，让主代理基于子代理结果继续完成
                # 原始任务，而不是直接以报告收尾。
                self.messages.append({"role": "user", "content": (
                    "【系统提示】子代理阶段已完成。请基于以上子代理的执行结果，评估原始任务"
                    "是否已真正完成：如果目标尚未达成（例如子任务只做了探测、准备工作），"
                    "请继续亲自执行剩余部分，不要仅以子代理报告收尾。")})
                self._delegated_this_turn = True

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
                self._record_skill_feedback(success=False, task_input=self._reflection_task_input,
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
            
            try:
                response, actual_model = self.llm.chat(messages=self.messages, tools=self.tool_schemas)
                choices = getattr(response, "choices", None) or []
                if not choices:
                    raise ValueError("LLM returned an empty choices list")
                message = choices[0].message
            except Exception as e:
                # LLM call failed (network error, empty choices, malformed
                # response). Must not escape run_turn — run the same cleanup
                # as the max-iterations path so stats/KG/post-process still run.
                error_text = (f"[LLM_ERROR] Agent stopped: LLM call failed at iteration "
                              f"{current_iter}: {e}")
                if verbose:
                    print(f"[Agent] {error_text}")
                self.messages.append({"role": "assistant",
                                      "content": f"Error: LLM call failed: {e}"})
                self._finalize_failed_turn(user_input, current_iter,
                                           _time.time() - _task_start)
                return error_text
            
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
                                    # never exposed to other tools). Shared session store is authoritative.
                                    _sudo_pw = (self._pending_sudo_password
                                                or self._get_shared_sudo_password()
                                                or self._session_sudo_password)
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
                                    # so it survives agent recreation on task resume.
                                    # Secrets are vault entries, not sandbox paths: skip.
                                    if progress_callback and getattr(sb, "category", "") != "secret":
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
                            if function_name in self.full_available_tools:
                                # Known but lazy (tiered exposure): guide the
                                # model through the discovery path instead of
                                # a dead-end "not found".
                                result = (f"Error: Tool '{function_name}' is not enabled in the current session. "
                                          f"Call search_available_tools with a related query to enable it, "
                                          f"then retry your call.")
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
                            from tools.auto_tool import (record_tool_usage,
                                                         check_graduation, graduate_tool)
                            # Usage recording feeds the trust file that drives
                            # graduation. The trust file lives beside the tool,
                            # so resolve the tool's home dir (session dir or
                            # skills/permanent) rather than assuming session.
                            _tools_dir = getattr(self, "_dynamic_tool_dirs", {}).get(
                                function_name) or self._session_auto_tools_dir()
                            record_tool_usage(_tools_dir, function_name, tool_success)
                            # Only session-scoped tools graduate; permanent
                            # tools already graduated (a second graduate would
                            # move the file onto itself).
                            if _tools_dir == self._session_auto_tools_dir() and \
                                    check_graduation(_tools_dir, function_name):
                                print(f"[Agent] Auto-tool {function_name} ready for graduation!")
                                if graduate_tool(_tools_dir, function_name):
                                    _dyn_dirs = getattr(self, "_dynamic_tool_dirs", None)
                                    if _dyn_dirs is not None:
                                        _dyn_dirs[function_name] = os.path.join(
                                            get_skills_dir(), "permanent")
                                    self.skill_store.refresh()
                        except Exception:
                            pass

                    result_str = str(result)

                    # Vision data: extract base64 image payloads from the full
                    # (untruncated) result FIRST, then swap the marker for a
                    # short placeholder — otherwise the tool message would
                    # retain a second copy of the base64 blob alongside the
                    # user image message injected below.
                    url = extract_screenshot_data(result_str)
                    if url:
                        screenshot_urls.append((url, "[工具执行截图 — 请根据此截图内容继续后续操作]"))
                    img_url = extract_image_data(result_str)
                    if img_url:
                        screenshot_urls.append((img_url, "[image_view 读取的本地图片 — 请查看图片内容并继续后续操作]"))
                    result_str = replace_image_markers(result_str)

                    # Secrets masking BEFORE truncation (single choke point, covers
                    # ALL tools): a truncation cut through a password/URI would let
                    # the pieces escape whole-string matching — mask first.
                    try:
                        from core.secrets import mask_secrets as _mask_secret_values
                        result_str = _mask_secret_values(result_str)
                    except Exception:
                        pass

                    # Context Compaction（写入侧截断）：超长工具结果按工具类型
                    # cap 后写入 messages（read_file/fetch_url 8000、
                    # execute_shell/execute_python 12000、其余 4000），超出先走
                    # compress_tool_result（头+评分中段+尾），仍超则硬截断。
                    # 与下方 _full_cap 的分工：这里裁的是写入 self.messages 的
                    # 上下文内容；_full_cap 只裁进度事件 full_result（前端展示/
                    # 落库 task_steps），不影响上下文。
                    result_str = self._truncate_tool_result_for_context(result_str, function_name)

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
                            "fetch_url": 3000,
                            "read_file": 3000,
                            "write_file": 2000,
                            "edit_file": 2000,
                            "apply_patch": 2000,
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
                                # (-3 = user interjection; -2 = assistant tool_call; -1 = tool result)
                                clean_msg = jr.get("response", "") or "已收到"
                                self.messages[-3]["content"] = f"[用户插入已接受] {clean_msg}"
                                if verbose:
                                    print(f"[Agent] ✅ Interjection accepted: {clean_msg[:60]}")
                            elif action == "reject":
                                self.pending_messages.pop(0)
                                reason = jr.get("reason", "")
                                self._rejected_interjection = {
                                    "message": self.messages[-3].get("content", ""),
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

                    # Collect screenshot/image data for vision injection
                    # (extraction + placeholder replacement happen right after
                    # `result_str = str(result)` above; see that block)

                # After all tool results in this iteration, inject screenshot vision observations
                for url, caption in screenshot_urls:
                    self.messages.append({
                        "role": "user",
                        "content": [
                            {"type": "text", "text": caption},
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
                    # 无条件写回：time_based_microcompact 会为缺失的消息补
                    # _timestamp——第一轮写回时间戳，TTL 过后第二轮才能据此
                    # 识别冷区并清理老旧工具结果。恢复路径对 _timestamp 的
                    # 剥离逻辑保持不变（api/background.py、api/ws.py）。
                    if compacted is not None and compacted is not self.messages:
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
                # Defer reflection + auto-tool to the serial post-process worker
                self._enqueue_post_process(self._reflection_task_input, _time.time() - _task_start, True)
                # If there are rejected interjections, attach them to the response
                if self._rejected_interjection:
                    import json as _rj
                    reject_data = self._rejected_interjection
                    self._rejected_interjection = None
                    return f"[INTERJECTION_REJECTED] {_rj.dumps(reject_data, ensure_ascii=False)}\n{final_answer}"
                return final_answer

        # KG extraction + stats + post-process even on failure
        self._finalize_failed_turn(user_input, current_iter, _time.time() - _task_start)
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

        Polls the queue with a 1s timeout so task interrupts are honored
        (a stopped task never leaks this thread). After a total timeout
        (default 1800s, overridable via ``self._user_input_timeout``) the
        task is NOT killed: raises ``TaskPaused`` so run_turn moves the task
        to background-paused — a late answer via WS tool_reply or
        POST /api/tasks/{id}/reply injects it into the context and resumes.
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

        # Block until the websocket sends a response, waking periodically to
        # check for interrupts.
        total_timeout = getattr(self, "_user_input_timeout", 1800.0)
        deadline = _time.time() + total_timeout
        while True:
            if getattr(self, "is_interrupted", False):
                return "[用户已中断任务]"
            try:
                return self.user_input_queue.get(timeout=1.0)
            except queue.Empty:
                if _time.time() >= deadline:
                    # Total timeout — pause to background (same flow as the
                    # sandbox-auth timeout) instead of killing the task. The
                    # question is embedded so a late answer can be matched to
                    # it when the task is resumed.
                    raise TaskPaused(
                        "等待用户回答超时，任务转入后台挂起。"
                        f"请回答此前的问题后任务自动恢复。问题: {str(question)[:120]}")
