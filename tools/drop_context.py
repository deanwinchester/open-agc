# -*- coding: utf-8 -*-
"""drop_context 工具：agent 主动丢弃已无价值的历史工具结果，释放上下文。

动机（用户需求）：大段无关检索结果等低价值内容会一直占着上下文 token，
直到压缩/折叠机制被动触发。给模型一个主动口子：确认某段工具结果不再有
价值时，调用本工具把它替换成占位符。

协议约束（OpenAI）：assistant 的 tool_calls 必须有对应的 tool 消息，所以
只替换 tool 消息的 content、绝不删消息——消息骨架（role/tool_call_id/name）
原样保留。最近 2 轮（assistant tool_calls 轮）是当前工作现场，禁止丢弃，
防止模型把自己正在用的数据拆掉。
"""
import logging
import re
from typing import Any, Dict, List

from tools.base import BaseTool

logger = logging.getLogger("agent.drop_context")

# 保护窗口：最近 2 轮工具调用内的结果禁止丢弃
_PROTECTED_RECENT_ROUNDS = 2


class DropContextTool(BaseTool):
    name: str = "drop_context"
    description: str = (
        "主动丢弃已无价值的历史工具结果以释放上下文（如大段无关检索结果、已用完的长日志）。"
        "targets 填要丢弃的内容标识：工具结果的 tool_call_id，或「最近第N轮」（丢弃该轮全部工具结果）。"
        "被丢弃处保留占位符与 system 备注，原始内容可在沙箱 tool_results/ 或重新执行获得；"
        "最近 2 轮是当前工作现场，受保护不可丢弃。"
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
                        "targets": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "要丢弃的内容标识列表：tool_call_id，或「最近第N轮」/纯数字序号 N（从最近往回数）。",
                        },
                        "reason": {
                            "type": "string",
                            "description": "丢弃原因，会写进占位符（如「该检索结果与任务无关」）。",
                        },
                    },
                    "required": ["targets", "reason"],
                },
            },
        }

    def execute(self, targets=None, reason: str = "", _agent_context=None, **kwargs) -> str:
        if _agent_context is None or not hasattr(_agent_context, "messages"):
            return "Error: 缺少 Agent 上下文，无法执行丢弃。"
        messages = _agent_context.messages
        if isinstance(targets, str):
            targets = [targets]
        if not isinstance(targets, list) or not targets:
            return "Error: targets 必须是非空数组（tool_call_id 或「最近第N轮」）。"
        reason = (reason or "").strip() or "未说明原因"

        # 轮 = 一轮 assistant tool_calls 及其跟随的 tool 结果消息。
        # 保护最近 2 轮（当前工作现场）；空轮（结果被整体移除过）无可保护内容。
        rounds: List[List[int]] = []
        current = None
        for i, m in enumerate(messages):
            if m.get("role") == "assistant" and m.get("tool_calls"):
                current = []
                rounds.append(current)
            elif m.get("role") == "tool" and current is not None:
                current.append(i)
        non_empty_rounds = [r for r in rounds if r]
        protected = set()
        for r in non_empty_rounds[-_PROTECTED_RECENT_ROUNDS:]:
            protected.update(r)

        by_id = {}
        for i, m in enumerate(messages):
            if m.get("role") == "tool" and m.get("tool_call_id"):
                by_id.setdefault(str(m["tool_call_id"]), i)

        placeholder = (f"[该内容已被你主动丢弃以节省上下文：{reason}；"
                       f"原始内容如需可在沙箱 tool_results/ 或重新执行获得]")
        dropped, refused, not_found = [], [], []
        for raw in targets:
            t = str(raw).strip()
            if not t:
                continue
            m_ord = re.fullmatch(r"(?:最近)?第?\s*(\d+)\s*轮", t) or re.fullmatch(r"(\d+)", t)
            if m_ord:
                n = int(m_ord.group(1))
                if 1 <= n <= len(non_empty_rounds):
                    idxs = list(non_empty_rounds[-n])  # 最近第 N 轮
                else:
                    not_found.append(t)
                    continue
            elif t in by_id:
                idxs = [by_id[t]]
            else:
                not_found.append(t)
                continue
            for i in idxs:
                if i in protected:
                    refused.append(t)
                elif i not in dropped:
                    # 只替换 content——role/tool_call_id/name 骨架保留，
                    # assistant 的 tool_calls 才有对应 tool 消息（协议要求）。
                    messages[i]["content"] = placeholder
                    dropped.append(i)

        if dropped:
            # 在丢弃处之后留一条 system 备注，让模型明确知道「这是你主动丢弃的」。
            # 不能插进 assistant.tool_calls 与其 tool 结果链之间（协议要求 tool
            # 消息紧跟），顺延到该轮结果链之后。
            note = (f"[上下文备注] 上文 {len(dropped)} 段工具结果是你通过 drop_context "
                    f"主动丢弃的（原因：{reason}）——这是你自己的主动操作，不是系统故障或数据丢失；"
                    f"原始内容可在沙箱 tool_results/ 或重新执行获得。")
            insert_at = max(dropped) + 1
            while insert_at < len(messages) and messages[insert_at].get("role") == "tool":
                insert_at += 1
            messages.insert(insert_at, {"role": "system", "content": note})
            logger.info("drop_context: 丢弃 %d 段工具结果（下标 %s），原因：%s",
                        len(dropped), dropped, reason)

        parts = []
        if dropped:
            parts.append(f"已丢弃 {len(dropped)} 段工具结果（内容替换为占位符，消息骨架保留）。")
        if refused:
            parts.append(f"以下目标属于最近 {_PROTECTED_RECENT_ROUNDS} 轮（当前工作现场），受保护未丢弃："
                         f"{', '.join(sorted(set(refused)))}。")
        if not_found:
            parts.append(f"未找到目标：{', '.join(not_found)}"
                         f"（targets 填 tool_call_id 或「最近第N轮」）。")
        if not parts:
            return "Error: 没有可丢弃的目标。"
        return "\n".join(parts)
