"""
Tests for tool schema generation and basic tool behavior.
Each test instantiates a tool and verifies its OpenAI function-calling schema.
"""
import os
import sys
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ── Dependency guards ──
_HAS_LITELLM = False
_HAS_BS4 = False
_HAS_PYAUTOGUI = False
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
try:
    import pyautogui  # noqa: F401
    _HAS_PYAUTOGUI = True
except ImportError:
    pass

_litellm = pytest.mark.skipif(not _HAS_LITELLM, reason="requires litellm")
_bs4 = pytest.mark.skipif(not _HAS_BS4, reason="requires bs4 / beautifulsoup4")
_pyautogui = pytest.mark.skipif(not _HAS_PYAUTOGUI, reason="requires pyautogui")


def _validate_schema(schema):
    """Shared schema validation helper.

    Tools may return either a flat schema:
        {"name": "...", "description": "...", "parameters": {...}}
    or an OpenAI wrapper:
        {"type": "function", "function": {"name": "...", ...}}
    """
    assert isinstance(schema, dict), f"Schema must be dict, got {type(schema)}"
    if "type" in schema and schema.get("type") == "function" and "function" in schema:
        fn = schema["function"]
        assert "name" in fn, "Wrapped schema missing 'name'"
        assert "description" in fn, "Wrapped schema missing 'description'"
        assert "parameters" in fn, "Wrapped schema missing 'parameters'"
        assert fn["parameters"].get("type") == "object"
        assert isinstance(fn["name"], str) and len(fn["name"]) > 0
        return fn
    else:
        assert "name" in schema, "Schema missing 'name'"
        assert "description" in schema, "Schema missing 'description'"
        assert "parameters" in schema, "Schema missing 'parameters'"
        assert schema["parameters"]["type"] == "object"
        assert "properties" in schema["parameters"]
        assert isinstance(schema["name"], str) and len(schema["name"]) > 0
        assert isinstance(schema["description"], str) and len(schema["description"]) > 0
        return schema


class TestShellTool:
    def test_schema_structure(self):
        from tools.shell import ShellTool
        tool = ShellTool()
        fn = _validate_schema(tool.get_openai_schema())
        assert "command" in fn["parameters"]["properties"]

    def test_schema_has_timeout_param(self):
        from tools.shell import ShellTool
        tool = ShellTool()
        fn = _validate_schema(tool.get_openai_schema())
        props = fn["parameters"]["properties"]
        assert any("timeout" in k for k in props)


class TestFilesystemTools:
    def test_read_file_schema(self):
        from tools.filesystem import ReadFileTool
        fn = _validate_schema(ReadFileTool().get_openai_schema())
        props = fn["parameters"]["properties"]
        # Tool uses "path" as the key name
        assert any(k in props for k in ("file_path", "path"))

    def test_write_file_schema(self):
        from tools.filesystem import WriteFileTool
        fn = _validate_schema(WriteFileTool().get_openai_schema())
        props = fn["parameters"]["properties"]
        assert any(k in props for k in ("file_path", "path"))
        assert "content" in props

    def test_edit_file_schema(self):
        from tools.filesystem import EditFileTool
        fn = _validate_schema(EditFileTool().get_openai_schema())
        props = fn["parameters"]["properties"]
        assert any(k in props for k in ("file_path", "path"))


class TestPythonREPLTool:
    def test_schema_structure(self):
        from tools.python_repl import PythonREPLTool
        fn = _validate_schema(PythonREPLTool().get_openai_schema())
        assert "code" in fn["parameters"]["properties"]


class TestMemoryTool:
    def test_schema_structure(self):
        from tools.memory import MemoryTool
        fn = _validate_schema(MemoryTool().get_openai_schema())
        assert "action" in fn["parameters"]["properties"]


class TestGrepSearchTool:
    def test_schema_structure(self):
        from tools.search import GrepSearchTool
        fn = _validate_schema(GrepSearchTool().get_openai_schema())
        assert "pattern" in fn["parameters"]["properties"]


class TestGlobTool:
    def test_schema_structure(self):
        from tools.search import GlobTool
        fn = _validate_schema(GlobTool().get_openai_schema())
        assert "pattern" in fn["parameters"]["properties"]


class TestCompactContextTool:
    def test_schema_structure(self):
        from tools.compact_context import CompactContextTool
        _validate_schema(CompactContextTool().get_openai_schema())


class TestSelfReviewTool:
    def test_schema_structure(self):
        from tools.self_review import SelfReviewTool
        _validate_schema(SelfReviewTool().get_openai_schema())


class TestInteractionTools:
    def test_ask_user_schema(self):
        from tools.interaction import AskUserQuestionTool
        _validate_schema(AskUserQuestionTool().get_openai_schema())

    def test_search_history_schema(self):
        from tools.interaction import SearchHistoryTool
        _validate_schema(SearchHistoryTool().get_openai_schema())


class TestConfigureSystemTool:
    @_litellm
    def test_schema_structure(self):
        from tools.system_config import ConfigureSystemTool
        _validate_schema(ConfigureSystemTool().get_openai_schema())


class TestDynamicTool:
    def test_dynamic_tool_wrapper(self):
        """DynamicTool wraps a schema + execute function."""
        from tools.auto_tool import DynamicTool

        def execute(**kwargs) -> str:
            msg = kwargs.get("msg", "")
            return f"echo: {msg}"

        tool = DynamicTool(
            name="test_tool",
            description="A test tool",
            tool_schema={
                "name": "test_tool",
                "description": "A test tool",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "msg": {"type": "string", "description": "A message"}
                    },
                    "required": ["msg"],
                },
            },
            fn=execute,
        )
        schema = tool.get_openai_schema()
        assert schema["function"]["name"] == "test_tool"
        assert "parameters" in schema["function"]
        result = tool.execute(msg="hello")
        assert result == "echo: hello"


@_litellm
class TestWebSearchTool:
    def test_schema_structure(self):
        from tools.web_search import WebSearchTool
        fn = _validate_schema(WebSearchTool().get_openai_schema())
        assert "query" in fn["parameters"]["properties"]


@_pyautogui
class TestComputerTool:
    def test_schema_structure(self):
        from tools.computer import ComputerTool
        _validate_schema(ComputerTool().get_openai_schema())
