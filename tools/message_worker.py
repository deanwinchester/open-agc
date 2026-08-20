# -*- coding: utf-8 -*-
"""message_worker — 调度者模式 M2：主 agent 向运行中的 worker 转发追加指令。

职责边界（用户指正）：插话分类是主 agent 的职责。用户消息到达后主 agent
判定——闲聊/无关提问直接回答；与当前任务相关的追加要求/纠正才用本工具
转发进 worker 的专属队列（worker 的插话通道只收这里的内容，与原始
pending_messages 物理隔离）。
"""
import json
from typing import Any, Dict

from tools.base import BaseTool


class MessageWorkerTool(BaseTool):
    model_config = {"extra": "allow", "arbitrary_types_allowed": True}

    name: str = "message_worker"
    description: str = (
        "向正在执行任务的 worker 转发用户的追加指令/纠正（仅限与当前任务直接"
        "相关的补充）。闲聊、无关提问、新话题不要使用本工具——闲聊直接回答，"
        "新任务用 dispatch_worker 另派。"
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
                        "message": {
                            "type": "string",
                            "description": "要转发给 worker 的追加指令（你确认与当前任务相关）。",
                        },
                    },
                    "required": ["message"],
                },
            },
        }

    def execute(self, message: str = "", **kwargs) -> str:
        from agent import dispatcher  # 延迟导入避免循环

        agent = kwargs.get("_agent_context")
        if agent is None:
            return "Error: message_worker 需要 agent 上下文（_agent_context）。"
        msg = (message or "").strip()
        if not msg:
            return "Error: message 不能为空。"
        sid = getattr(agent, "session_id", None)
        tid = getattr(agent, "task_id", None)
        if dispatcher.get_running_dispatch(sid, tid) is None:
            return json.dumps({
                "delivered": False,
                "note": ("当前任务没有运行中的 worker——若 worker 已完成，"
                         "请直接回答用户或重新 dispatch_worker；不要转发。"),
            }, ensure_ascii=False)
        dispatcher.push_worker_inbox(sid, tid, msg)
        return json.dumps({
            "delivered": True,
            "note": "追加指令已注入 worker 队列，它会在下一步行动前看到并采纳。",
        }, ensure_ascii=False)
