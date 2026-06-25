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
    from tools.task_plan import load_goals, save_goals, _new_goal_id, _MAX_GOALS, archive_overlapping_goals
    goals = load_goals()
    if len(goals["items"]) >= _MAX_GOALS:
        raise HTTPException(status_code=400, detail=f"目标已达上限 {_MAX_GOALS} 项")
    if not req.desc or not req.desc.strip():
        raise HTTPException(status_code=400, detail="目标描述不能为空")
    desc = req.desc.strip()[:100]
    archive_overlapping_goals(desc, goals["items"])
    new_goal = {
        "id": _new_goal_id(goals["items"]),
        "desc": desc,
        "status": "pending",
        "updated": _time.strftime("%Y-%m-%d %H:%M"),
        "task_ids": [],
        "resume_count": 0,
    }
    goals["items"].append(new_goal)
    save_goals(goals)
    return {"status": "success", "goal": new_goal}


@router.put("/api/goals/{goal_id}")
async def update_goal(goal_id: int, req: GoalUpdateRequest):
    """Update a goal's description and/or status."""
    from tools.task_plan import load_goals, save_goals
    goals = load_goals()
    valid_statuses = {"pending", "doing", "done", "stuck", "archived"}
    for item in goals["items"]:
        if item["id"] == goal_id:
            if req.desc is not None:
                if not req.desc.strip():
                    raise HTTPException(status_code=400, detail="目标描述不能为空")
                item["desc"] = req.desc.strip()[:100]
            if req.status is not None:
                if req.status not in valid_statuses:
                    raise HTTPException(status_code=400, detail=f"无效状态，必须为: {', '.join(valid_statuses)}")
                item["status"] = req.status
            item["updated"] = _time.strftime("%Y-%m-%d %H:%M")
            save_goals(goals)
            return {"status": "success", "goal": item}
    raise HTTPException(status_code=404, detail="目标未找到")


@router.delete("/api/goals/{goal_id}")
async def delete_goal(goal_id: int):
    """Delete a goal by id."""
    from tools.task_plan import load_goals, save_goals
    goals = load_goals()
    for i, item in enumerate(goals["items"]):
        if item["id"] == goal_id:
            goals["items"].pop(i)
            save_goals(goals)
            return {"status": "success", "message": f"目标 #{goal_id} 已删除"}
    raise HTTPException(status_code=404, detail="目标未找到")
