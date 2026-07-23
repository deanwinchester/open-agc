# -*- coding: utf-8 -*-
"""dispatch_subagent — 显式子代理分派工具。

让模型在长任务中自主选择把子任务交给 SubAgent 独立执行，
而不是仅依赖 agent._should_delegate 的启发式自动委派。

工具在 agent 上下文中执行：主循环通过 _agent_context 注入当前 agent，
本子工具从中取得 llm / full_available_tools，复用 agent.sub_agent.SubAgent。
"""
import json
from typing import Any, Dict

from tools.base import BaseTool


class DispatchSubagentTool(BaseTool):
    model_config = {"extra": "allow", "arbitrary_types_allowed": True}

    name: str = "dispatch_subagent"
    description: str = (
        "分派一个子代理在独立上下文中执行子任务，并返回结构化结果（success/summary）。"
        "适用场景：大任务拆分、多个相互独立的子任务需要隔离执行——子代理不占用主对话上下文窗口。"
        "简单的单步操作（读个文件、跑条命令）请直接在主循环自行执行，不要使用本工具。"
    )

    def get_openai_schema(self) -> Dict[str, Any]:
        from agent.sub_agent import TOOL_SETS
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": (
                                "子任务的完整描述。子代理看不到主对话，"
                                "描述必须自包含（背景、目标、产出要求）。"
                            ),
                        },
                        "tool_set": {
                            "type": "string",
                            "enum": list(TOOL_SETS.keys()),
                            "description": "子代理工具集；省略时按 task 自动匹配。",
                        },
                        "max_iterations": {
                            "type": "integer",
                            "description": "子代理最大迭代次数，默认 10。",
                            "default": 10,
                        },
                    },
                    "required": ["task"],
                },
            },
        }

    def execute(self, task: str, tool_set: str = None,
                max_iterations: int = 10, **kwargs) -> str:
        from agent.sub_agent import SubAgent, TOOL_SETS, match_tool_set

        agent = kwargs.get("_agent_context")
        if agent is None:
            return "Error: dispatch_subagent 需要 agent 上下文（_agent_context）。"

        llm = getattr(agent, "llm", None)
        if llm is None:
            return "Error: 当前 agent 上下文没有可用的 LLM client。"

        parent_tools = (getattr(agent, "full_available_tools", None)
                        or getattr(agent, "available_tools", None) or {})

        # Resolve tool set: explicit choice validated; otherwise auto-match by task
        if tool_set:
            if tool_set not in TOOL_SETS:
                return (f"Error: 未知 tool_set '{tool_set}'。"
                        f"可选：{', '.join(TOOL_SETS.keys())}")
            matched_set = tool_set
        else:
            matched_set = match_tool_set(task)
        tools = TOOL_SETS[matched_set]["tools"]

        try:
            max_iterations = int(max_iterations)
        except (TypeError, ValueError):
            max_iterations = 10
        max_iterations = max(1, min(max_iterations, 30))

        # Sub-agents are context-isolated: forward the session brief (goal /
        # recent user messages / paths) when the agent provides one.
        context_brief = ""
        _brief_fn = getattr(agent, "_build_context_brief", None)
        if callable(_brief_fn):
            try:
                context_brief = _brief_fn() or ""
            except Exception:
                context_brief = ""

        sub = SubAgent(
            task=task,
            tools=tools,
            parent_tools=parent_tools,
            max_iterations=max_iterations,
            progress_callback=kwargs.get("_progress_cb"),
            llm_client=llm,
            agent_context=agent,
            session_whitelist=(kwargs.get("_session_whitelist")
                               or getattr(agent, "_session_sandbox_whitelist", None)),
            network_whitelist=(kwargs.get("_network_whitelist")
                               or getattr(agent, "_session_network_whitelist", None)),
            permission_whitelist=(kwargs.get("_permission_whitelist")
                                  or getattr(agent, "_session_permission_whitelist", None)),
            session_id=(kwargs.get("_session_id")
                        or getattr(agent, "session_id", None)),
            context_brief=context_brief,
        )
        # SandboxBlocked propagates to the main loop's handler (sub-agents
        # have no user-authorization channel of their own).
        result = sub.run()

        return json.dumps({
            "success": bool(result.get("success")),
            "summary": result.get("summary", ""),
            "tool_set": matched_set,
            "iterations_used": result.get("iterations_used"),
            "tool_calls": result.get("tool_calls"),
            "duration": round(result.get("duration", 0) or 0, 1),
            "output_files": result.get("output_files", []),
        }, ensure_ascii=False)
