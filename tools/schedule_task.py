# -*- coding: utf-8 -*-
"""定时/周期任务工具：agent 创建与管理 cron 周期任务。

背景：cron 调度器（api/background.py start_task_scheduler，每 30s 扫描
task_type='scheduled' 且到期者点火）早就存在，但创建入口只有 REST API
——agent 搜不到、建不了「每隔 N 分钟做某事」（生产实证：用户要每 10 分钟
发热门新闻，agent 研究半天没建成）。本工具是调度器的 agent 侧入口。

时区口径：调度器按 UTC 比较 next_run_at，cron 表达式按 UTC 解析——
「每天 9 点」指北京时间时要换算（UTC = 北京 - 8h，即 0 1 * * *）。
"""
import sqlite3
from typing import Any, Dict

from tools.base import BaseTool


def _next_run_utc(cron: str) -> str:
    """与 routes_tasks._next_run_utc 同口径：调度器按 UTC 比较。"""
    from croniter import croniter
    from datetime import datetime, timezone
    return croniter(cron, datetime.now(timezone.utc)).get_next(datetime) \
        .strftime('%Y-%m-%d %H:%M:%S')


class ScheduleTaskTool(BaseTool):
    name: str = "schedule_task"
    description: str = (
        "创建和管理定时任务（周期执行、定时提醒、cron、schedule、每隔 N 分钟/"
        "每天 X 点做某事）。用户要求「定期/定时/每隔一段时间/每天每周」做某事时"
        "用它建 cron 周期任务，不要用 pause_and_wait 自循环模拟（会耗尽恢复"
        "次数）。cron 按 UTC 解析：北京时间需先换算（UTC = 北京 - 8 小时）。"
        "周期任务每次执行都会以「定时任务」身份跑一遍 query 并把结果发给用户。"
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
                        "action": {
                            "type": "string",
                            "enum": ["create", "list", "toggle"],
                            "description": "create 建周期任务；list 列出现有周期任务；toggle 启用/停用",
                        },
                        "title": {
                            "type": "string",
                            "description": "任务标题（create 必填），如「每10分钟发热门新闻」",
                        },
                        "query": {
                            "type": "string",
                            "description": ("每次触发要执行的任务内容（create 必填），写成给 agent 的"
                                            "指令，如「搜索当前最新热门新闻，整理一条简报发给用户」"),
                        },
                        "cron": {
                            "type": "string",
                            "description": ("cron 表达式（create 必填，UTC 口径）。每10分钟 "
                                            "*/10 * * * *；每小时 0 * * * *；每天 0 9 * * *"),
                        },
                        "task_id": {
                            "type": "integer",
                            "description": "toggle 时的任务 ID",
                        },
                    },
                    "required": ["action"],
                },
            }
        }

    def execute(self, **kwargs) -> str:
        action = kwargs.get("action")
        agent_ctx = kwargs.get("_agent_context")
        session_id = getattr(agent_ctx, "session_id", 1) if agent_ctx else 1
        try:
            if action == "create":
                return self._create(kwargs, session_id)
            elif action == "list":
                return self._list(session_id)
            elif action == "toggle":
                return self._toggle(kwargs.get("task_id"))
            return f"Error: 未知 action '{action}'（create/list/toggle）"
        except Exception as e:
            return f"Error: 定时任务操作失败: {e}"

    def _create(self, kwargs, session_id: int) -> str:
        title = (kwargs.get("title") or "").strip()
        query = (kwargs.get("query") or "").strip()
        cron = (kwargs.get("cron") or "").strip()
        if not (title and query and cron):
            return "Error: create 需要 title、query、cron 三个参数"
        try:
            from croniter import croniter
            croniter(cron)
        except Exception:
            return (f"Error: cron 表达式无效: {cron!r}。格式如 */10 * * * *"
                    "（每10分钟）、0 * * * *（每小时）、0 9 * * *（每天 UTC 9 点）")
        from api.task_core import create_task
        task_id = create_task(
            title=title, user_query=query, task_type='scheduled',
            schedule_cron=cron, schedule_enabled=True, session_id=session_id)
        next_run = _next_run_utc(cron)
        from api.db import db_connect
        conn = db_connect()
        conn.execute("UPDATE tasks SET next_run_at=? WHERE id=?", (next_run, task_id))
        conn.commit()
        conn.close()
        return (f"已创建定时任务 #{task_id}「{title}」（cron: {cron}，UTC）。"
                f"下次触发: {next_run} UTC。到期自动执行并把结果发到本会话，"
                "可用 schedule_task list 查看、toggle 启停。")

    def _list(self, session_id: int) -> str:
        from api.db import db_connect
        conn = db_connect()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, title, schedule_cron, schedule_enabled, next_run_at, status "
            "FROM tasks WHERE task_type='scheduled' AND session_id=? "
            "ORDER BY id DESC LIMIT 20", (session_id,)).fetchall()
        conn.close()
        if not rows:
            return "当前会话没有定时任务。"
        lines = []
        for r in rows:
            state = "启用" if r["schedule_enabled"] else "停用"
            lines.append(f"- #{r['id']}「{r['title']}」cron={r['schedule_cron']} "
                         f"({state}, 下次 {r['next_run_at']} UTC)")
        return "定时任务列表（cron 均为 UTC）：\n" + "\n".join(lines)

    def _toggle(self, task_id) -> str:
        if not task_id:
            return "Error: toggle 需要 task_id"
        from api.db import db_connect
        conn = db_connect()
        row = conn.execute(
            "SELECT schedule_enabled, schedule_cron FROM tasks "
            "WHERE id=? AND task_type='scheduled'", (task_id,)).fetchone()
        if not row:
            conn.close()
            return f"Error: 定时任务 #{task_id} 不存在"
        enabled = 0 if row[0] else 1
        if enabled:
            conn.execute(
                "UPDATE tasks SET schedule_enabled=?, next_run_at=?, "
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (enabled, _next_run_utc(row[1]), task_id))
        else:
            conn.execute(
                "UPDATE tasks SET schedule_enabled=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=?", (enabled, task_id))
        conn.commit()
        conn.close()
        return f"定时任务 #{task_id} 已{'启用' if enabled else '停用'}"
