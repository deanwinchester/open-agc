import json
import os
import time
import hashlib
from typing import Any, Dict, List, Optional
from tools.base import BaseTool


def _get_plans_dir():
    from core.paths import get_data_dir
    d = os.path.join(get_data_dir(), "plans")
    os.makedirs(d, exist_ok=True)
    return d


def _goal_hash(goal: str) -> str:
    return hashlib.md5(goal.strip().lower().encode()).hexdigest()[:8]


def _plan_path(goal: str) -> str:
    return os.path.join(_get_plans_dir(), f"plan_{_goal_hash(goal)}.json")


def _plan_path_by_id(plan_id: str) -> str:
    return os.path.join(_get_plans_dir(), f"plan_{plan_id}.json")


def load_plan(goal: str = None, plan_id: str = None, task_id: int = None) -> Optional[dict]:
    if plan_id:
        path = _plan_path_by_id(plan_id)
    elif goal:
        path = _plan_path(goal)
    elif task_id is not None:
        plans_dir = _get_plans_dir()
        if os.path.isdir(plans_dir):
            for fn in os.listdir(plans_dir):
                if fn.startswith("plan_") and fn.endswith(".json"):
                    try:
                        with open(os.path.join(plans_dir, fn), "r") as f:
                            p = json.load(f)
                        if task_id in p.get("task_ids", []) or p.get("current_task_id") == task_id:
                            return p
                    except Exception:
                        continue
        return None
    else:
        return None
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def save_plan(plan: dict) -> bool:
    path = _plan_path_by_id(plan["plan_id"])
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def format_plan_for_prompt(plan: dict) -> str:
    """Format a plan as a readable markdown string for system prompt injection."""
    lines = [
        f"## 📋 任务计划（#{plan.get('plan_id', '?')}）",
        f"**目标**：{plan.get('goal', '未设置')}",
        f"**状态**：{plan.get('status', 'unknown')}",
    ]

    total = len(plan.get("steps", []))
    done = sum(1 for s in plan["steps"] if s["status"] == "done")
    if total:
        pct = int(done / total * 100)
        bars = "█" * (pct // 10) + "░" * (10 - pct // 10)
        lines.append(f"**进度**：{bars} {done}/{total} ({pct}%)")

    lines.append("")
    lines.append("**步骤**：")
    for s in plan.get("steps", []):
        icon = {"done": "✅", "doing": "🔄", "todo": "⬜"}.get(s["status"], "⬜")
        desc = s.get("desc", s.get("description", ""))
        result = s.get("result", "")
        line = f"  {icon} {desc}"
        if result:
            line += f" — {result[:120]}"
        lines.append(line)

    if plan.get("key_findings"):
        lines.append("")
        lines.append("**关键发现**：")
        for k in plan["key_findings"]:
            lines.append(f"  📌 {k[:200]}")

    if plan.get("created_files"):
        lines.append("")
        lines.append("**已创建文件**：")
        for f in plan["created_files"]:
            purpose = f.get("purpose", "")
            fp = f.get("path", "")
            if purpose:
                lines.append(f"  📄 {fp} — {purpose}")
            else:
                lines.append(f"  📄 {fp}")

    return "\n".join(lines)


# ── Todo List ──

_MAX_TODOS = 10
_MAX_TODO_DESC = 100


def _get_todos_path():
    from core.paths import get_data_dir
    d = get_data_dir()
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "todos.json")


def load_todos() -> dict:
    path = _get_todos_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"items": []}


def save_todos(data: dict) -> bool:
    path = _get_todos_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def _new_todo_id(items: list) -> int:
    return max((i.get("id", 0) for i in items), default=0) + 1


def format_todo_list_for_prompt(todos: dict = None) -> str:
    """Format todos as a compact section for system prompt injection."""
    if todos is None:
        todos = load_todos()
    items = todos.get("items", [])
    if not items:
        return ""
    lines = ["## 当前待办事项"]
    icons = {"todo": "⬜", "doing": "🔄", "done": "✅", "stuck": "🔴"}
    for item in items:
        icon = icons.get(item.get("status", "todo"), "⬜")
        desc = item.get("desc", "")[:_MAX_TODO_DESC]
        updated = item.get("updated", "")
        extra = f"（{updated}）" if updated else ""
        lines.append(f"  {icon} {desc} {extra}".strip())
    return "\n".join(lines)


def _heartbeat_plan_context(plan: dict) -> str:
    """Mechanically extract a summary from a plan for heartbeat LLM context."""
    total = len(plan.get("steps", []))
    done = sum(1 for s in plan["steps"] if s["status"] == "done")
    doing = [s for s in plan["steps"] if s["status"] == "doing"]
    lines = [f"目标: {plan.get('goal', '')}", f"步骤进度: {done}/{total}"]
    for s in doing:
        lines.append(f"当前执行中: {s.get('desc', '')}")
        if s.get("result"):
            lines.append(f"  已有结果: {s['result'][:200]}")
    return "\n".join(lines)


class TaskPlanTool(BaseTool):
    name: str = "manage_task_plan"
    description: str = (
        "管理任务计划和待办事项。用于制定任务步骤、跟踪进度、记录关键结果。\n\n"
        "适用场景：\n"
        "- 涉及多步骤的复杂任务（爬虫、批量下载、多文件处理等）\n"
        "- 可能会被中断的长时间任务\n"
        "- 需要恢复上下文的任务\n\n"
        "操作说明：\n"
        "- create：根据 goal 和 steps 创建新计划，系统自动生成 plan_id\n"
        "- update：更新步骤状态(done/doing/todo)、添加关键发现、记录创建的文件\n"
        "- show：查看当前计划内容和进度\n"
        "- check：检查是否所有步骤已完成，未完成时禁止结束任务\n\n"
        "待办事项操作：\n"
        "- todo_add(desc=...)：添加一条待办事项（最多 10 项）\n"
        "- todo_start(id=N)：标记待办为执行中\n"
        "- todo_done(id=N)：标记待办为已完成\n"
        "- todo_stuck(id=N, reason=...)：标记待办为受阻\n"
        "- todo_reset(id=N)：将完成或受阻的待办重置为待执行\n"
        "- todo_list()：查看当前所有待办事项\n"
        "注意：创建 plan 时请同时添加对应的 todo 项。所有复杂任务都应该有 todo。"
    )

    def execute(self, action: str = "show", goal: str = "",
                steps: list = None, step_id: int = None,
                step_status: str = "", step_result: str = "",
                key_findings: list = None,
                created_files: list = None,
                task_id: int = None,
                desc: str = "", reason: str = "",
                todo_id: int = None,
                **kwargs) -> str:
        steps = steps or []

        # ── Todo operations ──
        if action == "todo_add":
            if not desc:
                return "[TaskPlan] todo_add 需要 desc 参数。"
            todos = load_todos()
            if len(todos["items"]) >= _MAX_TODOS:
                return f"[TaskPlan] ⚠️ 待办事项已达上限 {_MAX_TODOS} 项，请先完成一些再添加。"
            if len(desc) > _MAX_TODO_DESC:
                desc = desc[:_MAX_TODO_DESC]
            new_item = {
                "id": _new_todo_id(todos["items"]),
                "desc": desc,
                "status": "todo",
                "updated": time.strftime("%Y-%m-%d %H:%M"),
                "task_id": task_id,
                "resume_count": 0,
            }
            todos["items"].append(new_item)
            save_todos(todos)
            return f"[TaskPlan] ✅ 已添加待办: {desc}\n\n{format_todo_list_for_prompt(todos)}"

        if action == "todo_start":
            todos = load_todos()
            for item in todos["items"]:
                if item["id"] == todo_id:
                    item["status"] = "doing"
                    item["updated"] = time.strftime("%Y-%m-%d %H:%M")
                    if task_id:
                        item["task_id"] = task_id
                    save_todos(todos)
                    return f"[TaskPlan] 🔄 已开始: {item['desc']}\n\n{format_todo_list_for_prompt(todos)}"
            return f"[TaskPlan] ⚠️ 未找到 id={todo_id} 的待办项。"

        if action == "todo_done":
            todos = load_todos()
            for item in todos["items"]:
                if item["id"] == todo_id:
                    item["status"] = "done"
                    item["updated"] = time.strftime("%Y-%m-%d %H:%M")
                    save_todos(todos)
                    return f"[TaskPlan] ✅ 已完成: {item['desc']}\n\n{format_todo_list_for_prompt(todos)}"
            return f"[TaskPlan] ⚠️ 未找到 id={todo_id} 的待办项。"

        if action == "todo_stuck":
            todos = load_todos()
            for item in todos["items"]:
                if item["id"] == todo_id:
                    item["status"] = "stuck"
                    item["updated"] = time.strftime("%Y-%m-%d %H:%M")
                    if reason:
                        item["reason"] = reason[:_MAX_TODO_DESC]
                    save_todos(todos)
                    return f"[TaskPlan] 🔴 已标记受阻: {item['desc']}（{reason or '无原因'}）\n\n{format_todo_list_for_prompt(todos)}"
            return f"[TaskPlan] ⚠️ 未找到 id={todo_id} 的待办项。"

        if action == "todo_reset":
            todos = load_todos()
            for item in todos["items"]:
                if item["id"] == todo_id:
                    item["status"] = "todo"
                    item["updated"] = time.strftime("%Y-%m-%d %H:%M")
                    item.pop("reason", None)
                    save_todos(todos)
                    return f"[TaskPlan] ⬜ 已重置: {item['desc']}\n\n{format_todo_list_for_prompt(todos)}"
            return f"[TaskPlan] ⚠️ 未找到 id={todo_id} 的待办项。"

        if action == "todo_list":
            todos = load_todos()
            if not todos["items"]:
                return "[TaskPlan] 📋 当前无待办事项。"
            return f"[TaskPlan] 📋 待办事项\n\n{format_todo_list_for_prompt(todos)}"

        # ── Plan operations ──
        if action == "create":
            if not goal:
                return "[TaskPlan] 创建计划需要 goal 参数（任务目标）。"
            if not steps:
                return "[TaskPlan] 创建计划需要 steps 参数（执行步骤列表）。"

            plan_id = _goal_hash(goal)
            existing = load_plan(plan_id=plan_id)
            if existing:
                existing["current_task_id"] = task_id
                if task_id and task_id not in existing.get("task_ids", []):
                    existing.setdefault("task_ids", []).append(task_id)
                save_plan(existing)
                return f"[TaskPlan] ✅ 已关联到已有计划 #{plan_id}\n\n{format_plan_for_prompt(existing)}"

            plan = {
                "plan_id": plan_id,
                "goal": goal,
                "status": "in_progress",
                "task_ids": [task_id] if task_id else [],
                "current_task_id": task_id,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "steps": [],
                "key_findings": [],
                "created_files": [],
            }
            for i, s in enumerate(steps, 1):
                if isinstance(s, str):
                    plan["steps"].append({"id": i, "desc": s, "status": "todo", "result": ""})
                elif isinstance(s, dict):
                    plan["steps"].append({
                        "id": i,
                        "desc": s.get("desc", s.get("description", "")),
                        "status": s.get("status", "todo"),
                        "result": s.get("result", ""),
                    })
            plan["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save_plan(plan)
            return f"[TaskPlan] ✅ 计划已创建 (plan_id: {plan_id})\n\n{format_plan_for_prompt(plan)}"

        elif action == "update":
            plan = self._find_plan(goal, task_id)
            if not plan:
                return "[TaskPlan] 未找到关联计划，请先用 create 创建。"

            if step_id is not None:
                for s in plan["steps"]:
                    if s["id"] == step_id:
                        if step_status:
                            s["status"] = step_status
                        if step_result:
                            old = s.get("result", "")
                            s["result"] = (old + "\n" + step_result).strip()[:500] if old else step_result[:500]
                        break

            if key_findings:
                for k in key_findings:
                    if k not in plan.get("key_findings", []):
                        plan.setdefault("key_findings", []).append(k)

            if created_files:
                for f in created_files:
                    if isinstance(f, str):
                        plan.setdefault("created_files", []).append({"path": f, "purpose": ""})
                    elif isinstance(f, dict):
                        plan.setdefault("created_files", []).append(f)

            all_done = all(s["status"] == "done" for s in plan["steps"]) if plan["steps"] else False
            plan["status"] = "completed" if all_done else "in_progress"
            plan["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save_plan(plan)
            return f"[TaskPlan] ✅ 计划已更新\n\n{format_plan_for_prompt(plan)}"

        elif action == "show":
            plan = self._find_plan(goal, task_id)
            if not plan:
                return "[TaskPlan] 未找到关联计划。如需创建计划，请用 create 操作。"
            return f"[TaskPlan] 📋 当前计划\n\n{format_plan_for_prompt(plan)}"

        elif action == "check":
            plan = self._find_plan(goal, task_id)
            if not plan:
                return "[TaskPlan] ✅ 无关联计划（简单任务无需检查）。"

            incomplete = [s for s in plan["steps"] if s["status"] != "done"]
            if not incomplete:
                return "[TaskPlan] ✅ 所有步骤已完成！可以结束任务。"
            else:
                names = "\n".join(f"  {s['id']}. ❌ {s['desc']}" for s in incomplete)
                return (
                    f"[TaskPlan] ⚠️ 以下步骤尚未完成：\n{names}\n\n"
                    f"请继续执行这些步骤，或调用 manage_task_plan 标记确认跳过。"
                )

        return f"[TaskPlan] 未知操作: {action}。支持的操作: create, update, show, check"

    def _find_plan(self, goal: str = None, task_id: int = None) -> Optional[dict]:
        # Try by goal first
        if goal:
            p = load_plan(goal=goal)
            if p:
                return p

        # Try by task_id
        if task_id:
            plans_dir = _get_plans_dir()
            if os.path.isdir(plans_dir):
                for fn in os.listdir(plans_dir):
                    if fn.startswith("plan_") and fn.endswith(".json"):
                        try:
                            with open(os.path.join(plans_dir, fn), "r") as f:
                                p = json.load(f)
                            if task_id in p.get("task_ids", []) or p.get("current_task_id") == task_id:
                                return p
                        except Exception:
                            continue
        return None

    def get_openai_schema(self) -> dict:
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
                            "enum": ["create", "update", "show", "check",
                                     "todo_add", "todo_start", "todo_done",
                                     "todo_stuck", "todo_reset", "todo_list"],
                            "description": "操作类型"
                        },
                        "goal": {
                            "type": "string",
                            "description": "任务目标描述（create 时必填）"
                        },
                        "steps": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "执行步骤列表（create 时必填）"
                        },
                        "step_id": {
                            "type": "integer",
                            "description": "要更新的步骤编号（update 时填）"
                        },
                        "step_status": {
                            "type": "string",
                            "enum": ["done", "doing", "todo"],
                            "description": "步骤的新状态（update 时填）"
                        },
                        "step_result": {
                            "type": "string",
                            "description": "步骤执行结果描述（update 时可选）"
                        },
                        "key_findings": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "关键发现列表"
                        },
                        "created_files": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "purpose": {"type": "string"}
                                }
                            },
                            "description": "创建的文件/文件夹及其用途"
                        },
                        "todo_id": {
                            "type": "integer",
                            "description": "待办事项 ID（todo_start/done/stuck/reset 时必填）"
                        },
                        "desc": {
                            "type": "string",
                            "description": "待办事项描述（todo_add 时必填，最长 100 字）"
                        },
                        "reason": {
                            "type": "string",
                            "description": "受阻原因（todo_stuck 时必填）"
                        }
                    },
                    "required": ["action"]
                }
            }
        }
