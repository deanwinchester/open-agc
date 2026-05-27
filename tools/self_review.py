import json
import os
from typing import Any, Dict
from tools.base import BaseTool


class SelfReviewTool(BaseTool):
    name: str = "self_review"
    description: str = (
        "自我审查工具：当任务长时间无进展、感觉陷入死循环、或系统提示已达最大迭代次数时使用。"
        "调用后 AI 会审查当前会话，分析是否陷入无效循环、评估任务进度、"
        "并给出继续执行或中断的建议。通过诚实审查可获额外执行机会。"
    )

    def execute(self, max_iterations_reached: bool = False,
                progress_summary: str = "",
                analysis_focus: str = "", **kwargs) -> str:
        agent_ctx = kwargs.get("_agent_context")
        if not agent_ctx:
            return (
                "[SelfReview] No agent context available. "
                "Cannot perform self-review without access to the conversation."
            )

        messages = getattr(agent_ctx, "messages", [])
        llm = getattr(agent_ctx, "llm", None)
        recent_tool_calls = getattr(agent_ctx, "recent_tool_calls", [])
        correction_attempts = getattr(agent_ctx, "_correction_attempts", 0)
        max_correction_attempts = getattr(agent_ctx, "_max_correction_attempts", 5)

        if not llm:
            return "[SelfReview] Cannot perform review: LLM client not available."

        # Extract recent tool call patterns from messages (last 20 tool messages)
        tool_patterns = []
        for msg in reversed(messages):
            if msg.get("role") == "tool" and msg.get("name"):
                tool_patterns.append({
                    "name": msg["name"],
                    "content_preview": msg.get("content", "")[:200]
                })
            if len(tool_patterns) >= 20:
                break
        tool_patterns.reverse()

        # Build loop detection stats from recent_tool_calls hashes
        loop_stats = {}
        if recent_tool_calls:
            for h in recent_tool_calls:
                loop_stats[h] = loop_stats.get(h, 0) + 1
            repeated = {k: v for k, v in loop_stats.items() if v >= 2}
        else:
            repeated = {}

        # Count total tool calls and distinct tool types
        total_tool_calls = sum(1 for m in messages if m.get("role") == "tool")
        distinct_tools = len(set(m.get("name", "") for m in messages if m.get("role") == "tool"))

        # Build the review prompt
        review_prompt = (
            "你是一个严格的自我审查助手。请根据以下对话记录，分析 agent 的执行状态。\n\n"
            f"### 执行统计\n"
            f"- 总工具调用次数：{total_tool_calls}\n"
            f"- 使用的不同工具数：{distinct_tools}\n"
            f"- 最近工具调用哈希重复数：{len(repeated)} 组重复\n"
            f"- 当前纠偏尝试次数：{correction_attempts}/{max_correction_attempts}\n"
        )
        if max_iterations_reached:
            review_prompt += (
                "- 触发原因：已达到最大迭代次数\n"
            )
        if progress_summary:
            review_prompt += f"- Agent 自述进度：{progress_summary}\n"
        if analysis_focus:
            review_prompt += f"- 分析重点：{analysis_focus}\n"

        review_prompt += (
            "\n### 最近工具调用序列（按顺序）\n"
        )
        for tp in tool_patterns[-10:]:
            review_prompt += f"- {tp['name']}: {tp['content_preview'][:100]}\n"

        review_prompt += (
            "\n请输出严格 JSON 格式的审查结果，包含以下字段：\n"
            "1. `progress_summary` (string)：当前任务进度总结\n"
            "2. `loop_detected` (bool)：是否检测到无效循环\n"
            "3. `loop_evidence` (string)：检测到循环的具体证据，或空字符串\n"
            "4. `correction_suggestion` (string)：如果需要纠偏，具体建议；否则为空\n"
            "5. `continue_processing` (bool)：是否可以继续执行（true=可继续，false=建议中断）\n"
            "6. `reason` (string)：给出继续或中断的理由\n\n"
            "只输出 JSON，不要有其他文字。"
        )

        try:
            response, _ = llm.chat(
                messages=[{"role": "user", "content": review_prompt}]
            )
            result_text = response.choices[0].message.content.strip()

            # Strip markdown code fences if present
            if result_text.startswith("```"):
                lines = result_text.split("\n")
                result_text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

            # Validate it's parseable JSON
            result = json.loads(result_text)
            formatted = (
                f"[SelfReview 审查结果]\n"
                f"进度总结：{result.get('progress_summary', 'N/A')}\n"
                f"循环检测：{'是' if result.get('loop_detected') else '否'}\n"
                f"循环证据：{result.get('loop_evidence', '无')}\n"
                f"纠偏建议：{result.get('correction_suggestion', '无')}\n"
                f"建议继续：{'是' if result.get('continue_processing') else '否'}\n"
                f"理由：{result.get('reason', 'N/A')}\n"
                f"---\n"
                f"原始 JSON：{result_text}"
            )
            return formatted
        except json.JSONDecodeError:
            return (
                f"[SelfReview 审查结果]\n"
                f"进度总结：（LLM 返回非标准 JSON，使用统计摘要）\n"
                f"循环检测：{'是' if len(repeated) > 0 else '否'}\n"
                f"循环证据：{len(repeated)} 组重复调用\n"
                f"纠偏建议：基于统计检测到重复工具调用，建议切换策略\n"
                f"建议继续：是\n"
                f"理由：有重复但可能是正常执行过程，建议给予额外尝试\n"
            )
        except Exception as e:
            return (
                f"[SelfReview 审查异常]\n"
                f"审查过程出错：{e}\n"
                f"基于统计数据的保守判断：工具调用 {total_tool_calls} 次，"
                f"最近重复模式 {len(repeated)} 组。建议继续执行。\n"
                f"建议继续：是"
            )

    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "self_review",
                "description": (
                    "执行任务进度自我审查。当你觉得任务陷入循环、长时间无进展、"
                    "需要总结当前进度、或系统提示已达最大迭代次数时调用。"
                    "系统会根据审查结果决定是否允许继续执行。请诚实分析。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "max_iterations_reached": {
                            "type": "boolean",
                            "description": "是否因达到最大迭代次数而触发审查。系统自动设置。"
                        },
                        "progress_summary": {
                            "type": "string",
                            "description": "你对自己当前进度的总结。已经完成了什么，还剩什么。可选。"
                        },
                        "analysis_focus": {
                            "type": "string",
                            "description": "分析重点：'loop_detection'（循环检测）/ "
                                           "'progress_summary'（进度总结）/ "
                                           "'direction_correction'（方向纠偏）。可选。"
                        }
                    },
                    "required": []
                }
            }
        }
