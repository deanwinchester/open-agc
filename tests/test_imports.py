"""
Smoke tests: verify that all core modules import cleanly
without requiring API keys or external services.
"""
import sys
import os
import platform
import pytest

_HAS_LITELLM = False
_HAS_BS4 = False
try:
    import litellm  # noqa: F401
    _HAS_LITELLM = True
except ImportError:
    pass
try:
    import bs4  # noqa: F401
    _HAS_BS4 = True
except ImportError:
    pass

_litellm = pytest.mark.skipif(not _HAS_LITELLM, reason="requires litellm")
_bs4 = pytest.mark.skipif(not _HAS_BS4, reason="requires bs4 / beautifulsoup4")


def test_core_paths_import():
    """core/paths.py — foundational, no deps beyond os."""
    from core.paths import get_data_path, get_data_dir, get_skills_dir, get_bin_dir
    assert callable(get_data_path)
    assert callable(get_data_dir)
    assert callable(get_skills_dir)
    assert callable(get_bin_dir)


def test_core_version_import():
    """core/version.py — no external deps."""
    from core.version import get_version
    v = get_version()
    assert isinstance(v, str)
    assert v.count(".") >= 2  # semver-like


def test_core_logger_import():
    """core/logger.py — lightweight wrapper."""
    from core.logger import SessionLogger
    assert SessionLogger


def test_core_stats_manager_import():
    """core/stats_manager.py — singleton pattern."""
    from core.stats_manager import get_stats_manager
    mgr = get_stats_manager()
    assert mgr is not None


def test_core_token_budget_import():
    """core/token_budget.py — context budget logic."""
    from core.token_budget import TokenBudget, estimate_messages_tokens
    assert TokenBudget
    assert callable(estimate_messages_tokens)


def test_tools_base_import():
    """tools/base.py — BaseTool abstract class."""
    from tools.base import BaseTool
    assert BaseTool
    assert hasattr(BaseTool, "get_openai_schema")
    assert hasattr(BaseTool, "execute")


def test_tools_filesystem_import():
    """tools/filesystem.py — read/write tools."""
    from tools.filesystem import ReadFileTool, WriteFileTool, EditFileTool
    assert ReadFileTool
    assert WriteFileTool
    assert EditFileTool


def test_tools_shell_import():
    """tools/shell.py — shell execution tool."""
    from tools.shell import ShellTool
    assert ShellTool


def test_tools_search_import():
    """tools/search.py — grep and glob tools."""
    from tools.search import GrepSearchTool, GlobTool
    assert GrepSearchTool
    assert GlobTool


def test_tools_python_repl_import():
    """tools/python_repl.py — Python REPL tool."""
    from tools.python_repl import PythonREPLTool
    assert PythonREPLTool


def test_tools_memory_import():
    """tools/memory.py — memory management tool."""
    from tools.memory import MemoryTool
    assert MemoryTool


@_bs4
def test_tools_web_search_import():
    """tools/web_search.py — web search tool."""
    from tools.web_search import WebSearchTool
    assert WebSearchTool


@_bs4
def test_tools_email_import():
    """tools/email_tool.py — email tool."""
    from tools.email_tool import SearchEmailTool, SendEmailTool
    assert SearchEmailTool
    assert SendEmailTool


def test_tools_compact_context_import():
    """tools/compact_context.py — context compaction."""
    from tools.compact_context import CompactContextTool
    assert CompactContextTool


def test_tools_discovery_import():
    """tools/discovery.py — tool discovery."""
    from tools.discovery import ToolDiscoveryTool
    assert ToolDiscoveryTool


def test_tools_self_review_import():
    """tools/self_review.py — self-review tool."""
    from tools.self_review import SelfReviewTool
    assert SelfReviewTool


def test_tools_sandbox_import():
    """tools/sandbox.py — sandbox tools."""
    from tools.sandbox import EnterWorktreeTool, ExitWorktreeTool
    assert EnterWorktreeTool
    assert ExitWorktreeTool


def test_tools_interaction_import():
    """tools/interaction.py — user interaction tools."""
    from tools.interaction import AskUserQuestionTool, PauseAndWaitTool, SearchHistoryTool
    assert AskUserQuestionTool
    assert PauseAndWaitTool
    assert SearchHistoryTool


def test_tools_mcp_import():
    """tools/mcp_tool.py — MCP integration."""
    from tools.mcp_tool import get_mcp_manager
    assert callable(get_mcp_manager)


@_litellm
def test_agent_import():
    """agent/agent.py — the main agent class (no API call)."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from agent.agent import OpenAGCAgent
    assert OpenAGCAgent


def test_agent_sub_agent_import():
    """agent/sub_agent.py — sub-agent delegation module."""
    from agent.sub_agent import SubAgent, TOOL_SETS
    assert SubAgent
    assert isinstance(TOOL_SETS, dict)
    assert "filesystem" in TOOL_SETS


def test_server_imports():
    """api/server.py — FastAPI app (no startup)."""
    # Only test that the module can be parsed/imported without running
    import importlib
    spec = importlib.util.find_spec("api.server")
    assert spec is not None, "api.server module not found"


def test_ws_imports():
    """api/ws.py — WebSocket handler module exists."""
    import importlib
    spec = importlib.util.find_spec("api.ws")
    assert spec is not None, "api.ws module not found"
