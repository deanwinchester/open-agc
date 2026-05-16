"""
AutoTool — Dynamic tool generation, registration, and execution.

Converts successful task trajectories into reusable Python tools
that can be dynamically registered and called by the LLM.
"""
import json
import os
import importlib.util
import re
from typing import Dict, Any, Optional, Callable
from tools.base import BaseTool


class DynamicTool(BaseTool):
    """A tool created at runtime from a schema and execute function."""

    name: str = "dynamic_tool"
    description: str = "A dynamically generated tool"
    tool_schema: Dict = {}
    fn: Callable = None

    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.tool_schema.get("parameters", {
                    "type": "object",
                    "properties": {},
                    "required": [],
                }),
            },
        }

    def execute(self, **kwargs) -> str:
        if not self.fn:
            return "Error: No execute function bound"
            
        import inspect
        try:
            sig = inspect.signature(self.fn)
            # Only pass arguments that the function accepts
            valid_args = {}
            for k, v in kwargs.items():
                if k in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                    valid_args[k] = v
            return self.fn(**valid_args)
        except Exception as e:
            return f"Error executing dynamic tool: {str(e)}"


# Tools directory for persisted auto-generated tools
AUTO_TOOLS_DIR = None  # Set via init


def init_auto_tools(tools_dir: str):
    """Set the auto-generated tools directory."""
    global AUTO_TOOLS_DIR
    AUTO_TOOLS_DIR = tools_dir
    os.makedirs(tools_dir, exist_ok=True)


def generate_tool_code(task_input: str, tool_sequence: str,
                       result_summary: str, llm_client) -> Optional[str]:
    """Use the LLM to synthesize a reusable Python tool from a successful trajectory.

    Returns the generated Python source code as a string, or None on failure.
    """
    prompt = f"""You are a tool generator. Convert this successful agent task into a reusable Python tool.

Task: {task_input[:300]}
Execution Steps:
{tool_sequence[:2000]}

Result: {result_summary[:500]}

Generate a Python file with:
1. A TOOL_SCHEMA dict (OpenAI function-calling format) with a clear name, description, and typed parameters
2. An execute() function that performs the task. The signature MUST be `def execute(..., **kwargs):`.
3. Minimal required parameters — extract concrete values as parameters. Always include `**kwargs` to handle system arguments.

Rules:
- The function should be self-contained, using only standard library or common packages
- Include error handling
- Return a string result
- The "name" in TOOL_SCHEMA must be a valid Python identifier (snake_case)
- Do NOT include any subprocess calls without proper error handling

Output ONLY the Python code, no explanation."""

    try:
        response, _ = llm_client.chat(
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.choices[0].message.content.strip()

        # Extract code from markdown fences if present
        code_match = re.search(r'```python\s*\n?(.*?)\n?```', text, re.DOTALL)
        if code_match:
            text = code_match.group(1).strip()

        # Basic validation
        if "TOOL_SCHEMA" not in text or "def execute" not in text:
            print(f"[AutoTool] Generated code missing TOOL_SCHEMA or execute(): {text[:200]}")
            return None

        return text
    except Exception as e:
        print(f"[AutoTool] Generation failed: {e}")
        return None


def validate_tool_code(code: str) -> bool:
    """Validate generated tool code for safety and correctness."""
    # Check for dangerous patterns
    dangerous_patterns = [
        r"rm\s+-rf\s+/",
        r"os\.remove\(['\"]/",  # Removing root files
        r"shutil\.rmtree\(['\"]/",
        r"__import__\(['\"]os['\"]\)\.system",
        r"eval\(.*request",
        r"exec\(.*request",
    ]
    for pattern in dangerous_patterns:
        if re.search(pattern, code):
            print(f"[AutoTool] Dangerous pattern detected: {pattern}")
            return False

    # Verify the code is syntactically valid Python
    try:
        compile(code, "<auto_tool>", "exec")
    except SyntaxError as e:
        print(f"[AutoTool] Syntax error in generated code: {e}")
        return False

    return True


def save_tool_code(code: str, name: str) -> Optional[str]:
    """Save generated tool code to the auto-tools directory.

    Returns the file path if successful, None otherwise.
    """
    if not AUTO_TOOLS_DIR:
        return None

    # Sanitize the filename
    safe_name = re.sub(r'[^a-zA-Z0-9_]', '', name)
    if not safe_name:
        safe_name = "auto_tool"
    filepath = os.path.join(AUTO_TOOLS_DIR, f"{safe_name}.py")

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(code)
        return filepath
    except Exception as e:
        print(f"[AutoTool] Save failed: {e}")
        return None


def load_dynamic_tool(filepath: str) -> Optional[DynamicTool]:
    """Dynamically load a tool module and return a DynamicTool instance."""
    try:
        spec = importlib.util.spec_from_file_location("dynamic_tool", filepath)
        if not spec or not spec.loader:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        if not hasattr(mod, "TOOL_SCHEMA") or not hasattr(mod, "execute"):
            print(f"[AutoTool] Module missing TOOL_SCHEMA or execute: {filepath}")
            return None

        tool = DynamicTool(
            name=mod.TOOL_SCHEMA["name"],
            description=mod.TOOL_SCHEMA.get("description", ""),
            tool_schema=mod.TOOL_SCHEMA,
            fn=mod.execute,
        )
        return tool
    except Exception as e:
        print(f"[AutoTool] Load failed for {filepath}: {e}")
        return None


def load_all_dynamic_tools(tools_dir: str) -> Dict[str, DynamicTool]:
    """Scan the auto-tools directory and load all tools."""
    tools = {}
    if not os.path.isdir(tools_dir):
        return tools

    for fname in os.listdir(tools_dir):
        if fname.endswith(".py") and fname != "__init__.py":
            filepath = os.path.join(tools_dir, fname)
            tool = load_dynamic_tool(filepath)
            if tool:
                tools[tool.name] = tool
    return tools
