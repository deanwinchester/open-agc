"""
AutoTool — Dynamic tool generation, registration, and execution.

Converts successful task trajectories into reusable Python tools
that can be dynamically registered and called by the LLM.
"""
import ast
import json
import os
import importlib.util
import re
import shutil
import threading
from datetime import datetime
from typing import Dict, Any, List, Optional, Callable
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


# Tools directory for persisted auto-generated tools. Legacy load-path marker
# only — write paths (save_tool_code) take an explicit tools_dir and must NOT
# rely on this global (multiple session agents in one process would otherwise
# save into whichever directory init_auto_tools ran last for).
AUTO_TOOLS_DIR = None  # Set via init


def init_auto_tools(tools_dir: str):
    """Create the auto-generated tools directory (load-path initialization).

    Still records AUTO_TOOLS_DIR for backward compatibility, but saving uses
    the explicit ``tools_dir`` argument of save_tool_code.
    """
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


# ── Safety whitelist for generated tool code (AST analysis, see validate_tool_code) ──
#
# Modules that generated code may NEVER import: process execution, system
# control, raw network, FFI/low-level memory, dynamic loading.
_BLOCKED_MODULES = {
    # process execution / system control / dynamic loading
    "subprocess", "sys", "signal", "multiprocessing", "runpy", "importlib",
    "code", "codeop", "pty",
    # raw network (requests with whitelisted methods is the sanctioned way out)
    "socket", "ftplib", "smtplib", "telnetlib", "http", "urllib.request",
    # low-level memory / FFI
    "ctypes", "mmap",
    # the builtins module re-exposes everything the name/attribute checks
    # block (builtins.exec, builtins.open('/etc/passwd'), ...). Generated
    # code never needs to import it explicitly — builtins are ambient.
    "builtins", "__builtin__",
}

# Modules that MAY be imported, but only the listed members may be used.
# Anything not listed (e.g. os.system, os.remove, shutil.rmtree) is rejected.
_RESTRICTED_MODULE_MEMBERS = {
    "os": {
        "path",       # os.path.* — joins, exists, basename, ...
        "listdir", "makedirs", "getcwd", "walk",
        "environ",    # read access to env vars (documented: values could leak)
        "sep", "name", "linesep",
    },
    "requests": {
        "get", "post", "put", "patch", "delete", "head", "options",
        "Session", "exceptions",
    },
    "shutil": {
        "copy", "copy2", "copyfile", "copytree", "move", "which",
        "disk_usage", "make_archive", "unpack_archive",
    },
    # io.open / codecs.open are alternates to builtin open() — they get the
    # same path/mode rules (see _validate_open_call). The rest of each
    # whitelist is pure in-memory / codec work.
    "io": {
        "open", "StringIO", "BytesIO", "TextIOBase", "TextIOWrapper",
        "BufferedReader", "BufferedWriter", "FileIO",
        "SEEK_SET", "SEEK_CUR", "SEEK_END", "DEFAULT_BUFFER_SIZE",
    },
    "codecs": {
        "open", "encode", "decode", "lookup", "register",
        "getreader", "getwriter", "getencoder", "getdecoder",
        "iterencode", "iterdecode",
    },
}

# pathlib constructor names whose literal absolute paths are rejected
# (Path('/etc/passwd').read_text() / .write_text(...) etc.).
_PATH_CONSTRUCTOR_NAMES = {"Path", "PosixPath", "WindowsPath"}

# Builtins/names that must never appear in generated code: dynamic evaluation
# and introspection that would bypass the attribute whitelist.
_BLOCKED_NAMES = {
    "eval", "exec", "__import__", "compile",
    "getattr", "setattr", "delattr", "globals", "locals", "vars",
    "breakpoint", "exit", "quit", "input",
    "__builtins__",
}

# Dunder attributes that are safe to reference; all others are rejected
# (blocks `__class__`/`__subclasses__`/`__globals__` sandbox escapes).
_ALLOWED_DUNDER_ATTRS = {
    "__init__", "__name__", "__doc__", "__main__",
    "__version__", "__all__", "__file__",
}


def _is_absolute_path_literal(p: str) -> bool:
    """Detect absolute paths in string literals, cross-platform.

    Covers POSIX (`/x`), Windows drive (`C:\\x`, `C:/x`), UNC (`\\\\srv`) and
    home-relative (`~/x`) forms.
    """
    return (
        p.startswith("/")
        or p.startswith("\\")
        or p.startswith("~")
        or bool(re.match(r'^[a-zA-Z]:[\\/]', p))
    )


def _validate_open_call(node: "ast.Call") -> Optional[str]:
    """Check an open() call. Returns a rejection reason, or None if acceptable.

    Rules:
      - Literal absolute path (any mode)            -> reject
      - Write/append/exclusive mode (`w`/`a`/`x`/`+`) with a literal
        relative path                               -> allow
      - Write mode with a non-literal path          -> allow (path cannot be
        resolved statically; documented residual risk)
      - Non-literal mode expression                 -> reject (unanalyzable)
    """
    if not node.args:
        return None
    path_arg = node.args[0]
    # mode: second positional arg or keyword mode=
    mode_arg = node.args[1] if len(node.args) > 1 else None
    for kw in node.keywords:
        if kw.arg == "mode":
            mode_arg = kw.value
    is_literal_path = isinstance(path_arg, ast.Constant) and isinstance(path_arg.value, str)
    if is_literal_path and _is_absolute_path_literal(path_arg.value):
        return f"open() with absolute path {path_arg.value!r}"
    if mode_arg is not None:
        if not (isinstance(mode_arg, ast.Constant) and isinstance(mode_arg.value, str)):
            return "open() with non-literal mode (cannot be verified)"
        if any(c in mode_arg.value for c in "wax+"):
            # Write mode: literal relative path is fine; non-literal path is
            # allowed but noted (see module docstring of validate_tool_code).
            return None
    return None


def _is_module_target(dotted: str) -> bool:
    """True if `dotted` refers to a blocked or restricted module itself."""
    return (
        dotted in _RESTRICTED_MODULE_MEMBERS
        or dotted in _BLOCKED_MODULES
        or dotted.split(".")[0] in _BLOCKED_MODULES
    )


def _is_path_constructor(func, aliases: Dict[str, str]) -> bool:
    """True if a Call's func is a pathlib Path/PosixPath/WindowsPath constructor.

    Only recognised when traceable to pathlib: `from pathlib import Path`
    (alias resolves to pathlib.Path) or `pathlib.Path` / `pl.Path` attribute
    form. A bare `Path(...)` with no pathlib import is left alone — it may be
    a user-defined name.
    """
    if isinstance(func, ast.Name):
        resolved = aliases.get(func.id, "")
        return resolved in {f"pathlib.{n}" for n in _PATH_CONSTRUCTOR_NAMES}
    if isinstance(func, ast.Attribute) and func.attr in _PATH_CONSTRUCTOR_NAMES \
            and isinstance(func.value, ast.Name):
        return aliases.get(func.value.id, func.value.id) == "pathlib"
    return False


def _validate_path_constructor(node: "ast.Call") -> Optional[str]:
    """Reject pathlib Path()/PosixPath()/WindowsPath() with a literal absolute
    path — covers `.read_text()` reads and `.write_text()/.write_bytes()/
    .open('w')` writes in one place. Non-literal paths cannot be resolved
    statically and are a documented residual risk (same policy as open())."""
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str) and \
            _is_absolute_path_literal(first.value):
        return f"pathlib {node.func.attr if isinstance(node.func, ast.Attribute) else 'Path'}() " \
               f"with absolute path {first.value!r}"
    return None


def validate_tool_code(code: str) -> bool:
    """Validate generated tool code for safety via AST analysis.

    Replaces the old regex blocklist (trivially bypassed with string
    concatenation, f-strings or getattr). The code is parsed and every node
    is walked; enforcement is by whitelist, documented at module level:

      1. Imports: blocked modules rejected outright; restricted modules may
         only expose whitelisted members; relative and star imports rejected.
         Everything else (pure-computation stdlib / common packages) is allowed.
      2. Dangerous builtins (eval/exec/__import__/compile/getattr/...) rejected
         as names anywhere, including inside f-strings.
      3. Dunder attribute access rejected except a small safe set
         (blocks `().__class__.__bases__[0].__subclasses__()` escapes).
      4. open() restricted (see _validate_open_call); io.open/codecs.open get
         the same rules; pathlib Path()/PosixPath()/WindowsPath() with a
         literal absolute path is rejected (covers read_text/write_text/
         write_bytes/open('w') through the constructor).
      5. Direct module aliasing (`x = os`) rejected — it would re-root a
         restricted/blocked module under an unchecked variable name.
      6. The `builtins` module itself is a blocked import (it re-exposes
         exec/eval/open under an attribute root).
      7. Syntax errors rejected (ast.parse).

    Residual risks (accepted, documented): non-literal paths in write-mode
    open()/io.open/codecs.open and in pathlib constructors; indirect module
    flows (modules stashed in containers, tuple unpacking, attribute chains
    such as `os.path.os`); allowed modules used maliciously. The generated
    tool runs with full user privileges; this validation blocks obvious
    escape hatches, it is not a complete sandbox.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        print(f"[AutoTool] Syntax error in generated code: {e}")
        return False
    except Exception as e:
        print(f"[AutoTool] Unparseable code: {e}")
        return False

    def _reject(reason: str) -> bool:
        print(f"[AutoTool] Code rejected: {reason}")
        return False

    def _module_check(dotted: str) -> Optional[str]:
        """Return rejection reason if module `dotted` may not be imported."""
        root = dotted.split(".")[0]
        if root in _BLOCKED_MODULES or dotted in _BLOCKED_MODULES:
            return f"import of blocked module '{dotted}'"
        # e.g. urllib.request blocked while urllib.parse allowed
        for blocked in _BLOCKED_MODULES:
            if dotted.startswith(blocked + "."):
                return f"import of blocked module '{dotted}'"
        return None

    # Pass 1: collect import aliases {local_name: dotted_target}
    aliases: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                reason = _module_check(a.name)
                if reason:
                    return _reject(reason)
                if a.asname:
                    aliases[a.asname] = a.name
                else:
                    # `import a.b` binds the top-level name `a` (full module a)
                    aliases[a.name.split(".")[0]] = a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                return _reject("relative imports are not allowed")
            mod = node.module or ""
            reason = _module_check(mod)
            if reason:
                return _reject(reason)
            for a in node.names:
                if a.name == "*":
                    return _reject(f"star import from '{mod}' is not allowed")
                if mod in _RESTRICTED_MODULE_MEMBERS and \
                        a.name not in _RESTRICTED_MODULE_MEMBERS[mod]:
                    return _reject(f"'{mod}.{a.name}' is not a whitelisted member")
                aliases[a.asname or a.name] = f"{mod}.{a.name}" if mod else a.name

    # Pass 2: walk every node (covers f-strings, decorators, nested funcs...)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id in _BLOCKED_NAMES:
                return _reject(f"use of blocked builtin '{node.id}'")
        elif isinstance(node, ast.Attribute):
            # Block dunder attribute traversal except safe constants
            if node.attr.startswith("__") and node.attr.endswith("__") and \
                    node.attr not in _ALLOWED_DUNDER_ATTRS:
                return _reject(f"dunder attribute access '{node.attr}'")
            # Enforce member whitelist on restricted modules:
            # only the attribute directly hanging off the module root needs
            # checking (e.g. `os.system` -> attr 'system' on root 'os').
            if isinstance(node.value, ast.Name):
                target = aliases.get(node.value.id, node.value.id)
                if target in _RESTRICTED_MODULE_MEMBERS and \
                        node.attr not in _RESTRICTED_MODULE_MEMBERS[target]:
                    return _reject(f"'{target}.{node.attr}' is not a whitelisted member")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            # Direct module alias: `x = os` would re-root the module under a
            # plain variable and slip past the per-attribute whitelist
            # (`x.system(...)` — root 'x' doesn't resolve to 'os'). Only this
            # direct form is covered; indirect flows (modules stashed in
            # containers, tuple unpacking, attribute chains like os.path.os)
            # are documented residual risks.
            value = node.value
            if isinstance(value, ast.Name):
                target = aliases.get(value.id, value.id)
                if _is_module_target(target):
                    return _reject(f"aliasing module '{target}' to a variable is not allowed")
        elif isinstance(node, ast.Call):
            func = node.func
            reason = None
            if isinstance(func, ast.Name):
                resolved = aliases.get(func.id)
                if func.id == "open" and resolved is None:
                    # builtin open(...)
                    reason = _validate_open_call(node)
                elif resolved in ("io.open", "codecs.open"):
                    # from io/codecs import open
                    reason = _validate_open_call(node)
                elif _is_path_constructor(func, aliases):
                    reason = _validate_path_constructor(node)
            elif isinstance(func, ast.Attribute):
                if func.attr == "open" and isinstance(func.value, ast.Name) and \
                        aliases.get(func.value.id, func.value.id) in ("io", "codecs"):
                    # io.open(...) / codecs.open(...)
                    reason = _validate_open_call(node)
                elif _is_path_constructor(func, aliases):
                    reason = _validate_path_constructor(node)
            if reason:
                return _reject(reason)

    return True


def save_tool_code(code: str, name: str, tools_dir: str) -> Optional[str]:
    """Save generated tool code into ``tools_dir``.

    The directory is an explicit parameter (not the AUTO_TOOLS_DIR global) so
    concurrent session agents never save into each other's directory — the
    trust file and the tool source must stay co-located for graduation.

    Returns the file path if successful, None otherwise.
    """
    if not tools_dir:
        return None

    # Sanitize the filename
    safe_name = re.sub(r'[^a-zA-Z0-9_]', '', name)
    if not safe_name:
        safe_name = "auto_tool"
    filepath = os.path.join(tools_dir, f"{safe_name}.py")

    try:
        os.makedirs(tools_dir, exist_ok=True)
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
    """Scan the auto-tools directory and load all tools.

    Skips ``_archive`` (pruned tools, see prune_auto_tools), ``__pycache__``,
    dotfiles and anything that is not a plain ``.py`` file.
    """
    tools = {}
    if not os.path.isdir(tools_dir):
        return tools

    for fname in os.listdir(tools_dir):
        if fname.startswith("_") or fname.startswith("."):
            continue  # _archive, __pycache__, _trust.json, dotfiles
        if not fname.endswith(".py"):
            continue
        filepath = os.path.join(tools_dir, fname)
        if not os.path.isfile(filepath):
            continue
        tool = load_dynamic_tool(filepath)
        if tool:
            tools[tool.name] = tool
    return tools


# ── Trajectory classification & pre-generation reusability gate ──

# A trajectory dominated by execute_shell/execute_python is deterministic
# command/script work — suitable to固化成工具. One dominated by read/search
# tools is exploratory (context-specific, one-off) and must not be固化.
_DETERMINISTIC_TOOLS = {"execute_shell", "execute_python"}

# Minimum tool calls in a successful trajectory before generation is considered.
MIN_TOOL_CALLS_FOR_GENERATION = 5


def parse_tool_names(tool_sequence: str) -> List[str]:
    """Extract ordered tool names from a rendered trajectory ("→ name: detail" lines)."""
    return re.findall(r'→\s*([A-Za-z_][A-Za-z0-9_]*)\s*:', tool_sequence)


def classify_trajectory(tool_sequence: str) -> str:
    """Classify a trajectory as "deterministic" or "exploratory".

    deterministic: ≥50% of calls are execute_shell/execute_python.
    exploratory:   anything else (read/search-dominated or unparseable).
    """
    names = parse_tool_names(tool_sequence)
    if not names:
        return "exploratory"
    det = sum(1 for n in names if n in _DETERMINISTIC_TOOLS)
    return "deterministic" if det / len(names) >= 0.5 else "exploratory"


def assess_reusability(task_input: str, tool_sequence: str,
                       existing_tools: Dict[str, str], llm_client) -> dict:
    """One lightweight LLM call: is this trajectory worth turning into a tool?

    ``existing_tools`` maps tool name → description for already-loaded auto
    tools (dedup context). Returns a dict with keys ``reusable`` (bool),
    ``reason`` (str), ``suggested_name`` (str), ``overlap_with`` (str|None).
    Fail-closed: any error yields reusable=False so a flaky LLM never
    re-opens the generation floodgate.
    """
    existing_desc = "\n".join(
        f"- {name}: {desc}" for name, desc in list(existing_tools.items())[:30]
    ) or "(none)"
    prompt = f"""Judge whether this successful agent task trajectory should become a reusable tool.

Task: {task_input[:200]}
Trajectory:
{tool_sequence[:1500]}

Existing auto-generated tools:
{existing_desc}

Answer with ONLY a JSON object, no other text:
{{"reusable": true/false, "reason": "short reason", "suggested_name": "snake_case_name", "overlap_with": "existing_tool_name_or_null"}}

Rules:
- reusable=false for one-off, context-specific work (specific files, URLs, dates, data) unlikely to recur.
- reusable=true only when the pattern is generic and likely to recur in future tasks.
- If the trajectory's function substantially overlaps an existing tool (name/description keywords match), set overlap_with to that tool's name — a duplicate must NOT be created."""

    try:
        response, _ = llm_client.chat(
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.choices[0].message.content.strip()
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if not match:
            raise ValueError("no JSON object in response")
        data = json.loads(match.group(0))
        return {
            "reusable": bool(data.get("reusable")),
            "reason": str(data.get("reason", ""))[:200],
            "suggested_name": str(data.get("suggested_name") or ""),
            "overlap_with": data.get("overlap_with") or None,
        }
    except Exception as e:
        print(f"[AutoTool] Reusability assessment failed, skipping generation: {e}")
        return {"reusable": False, "reason": f"assessment_error: {e}",
                "suggested_name": "", "overlap_with": None}


def plan_tool_generation(task_input: str, tool_sequence: str,
                         existing_tools: Dict[str, str], llm_client,
                         min_calls: int = MIN_TOOL_CALLS_FOR_GENERATION) -> dict:
    """Pre-generation gate. Pure decision function (only side effect: the LLM call).

    Returns a plan dict with ``action`` one of:
      - "skip":      do not generate ("reason" explains why)
      - "reinforce": trajectory overlaps existing tool "overlap_with" — the
                     caller should record usage for that tool instead of
                     generating a duplicate
      - "generate":  proceed to code generation ("suggested_name" may be "")
    """
    names = parse_tool_names(tool_sequence)
    if len(names) < min_calls:
        return {"action": "skip", "reason": f"too_few_calls:{len(names)}"}
    if classify_trajectory(tool_sequence) != "deterministic":
        return {"action": "skip", "reason": "exploratory_trajectory"}

    verdict = assess_reusability(task_input, tool_sequence, existing_tools, llm_client)
    if not verdict["reusable"]:
        return {"action": "skip", "reason": f"not_reusable:{verdict['reason']}"}
    overlap = verdict.get("overlap_with")
    if overlap and overlap in existing_tools:
        return {"action": "reinforce", "overlap_with": overlap,
                "reason": verdict["reason"]}
    suggested = verdict.get("suggested_name") or ""
    if suggested and suggested in existing_tools:
        return {"action": "reinforce", "overlap_with": suggested,
                "reason": "suggested_name_exists"}
    return {"action": "generate", "suggested_name": suggested,
            "reason": verdict["reason"]}


# ── Tool Graduation: auto-tool trust scoring ──

TRUST_FILE = "_trust.json"
GRADUATE_THRESHOLD = 3  # consecutive successes to graduate

# Serializes read-modify-write cycles on _trust.json: usage recording runs on
# the agent main loop while reinforce signals come from the background
# post-process worker — without a lock, interleaved load->mutate->save cycles
# silently drop each other's updates. Held only for file-IO-granularity
# critical sections.
_TRUST_LOCK = threading.Lock()


def _get_trust_path(tools_dir: str) -> str:
    return os.path.join(tools_dir, TRUST_FILE)


def _load_trust(tools_dir: str) -> dict:
    path = _get_trust_path(tools_dir)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_trust(tools_dir: str, trust: dict):
    path = _get_trust_path(tools_dir)
    os.makedirs(tools_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(trust, f, ensure_ascii=False, indent=2)


def record_tool_usage(tools_dir: str, tool_name: str, success: bool) -> dict:
    """Record usage of an auto-generated tool. Returns the tool's trust info."""
    with _TRUST_LOCK:
        trust = _load_trust(tools_dir)
        entry = trust.get(tool_name, {"total": 0, "successes": 0, "consecutive": 0,
                                       "failures": 0, "graduated": False})
        entry["total"] += 1
        if success:
            entry["successes"] += 1
            entry["consecutive"] += 1
        else:
            entry["failures"] += 1
            entry["consecutive"] = 0  # Reset streak on failure
        now_iso = datetime.now().astimezone().isoformat()
        entry.setdefault("first_used", now_iso)
        entry["last_used"] = now_iso
        trust[tool_name] = entry
        _save_trust(tools_dir, trust)
        return entry


def record_tool_reinforce(tools_dir: str, tool_name: str) -> dict:
    """Record a reinforce signal (generation deduped onto this existing tool).

    Bumps ``total`` and a separate ``reinforced`` counter and refreshes
    ``last_used``, but deliberately does NOT touch ``successes``/``consecutive``
    — a reinforce is not a real execution and must never count toward
    graduation (GRADUATE_THRESHOLD consecutive successes).
    """
    with _TRUST_LOCK:
        trust = _load_trust(tools_dir)
        entry = trust.get(tool_name, {"total": 0, "successes": 0, "consecutive": 0,
                                       "failures": 0, "graduated": False})
        entry["total"] += 1
        entry["reinforced"] = entry.get("reinforced", 0) + 1
        now_iso = datetime.now().astimezone().isoformat()
        entry.setdefault("first_used", now_iso)
        entry["last_used"] = now_iso
        trust[tool_name] = entry
        _save_trust(tools_dir, trust)
        return entry


def check_graduation(tools_dir: str, tool_name: str) -> bool:
    """Check if a tool has earned enough trust to graduate to permanent."""
    trust = _load_trust(tools_dir)
    entry = trust.get(tool_name, {})
    if entry.get("graduated"):
        return False  # Already graduated
    return entry.get("consecutive", 0) >= GRADUATE_THRESHOLD


def graduate_tool(tools_dir: str, tool_name: str) -> bool:
    """Move an auto-tool to skills/permanent/ after graduation."""
    from core.paths import get_data_path
    with _TRUST_LOCK:
        trust = _load_trust(tools_dir)
        if tool_name not in trust:
            return False

        source = os.path.join(tools_dir, f"{tool_name}.py")
        if not os.path.exists(source):
            return False

        from core.paths import get_skills_dir
        permanent_dir = os.path.join(get_skills_dir(), "permanent")
        os.makedirs(permanent_dir, exist_ok=True)
        dest = os.path.join(permanent_dir, f"{tool_name}.py")

        try:
            import shutil
            shutil.move(source, dest)
            trust[tool_name]["graduated"] = True
            _save_trust(tools_dir, trust)
            print(f"[AutoTool] {tool_name} graduated to permanent skill!")
            return True
        except Exception as e:
            print(f"[AutoTool] Graduation failed for {tool_name}: {e}")
            return False


# ── Pruning: archive stale, never-used auto-tools ──

ARCHIVE_DIRNAME = "_archive"


def _entry_last_used_ts(entry: dict, fallback_mtime: float) -> float:
    """Best-effort last-used timestamp for a trust entry (epoch seconds)."""
    raw = entry.get("last_used")
    if raw:
        try:
            return datetime.fromisoformat(raw).timestamp()
        except Exception:
            pass
    return fallback_mtime


def prune_auto_tools(tools_dir: str, max_age_days: int = 30,
                     min_calls: int = 1, now: float = None) -> dict:
    """Archive stale auto-tools into ``<tools_dir>/_archive/`` (never hard-deleted).

    A tool is archived when BOTH hold:
      - recorded calls < min_calls (from ``_trust.json``; missing entry = 0 calls)
      - unused for more than max_age_days (trust ``last_used``, falling back to
        file mtime for tools that predate usage recording)

    ``_archive`` is skipped by load_all_dynamic_tools, so archived tools stop
    being offered to the LLM but can be restored manually.

    Returns {"kept": [names], "archived": [names]}.
    """
    import time as _time
    now = now if now is not None else _time.time()
    result = {"kept": [], "archived": []}
    if not os.path.isdir(tools_dir):
        return result

    trust = _load_trust(tools_dir)
    archive_dir = os.path.join(tools_dir, ARCHIVE_DIRNAME)
    max_age_s = max_age_days * 86400

    for fname in sorted(os.listdir(tools_dir)):
        if fname.startswith("_") or fname.startswith("."):
            continue  # _archive, __pycache__, _trust.json, dotfiles
        if not fname.endswith(".py"):
            continue
        fpath = os.path.join(tools_dir, fname)
        if not os.path.isfile(fpath):
            continue
        tool_name = fname[:-3]
        entry = trust.get(tool_name) or {}
        calls = entry.get("total", 0)
        last_used = _entry_last_used_ts(entry, os.path.getmtime(fpath))
        if calls < min_calls and (now - last_used) > max_age_s:
            try:
                os.makedirs(archive_dir, exist_ok=True)
                shutil.move(fpath, os.path.join(archive_dir, fname))
                result["archived"].append(tool_name)
            except Exception as e:
                print(f"[AutoTool] Prune failed for {tool_name}: {e}")
                result["kept"].append(tool_name)
        else:
            result["kept"].append(tool_name)
    return result
