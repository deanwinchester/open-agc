"""Goals API endpoints."""
import time as _time
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()


class GoalCreateRequest(BaseModel):
    desc: str


class GoalUpdateRequest(BaseModel):
    desc: str = None
    status: str = None


@router.get("/api/goals")
async def get_goals():
    """List all goals."""
    from tools.task_plan import load_goals
    return load_goals()


@router.post("/api/goals")
async def create_goal(req: GoalCreateRequest):
    """Create a new goal."""
    from tools.task_plan import (
        update_goals, _new_goal_id, _MAX_GOALS, _ACTIVE_GOAL_STATUSES,
        archive_overlapping_goals,
    )
    if not req.desc or not req.desc.strip():
        raise HTTPException(status_code=400, detail="目标描述不能为空")
    desc = req.desc.strip()[:100]

    def _add(data):
        # 上限只统计 active（pending/doing/stuck）
        _active_n = sum(1 for i in data["items"]
                        if i.get("status") in _ACTIVE_GOAL_STATUSES)
        if _active_n >= _MAX_GOALS:
            return False, None
        archive_overlapping_goals(desc, data["items"])
        new_goal = {
            "id": _new_goal_id(data["items"]),
            "desc": desc,
            "status": "pending",
            "updated": _time.strftime("%Y-%m-%d %H:%M"),
            "task_ids": [],
            "resume_count": 0,
        }
        data["items"].append(new_goal)
        return True, new_goal

    new_goal = update_goals(_add)
    if new_goal is None:
        raise HTTPException(status_code=400, detail=f"进行中的目标已达上限 {_MAX_GOALS} 项")
    return {"status": "success", "goal": new_goal}


@router.put("/api/goals/{goal_id}")
async def update_goal(goal_id: int, req: GoalUpdateRequest):
    """Update a goal's description and/or status."""
    from tools.task_plan import update_goals
    valid_statuses = {"pending", "doing", "done", "stuck", "archived"}
    if req.desc is not None and not req.desc.strip():
        raise HTTPException(status_code=400, detail="目标描述不能为空")
    if req.status is not None and req.status not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"无效状态，必须为: {', '.join(valid_statuses)}")

    def _mut(data):
        for item in data["items"]:
            if item["id"] == goal_id:
                if req.desc is not None:
                    item["desc"] = req.desc.strip()[:100]
                if req.status is not None:
                    item["status"] = req.status
                item["updated"] = _time.strftime("%Y-%m-%d %H:%M")
                return True, dict(item)
        return False, None

    updated = update_goals(_mut)
    if updated is None:
        raise HTTPException(status_code=404, detail="目标未找到")
    return {"status": "success", "goal": updated}


@router.delete("/api/goals/{goal_id}")
async def delete_goal(goal_id: int):
    """Delete a goal by id."""
    from tools.task_plan import update_goals

    def _del(data):
        for i, item in enumerate(data["items"]):
            if item["id"] == goal_id:
                data["items"].pop(i)
                return True, True
        return False, False

    deleted = update_goals(_del)
    if not deleted:
        raise HTTPException(status_code=404, detail="目标未找到")
    return {"status": "success", "message": f"目标 #{goal_id} 已删除"}
