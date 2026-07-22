"""
SubAgent — Lightweight child agent with isolated context and tool set.

Delegated by the main agent for complex multi-step tasks to prevent
context window exhaustion and enable parallel execution.
"""
import json
import time as _time
import re
import threading
from typing import List, Dict, Any, Optional, Callable


# Tool sets for common sub-task types.
# Each entry separates domain keywords (semantic words used for matching user
# intent) from tool names — tool names must NOT appear in keywords, otherwise
# a request merely mentioning a tool (e.g. "用 read_file 读一下 X") would hit
# every domain at once and force sub-agent delegation.
TOOL_SETS: Dict[str, Dict[str, List[str]]] = {
    "filesystem": {
        "keywords": ["文件", "目录", "文件夹", "批量重命名", "整理文件"],
        "tools": ["read_file", "write_file", "execute_shell"],
    },
    "code": {
        "keywords": ["写代码", "代码", "编程", "脚本", "调试", "code", "script", "debug"],
        "tools": ["execute_python", "execute_shell", "read_file", "ask_user_question"],
    },
    "web": {
        "keywords": ["网页", "浏览器", "网站", "抓取", "browser", "website", "crawl"],
        "tools": ["browser_automation", "search_web", "ask_user_question"],
    },
    "analysis": {
        "keywords": ["分析", "统计", "数据", "报表", "analysis", "analyze"],
        "tools": ["execute_python", "read_file", "search_web", "ask_user_question"],
    },
    "deploy": {
        "keywords": ["部署", "发布", "上线", "deploy"],
        "tools": ["execute_shell", "read_file", "write_file", "ask_user_question"],
    },
    "monitor": {
        "keywords": ["监控", "告警", "运维", "monitor", "alert"],
        "tools": ["execute_shell", "read_file", "ask_user_question"],
    },
    "research": {
        "keywords": ["研究", "调研", "调查", "资料收集", "research"],
        "tools": ["search_web", "read_file", "ask_user_question"],
    },
}


def match_tool_set(task: str, default: str = "filesystem") -> str:
    """Pick the TOOL_SETS domain whose keywords best match a task description."""
    text = (task or "").lower()
    best, best_hits = default, 0
    for name, entry in TOOL_SETS.items():
        hits = sum(1 for kw in entry["keywords"] if kw in text)
        if hits > best_hits:
            best, best_hits = name, hits
    return best


class SubAgent:
    """Lightweight child agent with its own context and tool set."""

    # Tools that maintain global/singleton state — serialize access with locks
    STATEFUL_TOOLS = {"browser_automation", "computer_control"}
    # Pre-created at class definition time: check-then-act creation in run()
    # races when parallel sub-agents each create and overwrite the shared Lock,
    # silently breaking mutual exclusion for browser/computer.
    _tool_locks: Dict[str, threading.Lock] = {
        name: threading.Lock() for name in STATEFUL_TOOLS
    }

    def __init__(self, task: str, tools: List[str],
                 parent_tools: Dict,
                 max_iterations: int = 10,
                 progress_callback: Optional[Callable] = None,
                 llm_client=None,
                 agent_context=None,
                 session_whitelist=None,
                 network_whitelist=None,
                 permission_whitelist=None,
                 session_id=None):
        self.task = task
        self.max_iterations = max_iterations
        self.progress_callback = progress_callback
        self.llm = llm_client
        self.is_interrupted = False
        self._agent_context = agent_context
        self._session_whitelist = session_whitelist or set()
        self._network_whitelist = network_whitelist or set()
        self._permission_whitelist = permission_whitelist or set()
        self._session_id = session_id

        # Build system prompt
        now = _time.strftime("%Y-%m-%d %H:%M:%S")
        tool_list = "\n".join(f"  - {t}" for t in tools)
        self.messages = [
            {
                "role": "system",
                "content": (
                    f"你是 Open-AGC 的子代理，当前时间：{now}\n"
                    f"你的子任务是：{task}\n\n"
                    f"可用工具：\n{tool_list}\n\n"
                    f"规则：\n"
                    f"1. 专注于完成子任务，完成后返回结果摘要\n"
                    f"2. 不要使用未列出的工具\n"
                    f"3. 不要执行超出子任务范围的操作"
                )
            }
        ]

        # Filter parent tools to only what this sub-agent needs
        self.available_tools = {}
        for name in tools:
            if name in parent_tools:
                self.available_tools[name] = parent_tools[name]

        self.tool_schemas = [t.get_openai_schema()
                             for t in self.available_tools.values()]

    def run(self) -> Dict[str, Any]:
        """Execute the sub-task. Returns structured result."""
        from tools.base import SandboxBlocked
        start_time = _time.time()

        if not self.llm:
            return {"success": False, "summary": "No LLM client provided",
                    "duration": 0, "output_files": []}

        # Add the user message (the sub-task)
        self.messages.append({"role": "user", "content": self.task})

        current_iter = 0
        tool_call_count = 0
        recent_tool_calls = []
        steps = []
        MAX_REPEATED_TOOL_CALLS = 3

        while current_iter < self.max_iterations:
            if self.is_interrupted:
                return {"success": False, "summary": "Interrupted",
                        "duration": _time.time() - start_time, "output_files": []}

            current_iter += 1

            try:
                response, _ = self.llm.chat(
                    messages=self.messages,
                    tools=self.tool_schemas if self.tool_schemas else None,
                )
            except Exception as e:
                return {"success": False, "summary": f"LLM error: {e}",
                        "duration": _time.time() - start_time, "output_files": []}

            message = response.choices[0].message

            # Tool calls
            if message.tool_calls:
                tool_call_count += len(message.tool_calls)
                self.messages.append({
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments
                            }
                        }
                        for tc in message.tool_calls
                    ],
                })

                # Execute each tool call
                for tc in message.tool_calls:
                    func_name = tc.function.name
                    try:
                        func_args = json.loads(tc.function.arguments)
                    except Exception:
                        func_args = {}

                    # Tool loop detection
                    call_hash = f"{func_name}:{json.dumps(func_args, sort_keys=True)}"
                    recent_tool_calls.append(call_hash)
                    if len(recent_tool_calls) > 10:
                        recent_tool_calls.pop(0)
                    loop_count = recent_tool_calls.count(call_hash)

                    if self.progress_callback:
                        self.progress_callback({
                            "event": "tool_start",
                            "tool": func_name,
                            "step": tool_call_count,
                            "args_preview": tc.function.arguments[:200],
                            "sub_task": self.task[:50]
                        })

                    # Block if stuck in a loop
                    if loop_count >= MAX_REPEATED_TOOL_CALLS:
                        result = (f"System Guard: Blocked due to critical loop. "
                                  f"You have called `{func_name}` with these exact arguments "
                                  f"{loop_count} times recently. "
                                  f"You are likely stuck in a loop. "
                                  f"YOU MUST change your approach or use different parameters.")
                        success = False
                        self.messages.append({
                            "role": "tool",
                            "content": str(result),
                            "tool_call_id": tc.id,
                            "name": func_name,
                        })
                        if self.progress_callback:
                            self.progress_callback({
                                "event": "tool_done",
                                "tool": func_name,
                                "step": tool_call_count,
                                "success": False,
                                "result_preview": str(result)[:200],
                                "sub_task": self.task[:50],
                            })
                        steps.append({
                            "tool": func_name,
                            "args": tc.function.arguments[:200],
                            "result_preview": str(result)[:300],
                            "success": False,
                        })
                        continue

                    tool = self.available_tools.get(func_name)
                    success = False
                    if not tool:
                        result = f"Error: Tool '{func_name}' not available in this sub-agent"
                    else:
                        try:
                            extra_kwargs = {
                                "_session_whitelist": self._session_whitelist,
                                "_progress_cb": self.progress_callback,
                                "_network_whitelist": self._network_whitelist,
                                "_permission_whitelist": self._permission_whitelist,
                                "_session_id": self._session_id,
                            }
                            # Serialize access for stateful tools shared across threads
                            # (locks are pre-created at class level — no check-then-act here)
                            if func_name in self.STATEFUL_TOOLS:
                                self._tool_locks[func_name].acquire()
                                try:
                                    result = tool.execute(
                                        interrupt_check=lambda: self.is_interrupted,
                                        _agent_context=self._agent_context,
                                        **extra_kwargs,
                                        **func_args,
                                    )
                                finally:
                                    self._tool_locks[func_name].release()
                            else:
                                result = tool.execute(
                                    interrupt_check=lambda: self.is_interrupted,
                                    _agent_context=self._agent_context,
                                    **extra_kwargs,
                                    **func_args,
                                )
                            if len(result) > 15000:
                                result = result[:15000] + "\n...[truncated]"
                            success = True
                        except SandboxBlocked:
                            # Sub-agents have no channel to ask the user for
                            # authorization — re-raise so the main agent's
                            # delegation collector can route it into
                            # _handle_sandbox_blocked.
                            raise
                        except Exception as e:
                            result = f"Error executing {func_name}: {e}"

                    self.messages.append({
                        "role": "tool",
                        "content": str(result),
                        "tool_call_id": tc.id,
                        "name": func_name,
                    })

                    if self.progress_callback:
                        self.progress_callback({
                            "event": "tool_done",
                            "tool": func_name,
                            "step": tool_call_count,
                            "success": success,
                            "result_preview": str(result)[:200],
                            "sub_task": self.task[:50],
                        })
                    steps.append({
                        "tool": func_name,
                        "args": tc.function.arguments[:200],
                        "result_preview": str(result)[:300],
                        "success": success,
                    })

                continue

            # Text response — sub-task complete
            summary = message.content or ""
            # Collect output file references from messages
            output_files = re.findall(
                r'(?:saved|wrote|created|written to)\s*:?\s*([^\s)]+\.[a-zA-Z]+)',
                summary, re.IGNORECASE
            )

            return {
                "success": True,
                "summary": summary[:2000],
                "output_files": list(set(output_files)),
                "iterations_used": current_iter,
                "tool_calls": tool_call_count,
                "duration": _time.time() - start_time,
                "steps": steps,
            }

        return {"success": False, "summary": f"Max iterations ({self.max_iterations}) reached",
                "duration": _time.time() - start_time, "output_files": [], "steps": steps}
