"""
SubAgent — Lightweight child agent with isolated context and tool set.

Delegated by the main agent for complex multi-step tasks to prevent
context window exhaustion and enable parallel execution.
"""
import json
import time as _time
import re
from typing import List, Dict, Any, Optional, Callable


# Tool sets for common sub-task types
TOOL_SETS: Dict[str, List[str]] = {
    "filesystem": ["read_file", "write_file", "execute_shell"],
    "code": ["execute_python", "execute_shell", "read_file"],
    "web": ["browser_automation", "search_web"],
    "analysis": ["execute_python", "read_file", "search_web"],
    "deploy": ["execute_shell", "read_file", "write_file"],
    "research": ["search_web", "read_file"],
}


class SubAgent:
    """Lightweight child agent with its own context and tool set."""

    def __init__(self, task: str, tools: List[str],
                 parent_tools: Dict,
                 max_iterations: int = 10,
                 progress_callback: Optional[Callable] = None,
                 llm_client=None):
        self.task = task
        self.max_iterations = max_iterations
        self.progress_callback = progress_callback
        self.llm = llm_client
        self.is_interrupted = False

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
        start_time = _time.time()

        if not self.llm:
            return {"success": False, "summary": "No LLM client provided",
                    "duration": 0, "output_files": []}

        # Add the user message (the sub-task)
        self.messages.append({"role": "user", "content": self.task})

        current_iter = 0
        tool_call_count = 0

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

                    tool = self.available_tools.get(func_name)
                    if not tool:
                        result = f"Error: Tool '{func_name}' not available in this sub-agent"
                    else:
                        try:
                            result = tool.execute(**func_args)
                            if len(result) > 15000:
                                result = result[:15000] + "\n...[truncated]"
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
                            "type": "tool_done",
                            "tool": func_name,
                            "sub_task": self.task[:50],
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
            }

        return {"success": False, "summary": f"Max iterations ({self.max_iterations}) reached",
                "duration": _time.time() - start_time, "output_files": []}
