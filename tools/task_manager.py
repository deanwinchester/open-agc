"""Tool for the agent to query and manage tasks (list, search, view deliverables)."""
import json
import os
import sqlite3
from typing import Any, Dict, Optional
from tools.base import BaseTool


class TaskManagerTool(BaseTool):
    name: str = "manage_task"
    description: str = (
        "查看和管理任务。可以列出当前任务、按关键词搜索历史任务、查看任务详情和交付物。\n"
        "用于了解当前工作进度、检索历史任务经验、查看之前任务的产出物。"
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
                            "enum": ["list", "search", "get", "record_deliverable"],
                            "description": (
                                "操作类型：\n"
                                "- list: 列出任务，可按状态筛选\n"
                                "- search: 按关键词搜索任务\n"
                                "- get: 查看任务详情，包括步骤和交付物\n"
                                "- record_deliverable: 记录当前任务的交付物"
                            ),
                        },
                        "task_id": {
                            "type": "integer",
                            "description": "任务ID（get/record_deliverable 时需要）",
                        },
                        "status_filter": {
                            "type": "string",
                            "description": "状态筛选（list 时可选）：running, completed, failed, interrupted, backgrounded",
                        },
                        "keyword": {
                            "type": "string",
                            "description": "搜索关键词（search 时使用）",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "返回条数上限（默认 10）",
                        },
                        "deliverable_description": {
                            "type": "string",
                            "description": "交付物描述（record_deliverable 时需要）",
                        },
                        "deliverable_files": {
                            "type": "string",
                            "description": "交付物文件路径列表，JSON 数组格式（record_deliverable 时可选）",
                        },
                    },
                    "required": ["action"],
                },
            },
        }

    def execute(self, **kwargs) -> str:
        from core.paths import get_data_path

        action = kwargs.get("action", "list")
        task_id = kwargs.get("task_id")
        status_filter = kwargs.get("status_filter", "")
        keyword = kwargs.get("keyword", "")
        limit = min(kwargs.get("limit", 10), 50)

        db_path = get_data_path("chat_history.db")
        if not os.path.exists(db_path):
            return "数据库不存在"

        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row

            if action == "list":
                return self._list_tasks(conn, status_filter, limit)
            elif action == "search":
                return self._search_tasks(conn, keyword, limit)
            elif action == "get":
                return self._get_task_detail(conn, task_id)
            elif action == "record_deliverable":
                desc = kwargs.get("deliverable_description", "")
                files = kwargs.get("deliverable_files", "[]")
                return self._record_deliverable(conn, task_id, desc, files)
            else:
                return f"未知操作: {action}"

            conn.close()
        except Exception as e:
            return f"查询任务失败: {e}"

    def _list_tasks(self, conn, status_filter: str, limit: int) -> str:
        if status_filter:
            rows = conn.execute(
                "SELECT id, title, user_query, status, task_type, result_summary, "
                "output_files, created_at, updated_at, resume_count, max_resume_count "
                "FROM tasks WHERE status=? ORDER BY id DESC LIMIT ?",
                (status_filter, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, title, user_query, status, task_type, result_summary, "
                "output_files, created_at, updated_at, resume_count, max_resume_count "
                "FROM tasks ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()

        if not rows:
            return "暂无任务记录"

        lines = [f"共 {len(rows)} 个任务："]
        for r in rows:
            status_icon = {
                "running": "⏳", "completed": "✅", "failed": "❌",
                "interrupted": "⏸️", "backgrounded": "⏳", "detached": "🟢",
            }.get(r["status"], "📋")
            title = (r["title"] or r["user_query"] or "(无标题)")[:80]
            lines.append(
                f"  #{r['id']} {status_icon} {title} [{r['status']}] "
                f"{r['created_at'][:16] if r['created_at'] else ''}"
            )

        return "\n".join(lines)

    def _search_tasks(self, conn, keyword: str, limit: int) -> str:
        if not keyword:
            return "请提供搜索关键词"
        keyword_like = f"%{keyword}%"
        rows = conn.execute(
            "SELECT id, title, user_query, status, result_summary, created_at "
            "FROM tasks WHERE title LIKE ? OR user_query LIKE ? OR result_summary LIKE ? "
            "ORDER BY id DESC LIMIT ?",
            (keyword_like, keyword_like, keyword_like, limit),
        ).fetchall()

        if not rows:
            return f"未找到包含「{keyword}」的任务"

        lines = [f"搜索「{keyword}」结果（{len(rows)} 条）："]
        for r in rows:
            title = (r["title"] or r["user_query"] or "(无标题)")[:60]
            lines.append(f"  #{r['id']} {title} [{r['status']}]")
        return "\n".join(lines)

    def _get_task_detail(self, conn, task_id: int) -> str:
        if not task_id:
            return "请提供 task_id"
        row = conn.execute(
            "SELECT id, title, user_query, status, task_type, result_summary, "
            "output_files, created_at, updated_at, resume_count "
            "FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        if not row:
            return f"任务 #{task_id} 不存在"

        status_icon = {
            "running": "⏳", "completed": "✅", "failed": "❌",
            "interrupted": "⏸️", "backgrounded": "⏳", "detached": "🟢",
        }.get(row["status"], "📋")

        lines = [
            f"#{row['id']} {status_icon} {row['title'] or '(无标题)'}",
            f"  状态: {row['status']}",
            f"  类型: {row['task_type']}",
            f"  创建: {row['created_at'] or ''}",
            f"  已恢复: {row['resume_count'] or 0} 次",
        ]

        if row["result_summary"]:
            lines.append(f"  执行结果: {row['result_summary'][:500]}")

        # Parse output_files (JSON array)
        output_files = []
        try:
            output_files = json.loads(row["output_files"]) if row["output_files"] else []
        except Exception:
            pass
        if output_files:
            lines.append(f"  交付文件 ({len(output_files)} 个):")
            for f in output_files[:10]:
                lines.append(f"    📄 {f}")

        # Get steps summary
        steps = conn.execute(
            "SELECT tool_name, tool_label, args_preview, result_preview, success, created_at "
            "FROM task_steps WHERE task_id=? ORDER BY step_number DESC LIMIT 10",
            (task_id,),
        ).fetchall()
        if steps:
            lines.append(f"  最近步骤 ({len(steps)} 条):")
            for s in reversed(steps):
                label = s["tool_label"] or s["tool_name"]
                icon = "✅" if s["success"] else ("❌" if s["success"] is False else "⏳")
                lines.append(f"    {icon} {label}")

        # Check if task goal exists
        goal = row["user_query"] or ""
        if goal:
            lines.append(f"  任务目标: {goal[:200]}")

        return "\n".join(lines)

    def _record_deliverable(self, conn, task_id: int, desc: str, files_str: str) -> str:
        if not task_id:
            return "请提供 task_id"
        if not desc:
            return "请提供交付物描述"

        row = conn.execute("SELECT id, result_summary, output_files FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return f"任务 #{task_id} 不存在"

        # Update result_summary
        existing = row["result_summary"] or ""
        new_summary = f"{existing}\n- {desc}" if existing else desc
        new_summary = new_summary.strip()

        # Update output_files (JSON array)
        existing_files = []
        try:
            existing_files = json.loads(row["output_files"]) if row["output_files"] else []
        except Exception:
            pass
        try:
            new_files = json.loads(files_str) if files_str and files_str != "[]" else []
            for f in new_files:
                if f not in existing_files:
                    existing_files.append(f)
        except Exception:
            pass

        conn.execute(
            "UPDATE tasks SET result_summary=?, output_files=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (new_summary, json.dumps(existing_files, ensure_ascii=False), task_id),
        )
        conn.commit()

        parts = ["✅ 交付物已记录"]
        if desc:
            parts.append(f"描述: {desc[:200]}")
        if existing_files:
            parts.append(f"文件: {', '.join(existing_files[:10])}")
        return " | ".join(parts)
