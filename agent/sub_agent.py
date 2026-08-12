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
                 session_id=None,
                 context_brief: str = "",
                 full_tools_map: Optional[Dict] = None,
                 external_interrupt_check: Optional[Callable] = None,
                 pending_message_provider: Optional[Callable[[], str]] = None):
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
        self.context_brief = context_brief or ""
        # 全量工具发现（调度者模式 M1）：传入时 worker 可通过
        # search_available_tools 从该 map 解锁更多工具；不传维持现状。
        self._full_tools_map = full_tools_map
        # 调度者模式 M1 联动钩子（均可选，不传维持现状）：
        # - external_interrupt_check：主 agent 中断标志透传（评审 I-1）
        # - pending_message_provider：每迭代拉取用户插话注入 worker 上下文（评审 I-2）
        self._external_interrupt_check = external_interrupt_check
        self._pending_message_provider = pending_message_provider

        # Build system prompt — 执行者角色提示（调度者模式重构轮重写）：
        # 简报为权威依据；真实执行纪律；结构化交付汇报。
        now = _time.strftime("%Y-%m-%d %H:%M:%S")
        tool_list = "\n".join(f"  - {t}" for t in tools)
        discovery_rule = (
            "3. 需要未列出的能力时，先调用 search_available_tools 检索启用，再使用该工具\n"
            if self._full_tools_map is not None else ""
        )
        # Sub-agents run isolated from the main conversation; the brief
        # carries the goal / recent user messages / session paths so the
        # sub-agent never has to "find the repository" by blind scanning.
        brief_section = (
            f"\n\n## 会话上下文（参考）\n{self.context_brief}"
            if self.context_brief else ""
        )
        self.messages = [
            {
                "role": "system",
                "content": (
                    f"你是执行者（work agent），由调度者指派任务。当前时间：{now}\n\n"
                    f"## 任务简报（调度者撰写，是权威依据）\n{task}\n\n"
                    f"## 可用工具\n{tool_list}\n\n"
                    f"## 工作纪律\n"
                    f"1. 用工具真实执行——禁止只描述计划不行动；每个意图都落到工具调用上。\n"
                    f"2. 严格按简报的目标与产出要求执行，不做超出范围的操作。\n"
                    f"{discovery_rule}"
                    f"4. 产出文件写到简报指定位置；未指定则放沙箱 outputs/ 下，"
                    f"并在汇报中给出完整路径。\n"
                    f"5. 遇到困难先说明原因再换方案；同一方法失败两次就换思路，"
                    f"不要重复尝试同样的操作。\n"
                    f"6. 需求不清或客观不可行时：明确报告「无法完成」及原因、"
                    f"已排除的路径——严禁假装完成。\n\n"
                    f"## 交付汇报（最后一轮必须包含）\n"
                    f"- 完成内容（实际做了什么）\n"
                    f"- 产出清单（文件的完整路径）\n"
                    f"- 验收标准逐条自评（简报里有的话）\n"
                    f"- 遗留问题/风险（没有就写「无」）"
                    f"{brief_section}"
                )
            }
        ]

        # Filter parent tools to only what this sub-agent needs
        self.available_tools = {}
        for name in tools:
            if name in parent_tools:
                self.available_tools[name] = parent_tools[name]

        # 全量工具发现（M1）：发现工具必须绑定本子代理自己的解锁回调——
        # 复用主 agent 的 ToolDiscoveryTool 实例会把工具启用到主 agent 上。
        if self._full_tools_map is not None:
            from tools.discovery import ToolDiscoveryTool

            def _enable_more(tool_names):
                added = False
                for _n in tool_names or []:
                    if _n in self._full_tools_map and _n not in self.available_tools:
                        self.available_tools[_n] = self._full_tools_map[_n]
                        added = True
                if added:
                    self.tool_schemas = [t.get_openai_schema()
                                         for t in self.available_tools.values()
                                         if t is not None]

            self.available_tools["search_available_tools"] = ToolDiscoveryTool(
                full_tools=self._full_tools_map, enable_callback=_enable_more)

        self.tool_schemas = [t.get_openai_schema()
                             for t in self.available_tools.values()]

    def _interrupted(self) -> bool:
        """自身中断标志 + 外部（主 agent）中断标志联动（评审 I-1）。"""
        if self.is_interrupted:
            return True
        try:
            return bool(self._external_interrupt_check
                        and self._external_interrupt_check())
        except Exception:
            return False

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
            if self._interrupted():
                return {"success": False, "summary": "Interrupted",
                        "duration": _time.time() - start_time, "output_files": []}

            # 用户插话转发（I-2）：调度执行期间主循环轮询点到不了，
            # 由 provider 拉取主 agent 队列里的新消息注入 worker 上下文。
            if self._pending_message_provider:
                try:
                    _pending = self._pending_message_provider()
                except Exception:
                    _pending = ""
                if _pending:
                    self.messages.append({"role": "user", "content": _pending})

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
                            "tool_call_id": tc.id,
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
                                "tool_call_id": tc.id,
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
                        if self._full_tools_map and func_name in self._full_tools_map:
                            # 已知但未解锁（全量工具发现）：引导走发现路径，
                            # 与主 agent 的 tiered exposure 行为一致。
                            result = (f"Error: Tool '{func_name}' is not enabled in this sub-agent. "
                                      f"Call search_available_tools with a related query to enable it, "
                                      f"then retry your call.")
                        else:
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
                                        interrupt_check=lambda: self._interrupted(),
                                        _agent_context=self._agent_context,
                                        **extra_kwargs,
                                        **func_args,
                                    )
                                finally:
                                    self._tool_locks[func_name].release()
                            else:
                                result = tool.execute(
                                    interrupt_check=lambda: self._interrupted(),
                                    _agent_context=self._agent_context,
                                    **extra_kwargs,
                                    **func_args,
                                )
                            success = True
                        except SandboxBlocked:
                            # Sub-agents have no channel to ask the user for
                            # authorization — re-raise so the main agent's
                            # delegation collector can route it into
                            # _handle_sandbox_blocked.
                            raise
                        except Exception as e:
                            result = f"Error executing {func_name}: {e}"

                    # Secrets masking BEFORE truncation (same choke point as the
                    # main agent loop): a cut through a credential would let the
                    # pieces escape whole-string matching — mask first.
                    try:
                        from core.secrets import mask_secrets as _mask_secret_values
                        result = _mask_secret_values(str(result))
                    except Exception:
                        result = str(result)
                    if len(result) > 15000:
                        result = result[:15000] + "\n...[truncated]"

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
                            "tool_call_id": tc.id,
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

            # ── 工具调用营救（生产实证：模型把工具调用写成 JSON 文本时，
            # 旧逻辑直接当最终答复返回 success，子代理零执行"假完成"）──
            _rescued = None
            try:
                _txt = summary.strip()
                _m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', _txt, re.DOTALL)
                _cand = _m.group(1) if _m else _txt
                _obj = json.loads(_cand)
                if isinstance(_obj, dict) and isinstance(_obj.get("name"), str):
                    _rescued = _obj
            except Exception:
                _rescued = None
            if _rescued and _rescued.get("name") in self.available_tools:
                _fn = _rescued["name"]
                _fa = _rescued.get("arguments") or _rescued.get("parameters") or {}
                if isinstance(_fa, str):
                    try:
                        _fa = json.loads(_fa)
                    except Exception:
                        _fa = {}
                # 追加 assistant 文本后按正常工具调用继续循环
                self.messages.append({"role": "assistant", "content": summary})
                _tool = self.available_tools[_fn]
                try:
                    _res = _tool.execute(
                        interrupt_check=lambda: self._interrupted(),
                        _agent_context=self._agent_context,
                        _session_whitelist=self._session_whitelist,
                        _network_whitelist=self._network_whitelist,
                        _permission_whitelist=self._permission_whitelist,
                        _session_id=self._session_id,
                        **_fa,
                    )
                    _ok = True
                except Exception as _e:
                    _res = f"Error executing {_fn}: {_e}"
                    _ok = False
                tool_call_count += 1
                self.messages.append({
                    "role": "user",
                    "content": f"[工具 {_fn} 执行结果]\n{str(_res)[:15000]}",
                })
                steps.append({"tool": _fn, "args": json.dumps(_fa)[:200],
                              "result_preview": str(_res)[:300], "success": _ok})
                if self.progress_callback:
                    self.progress_callback({
                        "event": "tool_done", "tool": _fn,
                        "step": tool_call_count, "success": _ok,
                        "result_preview": str(_res)[:200],
                        "sub_task": self.task[:50],
                    })
                continue

            # ── 空谈守卫：零工具调用就返回文字计划 ≠ 完成（生产实证：
            # 子代理回一段计划即"成功"，主 agent 只能兜底重做）──
            if tool_call_count == 0:
                self._empty_nudges = getattr(self, "_empty_nudges", 0) + 1
                if self._empty_nudges <= 2:
                    self.messages.append({"role": "assistant", "content": summary})
                    self.messages.append({"role": "user", "content": (
                        "你还没有执行任何工具调用。请不要只描述计划——"
                        "现在就用可用工具实际执行子任务，完成后再汇报结果。")})
                    continue

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
