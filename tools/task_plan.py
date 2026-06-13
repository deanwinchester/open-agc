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


# ── Goal List ──

_MAX_GOALS = 10
_MAX_GOAL_DESC = 100


def _get_goals_path():
    from core.paths import get_data_dir
    d = get_data_dir()
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "goals.json")


def load_goals() -> dict:
    path = _get_goals_path()
    # Migration: rename old todos.json if it exists and goals.json doesn't
    old_path = os.path.join(os.path.dirname(path), "todos.json")
    if os.path.exists(old_path) and not os.path.exists(path):
        try:
            os.rename(old_path, path)
        except Exception:
            pass
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Migrate old status values: "todo" → "pending"
            migrated = False
            for item in data.get("items", []):
                if item.get("status") == "todo":
                    item["status"] = "pending"
                    migrated = True
            if migrated:
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass
            return data
        except Exception:
            pass
    return {"items": []}


def save_goals(data: dict) -> bool:
    path = _get_goals_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def _new_goal_id(items: list) -> int:
    return max((i.get("id", 0) for i in items), default=0) + 1


def format_goal_list_for_prompt(goals: dict = None) -> str:
    """Format goals as a compact section for system prompt injection."""
    if goals is None:
        goals = load_goals()
    items = goals.get("items", [])
    if not items:
        return ""
    lines = ["## 当前大目标"]
    icons = {"pending": "⬜", "doing": "🔄", "done": "✅", "stuck": "🔴"}
    for item in items:
        icon = icons.get(item.get("status", "pending"), "⬜")
        desc = item.get("desc", "")[:_MAX_GOAL_DESC]
        updated = item.get("updated", "")
        extra = f"（{updated}）" if updated else ""
        lines.append(f"  {icon} [#{item['id']}] {desc} {extra}".strip())
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
        "管理任务计划和大目标。用于制定任务步骤、跟踪进度、记录关键结果。\n\n"
        "适用场景：\n"
        "- 涉及多步骤的复杂任务（爬虫、批量下载、多文件处理等）\n"
        "- 可能会被中断的长时间任务\n"
        "- 需要恢复上下文的任务\n\n"
        "操作说明：\n"
        "- create：根据 goal 和 steps 创建新计划，系统自动生成 plan_id\n"
        "- update：更新步骤状态(done/doing/todo)、添加关键发现、记录创建的文件\n"
        "- show：查看当前计划内容和进度\n"
        "- check：检查是否所有步骤已完成，未完成时禁止结束任务\n\n"
        "大目标操作：\n"
        "- goal_add(desc=...)：添加一条大目标（最多 10 项）\n"
        "- goal_start(id=N)：标记大目标为执行中\n"
        "- goal_done(id=N)：标记大目标为已完成\n"
        "- goal_stuck(id=N, reason=...)：标记大目标为受阻\n"
        "- goal_reset(id=N)：将完成或受阻的大目标重置为待执行\n"
        "- goal_list()：查看当前所有大目标\n"
        "注意：创建 plan 时请同时添加对应的 goal 项。所有复杂任务都应该有 goal。"
    )

    def execute(self, action: str = "show", goal: str = "",
                steps: list = None, step_id: int = None,
                step_status: str = "", step_result: str = "",
                key_findings: list = None,
                created_files: list = None,
                task_id: int = None,
                desc: str = "", reason: str = "",
                goal_id: int = None,
                **kwargs) -> str:
        # Agent passes _task_id via extra_kwargs, not as named parameter
        if task_id is None and kwargs.get("_task_id"):
            task_id = kwargs["_task_id"]
        steps = steps or []

        # ── Goal operations ──
        if action == "goal_add":
            if not desc:
                return "[TaskPlan] goal_add 需要 desc 参数。"
            goals = load_goals()
            if len(goals["items"]) >= _MAX_GOALS:
                return f"[TaskPlan] ⚠️ 大目标已达上限 {_MAX_GOALS} 项，请先完成一些再添加。"
            if len(desc) > _MAX_GOAL_DESC:
                desc = desc[:_MAX_GOAL_DESC]
            new_goal = {
                "id": _new_goal_id(goals["items"]),
                "desc": desc,
                "status": "pending",
                "updated": time.strftime("%Y-%m-%d %H:%M"),
                "task_id": task_id,
                "resume_count": 0,
            }
            goals["items"].append(new_goal)
            save_goals(goals)
            return f"[TaskPlan] ✅ 已添加大目标: {desc}\n\n{format_goal_list_for_prompt(goals)}"

        if action == "goal_start":
            goals = load_goals()
            for item in goals["items"]:
                if item["id"] == goal_id:
                    item["status"] = "doing"
                    item["updated"] = time.strftime("%Y-%m-%d %H:%M")
                    if task_id:
                        item["task_id"] = task_id
                    save_goals(goals)
                    return f"[TaskPlan] 🔄 已开始: {item['desc']}\n\n{format_goal_list_for_prompt(goals)}"
            return f"[TaskPlan] ⚠️ 未找到 id={goal_id} 的大目标项。"

        if action == "goal_done":
            goals = load_goals()
            for item in goals["items"]:
                if item["id"] == goal_id:
                    desc = item["desc"]
                    goals["items"].remove(item)
                    save_goals(goals)
                    return f"[TaskPlan] ✅ 已完成: {item['desc']}\n\n{format_goal_list_for_prompt(goals)}"
            return f"[TaskPlan] ⚠️ 未找到 id={goal_id} 的大目标项。"

        if action == "goal_stuck":
            goals = load_goals()
            for item in goals["items"]:
                if item["id"] == goal_id:
                    item["status"] = "stuck"
                    item["updated"] = time.strftime("%Y-%m-%d %H:%M")
                    if reason:
                        item["reason"] = reason[:_MAX_GOAL_DESC]
                    save_goals(goals)
                    return f"[TaskPlan] 🔴 已标记受阻: {item['desc']}（{reason or '无原因'}）\n\n{format_goal_list_for_prompt(goals)}"
            return f"[TaskPlan] ⚠️ 未找到 id={goal_id} 的大目标项。"

        if action == "goal_reset":
            goals = load_goals()
            for item in goals["items"]:
                if item["id"] == goal_id:
                    item["status"] = "pending"
                    item["updated"] = time.strftime("%Y-%m-%d %H:%M")
                    item.pop("reason", None)
                    save_goals(goals)
                    return f"[TaskPlan] ⬜ 已重置: {item['desc']}\n\n{format_goal_list_for_prompt(goals)}"
            return f"[TaskPlan] ⚠️ 未找到 id={goal_id} 的大目标项。"

        if action == "goal_list":
            goals = load_goals()
            if not goals["items"]:
                return "[TaskPlan] 📋 当前无大目标。"
            return f"[TaskPlan] 📋 大目标\n\n{format_goal_list_for_prompt(goals)}"

        # ── Plan operations ──
        if action == "create":
            if not goal:
                return "[TaskPlan] 创建计划需要 goal 参数（任务目标）。"
            if not steps:
                return "[TaskPlan] 创建计划需要 steps 参数（执行步骤列表）。"

            plan_id = f"task_{task_id}" if task_id else _goal_hash(goal)
            # Also save under goal_hash for cross-task lookup
            alt_plan_id = _goal_hash(goal) if task_id else None
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

            # Write plan_id back to tasks table so agent can find the plan on next run_turn
            if task_id:
                try:
                    import sqlite3 as _sq3
                    from core.paths import get_data_path as _gdp
                    _conn = _sq3.connect(_gdp("chat_history.db"))
                    _conn.execute("UPDATE tasks SET plan_id=? WHERE id=?", (plan_id, task_id))
                    _conn.commit()
                    _conn.close()
                except Exception:
                    pass

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

        if action == "cleanup":
            import sqlite3 as _sq3
            from core.paths import get_data_path as _gdp
            _conn = _sq3.connect(_gdp("chat_history.db"))
            _removed = 0
            _plans_dir = _get_plans_dir()
            if os.path.isdir(_plans_dir):
                for fn in os.listdir(_plans_dir):
                    if not fn.startswith("plan_") or not fn.endswith(".json"):
                        continue
                    try:
                        with open(os.path.join(_plans_dir, fn), "r") as _f:
                            _p = json.load(_f)
                        _tids = _p.get("task_ids", [])
                        if _tids:
                            _placeholders = ",".join("?" for _ in _tids)
                            _exists = _conn.execute(f"SELECT COUNT(*) FROM tasks WHERE id IN ({_placeholders})", _tids).fetchone()[0]
                            if _exists == 0:
                                os.remove(os.path.join(_plans_dir, fn))
                                _removed += 1
                        else:
                            _mtime = os.path.getmtime(os.path.join(_plans_dir, fn))
                            if time.time() - _mtime > 604800:
                                os.remove(os.path.join(_plans_dir, fn))
                                _removed += 1
                    except Exception:
                        pass
            _conn.close()
            return f"[TaskPlan] 🧹 清理完成，已删除 {_removed} 个无关联计划。"

        return f"[TaskPlan] 未知操作: {action}。支持的操作: create, update, show, check, cleanup"

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
            # Fallback: check DB for plan_id (current task + same session)
            try:
                import sqlite3 as _sq3
                from core.paths import get_data_path as _gdp
                _c = _sq3.connect(_gdp("chat_history.db"))
                # Check current task first
                _r = _c.execute("SELECT plan_id FROM tasks WHERE id=?", (task_id,)).fetchone()
                if _r and _r[0]:
                    p = load_plan(plan_id=_r[0])
                    if p:
                        _c.close()
                        return p
                # Fallback: check other tasks in same session
                _session = _c.execute("SELECT session_id FROM tasks WHERE id=?", (task_id,)).fetchone()
                if _session and _session[0]:
                    for _r2 in _c.execute(
                        "SELECT plan_id FROM tasks WHERE session_id=? AND plan_id != '' AND plan_id IS NOT NULL ORDER BY updated_at DESC LIMIT 5",
                        (_session[0],)
                    ).fetchall():
                        if _r2[0]:
                            p = load_plan(plan_id=_r2[0])
                            if p:
                                _c.close()
                                return p
                _c.close()
            except Exception:
                pass
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
                            "enum": ["create", "update", "show", "check", "cleanup",
                                     "goal_add", "goal_start", "goal_done",
                                     "goal_stuck", "goal_reset", "goal_list"],
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
                        "goal_id": {
                            "type": "integer",
                            "description": "大目标 ID（goal_start/done/stuck/reset 时必填）"
                        },
                        "desc": {
                            "type": "string",
                            "description": "大目标描述（goal_add 时必填，最长 100 字）"
                        },
                        "reason": {
                            "type": "string",
                            "description": "受阻原因（goal_stuck 时必填）"
                        }
                    },
                    "required": ["action"]
                }
            }
        }
