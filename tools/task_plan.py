import json
import os
import re
import time
import hashlib
from typing import Any, Dict, List, Optional
from tools.base import BaseTool


def _atomic_json_write(path: str, data) -> None:
    """Write JSON atomically (tmp file + os.replace), always UTF-8."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


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
                        with open(os.path.join(plans_dir, fn), "r", encoding="utf-8") as f:
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
        _atomic_json_write(path, plan)
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
                    _atomic_json_write(path, data)
                except Exception:
                    pass
            # Migration 2: task_id (int) → task_ids (list)
            migrated_ids = False
            for item in data.get("items", []):
                if "task_id" in item and item["task_id"] is not None:
                    if "task_ids" not in item:
                        item["task_ids"] = [item["task_id"]]
                    del item["task_id"]
                    migrated_ids = True
            if migrated_ids:
                try:
                    _atomic_json_write(path, data)
                except Exception:
                    pass
            return data
        except Exception as e:
            # Never silently fall back to an empty list: the next save_goals
            # would wipe every goal. Keep the original file and fail loudly.
            print(f"[TaskPlan] ERROR: 解析 goals.json 失败 ({path}): {e} — 已保留原文件，请手动检查修复。")
            raise RuntimeError(f"goals.json 解析失败: {path}: {e}") from e
    return {"items": []}


def save_goals(data: dict) -> bool:
    path = _get_goals_path()
    try:
        _atomic_json_write(path, data)
        return True
    except Exception:
        return False


def _new_goal_id(items: list) -> int:
    return max((i.get("id", 0) for i in items), default=0) + 1


def archive_overlapping_goals(new_desc: str, items: list) -> list:
    """Archive existing goals whose descriptions overlap with a new goal.
    Uses keyword overlap to detect duplicates. Returns list of archived goal IDs."""
    if not new_desc or not items:
        return []
    archived_ids = []
    new_lower = new_desc.lower()
    # Extract keywords from new description (split on common delimiters)
    new_tokens = set(re.findall(r'[一-鿿\w]+', new_lower))
    if not new_tokens:
        return []

    for item in items:
        if item.get("status") in ("archived", "done"):
            continue
        old_desc = item.get("desc", "").lower()
        old_tokens = set(re.findall(r'[一-鿿\w]+', old_desc))
        if not old_tokens:
            continue
        # Overlap ratio: intersection / min(len(A), len(B))
        overlap = len(new_tokens & old_tokens) / max(len(old_tokens), 1)
        if overlap >= 0.4:  # 40% keyword overlap → likely same topic
            item["status"] = "archived"
            item["updated"] = time.strftime("%Y-%m-%d %H:%M")
            archived_ids.append(item["id"])
    return archived_ids


def format_goal_list_for_prompt(goals: dict = None) -> str:
    """Format goals as a compact section for system prompt injection."""
    if goals is None:
        goals = load_goals()
    items = goals.get("items", [])
    if not items:
        return ""
    icons = {"pending": "⬜", "doing": "🔄", "done": "✅", "stuck": "🔴", "archived": "📦"}
    lines = ["## 当前大目标"]
    for item in items:
        status = item.get("status", "pending")
        if status == "archived":
            continue  # Don't show archived goals in agent prompt
        icon = icons.get(status, "⬜")
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
        "管理任务计划与大目标。复杂/长任务先建计划；建 plan 应同时 goal_add；check 未完成禁止结束。"
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
            # Archive overlapping goals before adding new one
            _archived = archive_overlapping_goals(desc, goals["items"])
            if _archived:
                _archived_str = ", ".join(f"#{i}" for i in _archived)
                print(f"[TaskPlan] Archived overlapping goals: {_archived_str}")
            new_goal = {
                "id": _new_goal_id(goals["items"]),
                "desc": desc,
                "status": "pending",
                "updated": time.strftime("%Y-%m-%d %H:%M"),
                "task_ids": [task_id] if task_id else [],
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
                        if "task_ids" not in item or not isinstance(item["task_ids"], list):
                            item["task_ids"] = []
                        if task_id not in item["task_ids"]:
                            item["task_ids"].append(task_id)
                    save_goals(goals)
                    return f"[TaskPlan] 🔄 已开始: {item['desc']}\n\n{format_goal_list_for_prompt(goals)}"
            return f"[TaskPlan] ⚠️ 未找到 id={goal_id} 的大目标项。"

        if action == "goal_done":
            goals = load_goals()
            for item in goals["items"]:
                if item["id"] == goal_id:
                    item["status"] = "done"
                    item["updated"] = time.strftime("%Y-%m-%d %H:%M")
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
                    item.pop("archived", None)
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
                        with open(os.path.join(_plans_dir, fn), "r", encoding="utf-8") as _f:
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
                            with open(os.path.join(plans_dir, fn), "r", encoding="utf-8") as f:
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
                            "description": "create 建计划；update 更新；show 看进度；check 查完成；goal_* 管大目标"
                        },
                        "goal": {
                            "type": "string",
                            "description": "任务目标（create 必填）"
                        },
                        "steps": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "步骤列表（create 必填）"
                        },
                        "step_id": {
                            "type": "integer",
                            "description": "步骤编号（update 用）"
                        },
                        "step_status": {
                            "type": "string",
                            "enum": ["done", "doing", "todo"],
                            "description": "新状态（update 用）"
                        },
                        "step_result": {
                            "type": "string",
                            "description": "步骤结果（update 可选）"
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
                            "description": "产出文件及用途"
                        },
                        "goal_id": {
                            "type": "integer",
                            "description": "大目标 ID（goal_* 用）"
                        },
                        "desc": {
                            "type": "string",
                            "description": "大目标描述（goal_add 用，≤100 字）"
                        },
                        "reason": {
                            "type": "string",
                            "description": "受阻原因（goal_stuck 用）"
                        }
                    },
                    "required": ["action"]
                }
            }
        }
