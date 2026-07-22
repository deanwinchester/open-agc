# -*- coding: utf-8 -*-
"""Measure per-tool OpenAI schema bytes to size the tiered core set."""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.shell import ShellTool
from tools.filesystem import ReadFileTool, WriteFileTool, EditFileTool
from tools.search import GrepSearchTool, GlobTool
from tools.python_repl import PythonREPLTool
from tools.computer import ComputerTool
from tools.memory import MemoryTool
from tools.web_search import WebSearchTool
from tools.system_mac import MacSystemTool
from tools.save_skill import SaveSkillTool
from tools.download import DownloadTool
from tools.email_tool import SearchEmailTool, SendEmailTool
from tools.interaction import (AskUserQuestionTool, PauseAndWaitTool,
                               SearchHistoryTool, UserInterjectionResponseTool)
from tools.shell_interact import ShellSendTool
from tools.sandbox import EnterWorktreeTool, ExitWorktreeTool
from tools.self_review import SelfReviewTool
from tools.task_plan import TaskPlanTool
from tools.task_manager import TaskManagerTool
from tools.system_config import ConfigureSystemTool
from tools.plugin_dev import DevelopPluginTool
from tools.compact_context import CompactContextTool

tools = {
    "execute_shell": ShellTool(),
    "read_file": ReadFileTool(),
    "write_file": WriteFileTool(),
    "edit_file": EditFileTool(),
    "search_file_content": GrepSearchTool(),
    "find_files": GlobTool(),
    "execute_python": PythonREPLTool(),
    "computer_control": ComputerTool(),
    "search_web": WebSearchTool(),
    "mac_system_action": MacSystemTool(),
    "save_learned_skill": SaveSkillTool(),
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
    "compact_context": CompactContextTool(),
}
try:
    from core.paths import get_data_path
    tools["manage_memory"] = MemoryTool(db_path=get_data_path("memory.db"), session_id=0)
except Exception as e:
    print("manage_memory skipped:", e)
try:
    tools["browser_automation"] = BrowserAutomationTool(headless=True)
except Exception as e:
    print("browser_automation skipped:", e)
try:
    from tools.reader_lm import ReaderLMTool
    if ReaderLMTool.is_available():
        tools["parse_html"] = ReaderLMTool()
except Exception as e:
    print("parse_html skipped:", e)

sizes = {}
for name, tool in tools.items():
    try:
        schema = tool.get_openai_schema()
        sizes[name] = len(json.dumps(schema, ensure_ascii=False).encode("utf-8"))
    except Exception as e:
        print(f"{name}: ERROR {e}")

for name, size in sorted(sizes.items(), key=lambda x: -x[1]):
    print(f"{size:6d}  {name}")
print(f"{sum(sizes.values()):6d}  TOTAL ({len(sizes)} tools)")

CORE = ["read_file", "write_file", "edit_file", "execute_shell", "execute_python",
        "search_file_content", "find_files", "search_web", "ask_user_question",
        "self_review", "user_interjection_response"]
core_bytes = sum(sizes.get(n, 0) for n in CORE) + 500
print(f"\nCandidate core ({len(CORE)} + discovery): ~{core_bytes} bytes")
