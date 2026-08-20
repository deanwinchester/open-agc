# -*- coding: utf-8 -*-
"""dispatch_worker — 调度者模式 M1 派发入口（重构轮设计）。

与 dispatch_subagent（通用子代理分派，旧 TOOL_SETS 路径）的区别：
dispatch_worker 是调度者模式的唯一入口——**意图理解发生在主 agent 自己的
推理中**（它的 LLM 调用带全量会话上下文/历史/记忆注入），由它把写好的完整
任务简报传进来；agent.dispatcher 只做程序化检索增强（历史任务/语义记忆/
会话路径），不做理解。

流程：brief → enrich_handoff 检索增强 → SubAgent 执行（全量工具发现 +
中断/插话联动）→ verify_execution 证据验收 → 结构化返回。验收失败带原因
重派一次；双失败返回明确失败信息——调用发生在主循环内，主 agent 读到失败
后自然亲自接管执行。
"""
import json
from typing import Any, Dict

from tools.base import BaseTool


def _parse_acceptance(raw) -> list:
    """acceptance 入参归一化：list / JSON 数组字符串 / 换行分隔文本 → ≤3 条。"""
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            raw = parsed if isinstance(parsed, list) else [s]
        except Exception:
            raw = [ln.strip().lstrip("-0123456789.、 ").strip()
                   for ln in s.splitlines()]
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for c in raw:
        c = str(c).strip()
        if c:
            out.append(c[:200])
        if len(out) >= 3:
            break
    return out


class DispatchWorkerTool(BaseTool):
    model_config = {"extra": "allow", "arbitrary_types_allowed": True}

    name: str = "dispatch_worker"
    description: str = (
        "派发一个执行者（worker）在独立上下文中完成实质任务，后台异步执行并对产出做"
        "证据验收——调用立即返回，你**不要空等**：先回复用户已开工；worker 完成（含验收）"
        "后系统会以【执行者返回】通知你，届时你验收证据并呈现交付。"
        "调用前你必须基于全部会话上下文亲自理解用户意图，把完整任务简报（目标、背景、"
        "产出要求）写入 task_brief——worker 看不到本对话，简报必须自包含。"
        "系统会自动补充相关历史任务、记忆与文件路径作为参考。"
        "闲聊、简单问答、单步小操作（读个文件、跑条命令）不要使用本工具。"
    )

    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_brief": {
                            "type": "string",
                            "description": (
                                "你亲自写的完整任务简报：目标、背景、产出要求。"
                                "worker 看不到本对话，简报必须自包含。"
                            ),
                        },
                        "acceptance": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "可检验的验收标准（可选，≤3 条），"
                                "如「产出文件 X 存在且非空」「命令 Y 输出包含 Z」。"
                            ),
                        },
                        "max_iterations": {
                            "type": "integer",
                            "description": "worker 最大迭代次数（可选，默认 20，上限 30）。",
                        },
                    },
                    "required": ["task_brief"],
                },
            },
        }

    def execute(self, task_brief: str = "", acceptance=None,
                max_iterations=None, **kwargs) -> str:
        from agent import dispatcher  # 延迟导入避免循环

        agent = kwargs.get("_agent_context")
        if agent is None:
            return "Error: dispatch_worker 需要 agent 上下文（_agent_context）。"
        if getattr(agent, "llm", None) is None:
            return "Error: 当前 agent 上下文没有可用的 LLM client。"
        if not (task_brief or "").strip():
            return ("Error: task_brief 不能为空——请基于全部会话上下文"
                    "亲自写出完整任务简报（目标、背景、产出要求）。")

        result = dispatcher.dispatch_async(
            agent,
            task_brief.strip(),
            acceptance=_parse_acceptance(acceptance),
            max_iterations=max_iterations,
            progress_callback=kwargs.get("_progress_cb"),
        )

        out = {
            "dispatched": bool(result.get("dispatched")),
            "note": ("执行者已在后台开工。现在请立即回复用户「已开工 + 任务要点」，不要空等；"
                     "执行者完成（含证据验收）后系统会以【执行者返回】通知你，"
                     "届时你再验收并呈现交付。worker 执行期间用户发来的追加指令，"
                     "判断与当前任务相关时用 message_worker 转发，闲聊/无关提问直接回答。"),
        }
        return json.dumps(out, ensure_ascii=False)
