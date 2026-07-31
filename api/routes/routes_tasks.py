"""Tasks and Processes API endpoints."""
import os
import json
import re
import shutil
import sqlite3
import asyncio
import time as _time
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.db import DB_PATH
from api import deliverables_registry as _dr
from api.config import load_config
from api.state import _active_agents, _background_agents, connected_websockets, _broadcast_to_websockets, _llamacpp_download_state
from api.task_core import (
    create_task, update_task_status, update_task_type, get_task_context,
    save_task_context, add_task_step, _extract_task_title,
    _record_task_deliverables, _check_goal_completeness,
    kill_tracked_background_process,
)
from tools.shell import (
    interrupt_shell, get_background_processes, get_background_processes_for_task,
    get_orphan_processes, adopt_orphan_processes, _decode_mixed,
)

router = APIRouter()


@router.get("/api/tasks")
async def get_tasks(status: str = None, q: str = None, session_id: int = None,
                    page: int = 1, page_size: int = 50):
    """List tasks with optional status filter, search, and pagination."""
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 200:
        page_size = 50
    offset = (page - 1) * page_size
    conn = sqlite3.connect(DB_PATH, timeout=2)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=2000")
    columns = ("t.id, t.title, t.user_query, t.status, t.task_type, "
               "t.created_at, t.updated_at, t.result_summary, "
               "t.session_id, t.schedule_cron, t.schedule_enabled, "
               "t.next_run_at, t.resume_count, "
               "t.total_tokens, t.total_cost, "
               "t.prompt_tokens, t.completion_tokens, t.cached_tokens")
    conditions = []
    params = []
    if status and status != 'all':
        if status == 'scheduled':
            conditions.append("t.task_type = 'scheduled'")
        else:
            conditions.append("t.status = ?")
            params.append(status)
    if q:
        conditions.append("(t.title LIKE ? OR t.user_query LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    if session_id is not None:
        conditions.append("t.session_id = ?")
        params.append(session_id)
    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
    t0 = _time.time()
    total_count = conn.execute("SELECT COUNT(*) FROM tasks t" + where_clause, params).fetchone()[0]
    query = ("SELECT " + columns + ", sess.name as session_name, "
             "(SELECT COUNT(*) FROM task_steps WHERE task_id = t.id) as step_count "
             "FROM tasks t LEFT JOIN sessions sess ON sess.id = t.session_id" +
             where_clause + " ORDER BY t.created_at DESC LIMIT ? OFFSET ?")
    rows = conn.execute(query, params + [page_size, offset]).fetchall()
    conn.close()
    t2 = _time.time()
    tasks = []
    for row in rows:
        tasks.append({
            "id": row["id"], "title": row["title"], "user_query": row["user_query"],
            "status": row["status"], "task_type": row["task_type"] if "task_type" in row.keys() else "oneshot",
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "result_summary": row["result_summary"],
            "step_count": row["step_count"],
            "session_id": row["session_id"] if "session_id" in row.keys() else None,
            "session_name": row["session_name"] if "session_name" in row.keys() else None,
            "schedule_cron": row["schedule_cron"] if "schedule_cron" in row.keys() else None,
            "schedule_enabled": bool(row["schedule_enabled"]) if "schedule_enabled" in row.keys() else False,
            "next_run_at": row["next_run_at"] if "next_run_at" in row.keys() else None,
            "resume_count": row["resume_count"] if "resume_count" in row.keys() else 0,
            "total_tokens": row["total_tokens"] if "total_tokens" in row.keys() else 0,
            "total_cost": row["total_cost"] if "total_cost" in row.keys() else 0.0,
            "prompt_tokens": row["prompt_tokens"] if "prompt_tokens" in row.keys() else 0,
            "completion_tokens": row["completion_tokens"] if "completion_tokens" in row.keys() else 0,
            "cached_tokens": row["cached_tokens"] if "cached_tokens" in row.keys() else 0,
        })
    return {"tasks": tasks, "total_count": total_count, "page": page, "page_size": page_size}


@router.get("/api/tasks/{task_id}")
async def get_task_detail(task_id: int):
    """Get task detail with all steps."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Task not found")
    steps = conn.execute(
        "SELECT * FROM task_steps WHERE task_id=? ORDER BY created_at ASC", (task_id,)
    ).fetchall()
    conn.close()
    task = dict(row)
    if task.get("output_files"):
        try:
            task["output_files"] = json.loads(task["output_files"])
        except Exception:
            task["output_files"] = []
    task["steps"] = [dict(s) for s in steps]
    task["total_tokens"] = task.get("total_tokens", 0)
    task["total_cost"] = task.get("total_cost", 0.0)
    task["prompt_tokens"] = task.get("prompt_tokens", 0)
    task["completion_tokens"] = task.get("completion_tokens", 0)
    task["cached_tokens"] = task.get("cached_tokens", 0)
    return {"task": task}


@router.get("/api/tasks/{task_id}/steps")
async def get_task_steps(task_id: int, page: int = 1, page_size: int = 50):
    """Get paginated task steps."""
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 200:
        page_size = 50
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    total = conn.execute("SELECT COUNT(*) FROM task_steps WHERE task_id=?", (task_id,)).fetchone()[0]
    pages = max(1, (total + page_size - 1) // page_size)
    offset = (page - 1) * page_size
    rows = conn.execute(
        "SELECT * FROM task_steps WHERE task_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (task_id, page_size, offset)
    ).fetchall()
    conn.close()
    return {"steps": [dict(s) for s in rows], "total": total, "page": page, "page_size": page_size, "total_pages": pages}


@router.post("/api/tasks/{task_id}/interrupt")
async def interrupt_task(task_id: int):
    """Mark a task as interrupted by user and stop its agent."""
    for _agents in _active_agents.values():
        for _aid, _a in list(_agents.items()):
            if _aid == task_id:
                _a.is_interrupted = True
                _a._completed_by_user = True
    for _tid, _bg_a in list(_background_agents.items()):
        if _tid == task_id:
            _bg_a.is_interrupted = True
            _bg_a._completed_by_user = True
    interrupt_shell()
    # 中断任务时联动终止其注册的后台进程（先 kill_tree 杀进程树再清跟踪表；
    # 杀失败不阻断中断流程本身），并在任务上下文注入系统通知
    kill_tracked_background_process(task_id)
    # 中断任务时联动取消进行中的模型下载（此前 globals().get 恒为 None，是死代码）
    if _llamacpp_download_state.get("active"):
        _llamacpp_download_state["cancelled"] = True
    update_task_status(task_id, "interrupted", interruption_reason="user")
    return {"status": "success", "message": "Task marked as interrupted"}


def _normalize_artifact_dirs(task_id: int, raw_dirs: list) -> list:
    """把用户勾选的交付物目录清单规范化为沙箱相对路径列表，并逐项校验
    （在任何删除动作之前调用；任一项非法即整体 400，fail-closed）。

    校验口径（I1/I2，同类输入全走这一个判据）：
    - 拒绝绝对路径、.. 段、带驱动器号的非绝对路径（Windows `C:foo` 能穿过
      os.path.isabs——ntpath 实测——join 后逃逸 root）；
    - 每项必须落在本任务的交付物目录集合内（登记表 ∪ 未登记兜底来源
      outputs/task_<id>/ 与检查点 files_dir）——否则可借清单删任意沙箱内
      路径（如 .checkpoints/task_5.json），绕开"仅顶层条目"限制。
    """
    from api.routes.routes_sandbox import _sandbox_root, _resolve_files_dir
    from api.task_core import read_task_checkpoint
    root = _sandbox_root()
    allowed = {_dr.canon_dir_path(d["dir_path"])
               for d in _dr.get_task_dirs(task_id)}
    outputs_rel = f"outputs/task_{task_id}"
    if os.path.lexists(os.path.join(root, "outputs", f"task_{task_id}")):
        allowed.add(_dr.canon_dir_path(outputs_rel))
    ckpt = read_task_checkpoint(task_id)
    if ckpt:
        files_dir = ckpt.get("files_dir")
        if isinstance(files_dir, str) and files_dir.strip():
            resolved = _resolve_files_dir(root, files_dir)
            if resolved:
                allowed.add(_dr.canon_dir_path(
                    os.path.relpath(resolved, root)))
    norm = []
    for raw in raw_dirs:
        s = str(raw).strip()
        rel_flat = s.replace("\\", "/")
        parts = [p for p in rel_flat.split("/") if p]
        if (not parts or os.path.isabs(s) or os.path.splitdrive(s)[0]
                or any(p in (".", "..") for p in parts)):
            raise HTTPException(
                status_code=400, detail=f"非法交付物路径（拒绝绝对路径/../驱动器相对路径）: {s}")
        rel = _dr.canon_dir_path("/".join(parts))
        if rel not in allowed:
            raise HTTPException(
                status_code=400, detail=f"目录不在本任务的交付物清单内: {rel}")
        norm.append(rel)
    return norm


@router.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int, delete_artifacts: bool = False,
                      artifact_dirs: Optional[str] = None):
    """Delete a task and its associated data.

    交付物删除（登记制 + 共享删除策略），两种入参互斥、artifact_dirs 优先：
    - artifact_dirs（query，JSON 数组字符串）：用户逐目录勾选的交付物目录
      清单（沙箱相对路径，如 ["outputs/唐嫣照片"]）——逐项输入校验（I1/I2，
      见 _normalize_artifact_dirs）+ realpath 校验后删除；共享目录被显式
      勾选同样照删（删除后对其他任务标 missing）；
    - delete_artifacts=true（旧语义保留）：删除本任务全部独占目录（登记表
      独占项 + outputs/task_<id>/ 与检查点 files_dir 兜底——兜底候选同样先
      过登记表共享判定（C1），共享目录只进 skipped_shared 绝不进删除清单）。

    realpath 校验与一期同一判据（逃逸链接只删链接本身不跟随；分区目录
    拒绝），目录不存在不报错；响应带 artifacts_removed/artifacts_errors/
    skipped_shared 明细。任务行删除后连带删除检查点文件（I3）并清掉该
    任务的全部交付物关联（deliverables 行保留作历史，目录缺失由查询方标
    missing）。"""
    # 解析用户勾选的目录清单（JSON 数组）；非法格式在任何删除动作之前 400
    selected_dirs = None
    if artifact_dirs is not None:
        try:
            _parsed_dirs = json.loads(artifact_dirs)
            if not isinstance(_parsed_dirs, list):
                raise ValueError("not a list")
            selected_dirs = [str(x) for x in _parsed_dirs]
        except (ValueError, TypeError):
            raise HTTPException(status_code=400,
                                detail="artifact_dirs 需为 JSON 数组字符串")
        # 逐项校验 + 规范化（I1/I2）——在任何删除动作之前完成
        selected_dirs = _normalize_artifact_dirs(task_id, selected_dirs)
    # Interrupt running agents
    for _agents in _active_agents.values():
        for _aid, _a in list(_agents.items()):
            if _aid == task_id:
                try:
                    _a.is_interrupted = True
                except Exception:
                    pass
    for _tid, _bg_a in list(_background_agents.items()):
        if _tid == task_id:
            try:
                _bg_a.is_interrupted = True
            except Exception:
                pass
    interrupt_shell()
    # Collect and remove temp files
    try:
        conn_tmp = sqlite3.connect(DB_PATH)
        conn_tmp.row_factory = sqlite3.Row
        steps = conn_tmp.execute(
            "SELECT generated_files FROM task_steps WHERE task_id=?", (task_id,)
        ).fetchall()
        for s in steps:
            gf = s["generated_files"]
            if gf:
                try:
                    parsed = json.loads(gf) if isinstance(gf, str) else gf
                    if isinstance(parsed, list):
                        for f in parsed:
                            fpath = f.get("path", "") if isinstance(f, dict) else f
                            if fpath and f.get("type") == "temp":
                                try:
                                    if os.path.exists(fpath):
                                        os.remove(fpath)
                                except Exception:
                                    pass
                except Exception:
                    pass
        conn_tmp.execute("DELETE FROM task_steps WHERE task_id=?", (task_id,))
        conn_tmp.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        conn_tmp.commit()
        conn_tmp.close()
    except Exception as e:
        print(f"[Task] Delete error: {e}")
    # Clean up goal association
    try:
        from tools.task_plan import update_goals as _ug

        def _unlink(data):
            _changed = False
            for item in data.get("items", []):
                tids = item.get("task_ids", [])
                if task_id in tids:
                    item["task_ids"] = [t for t in tids if t != task_id]
                    _changed = True
            return _changed, None

        _ug(_unlink)
    except Exception:
        pass
    # 联动删除交付物目录：realpath 判据与一期 routes_sandbox 一致——逃逸链接
    # 只删链接本身不跟随；分区目录/沙箱根拒绝（记入 errors）；目录不存在不算
    # 错误；多来源同目录时只删一次。
    artifacts_removed = []
    artifacts_errors = []
    skipped_shared = []
    if selected_dirs is not None or delete_artifacts:
        try:
            from api.routes.routes_sandbox import (
                _sandbox_root, _resolve_files_dir, _FORBIDDEN_NAMES)
            from api.task_core import read_task_checkpoint
            root = _sandbox_root()
            root_real = os.path.normcase(os.path.realpath(root))
            forbidden_reals = {root_real} | {
                os.path.normcase(os.path.realpath(os.path.join(root, n)))
                for n in _FORBIDDEN_NAMES}
            candidates = []  # (display, abs_path, expected_real)
            if selected_dirs is not None:
                # 用户勾选清单：已在 _normalize_artifact_dirs 逐项校验（I1/I2），
                # 此处只需拼接候选；删除循环仍逐项 realpath 校验（链接判据）
                for rel in selected_dirs:
                    candidates.append((
                        rel, os.path.join(root, rel),
                        os.path.normcase(os.path.join(root_real, rel))))
            else:
                # delete_artifacts=true 旧语义：登记表独占目录删除、共享跳过
                # （task_ids 只含存活任务——I3，死任务残留关联不构成共享）
                for d in _dr.get_task_dirs(task_id):
                    others = [t for t in d["task_ids"] if t != task_id]
                    if others:
                        skipped_shared.append(
                            {"dir": d["dir_path"], "shared_with": others})
                        continue
                    rel = d["dir_path"]
                    candidates.append((
                        rel, os.path.join(root, rel),
                        os.path.normcase(os.path.join(root_real, rel))))
                # 未登记来源兜底：outputs/task_<id>/ 与检查点 files_dir。
                # C1：兜底候选先过登记表共享判定——共享目录只进 skipped_shared，
                # 绝不进删除清单（否则刚被判共享跳过的目录会被兜底分支重新加回，
                # rmtree 连坐删掉其他任务在用的交付物）。
                fallback = []
                outputs_dir = os.path.join(root, "outputs", f"task_{task_id}")
                if os.path.lexists(outputs_dir):
                    fallback.append((
                        f"outputs/task_{task_id}", outputs_dir,
                        os.path.normcase(os.path.join(root_real, "outputs",
                                                      f"task_{task_id}"))))
                ckpt = read_task_checkpoint(task_id)
                if ckpt:
                    files_dir = ckpt.get("files_dir")
                    if isinstance(files_dir, str) and files_dir.strip():
                        # _resolve_files_dir 已做 realpath 沙箱内校验（越出返回
                        # None，与 artifacts 端点同一口径——逃逸来源跳过不算错误）
                        resolved = _resolve_files_dir(root, files_dir)
                        if resolved:
                            fallback.append((
                                os.path.relpath(resolved, root).replace("\\", "/"),
                                resolved, os.path.normcase(resolved)))
                if fallback:
                    reg_map = _dr.get_dirs_map([rel for rel, _, _ in fallback])
                    skipped_dirs = {s["dir"] for s in skipped_shared}
                    for rel, abs_path, expected_real in fallback:
                        info = reg_map.get(_dr.canon_dir_path(rel))
                        others = ([t for t in info["task_ids"] if t != task_id]
                                  if info else [])
                        if others:
                            if rel not in skipped_dirs:
                                skipped_shared.append(
                                    {"dir": rel, "shared_with": others})
                                skipped_dirs.add(rel)
                            continue
                        candidates.append((rel, abs_path, expected_real))
            seen = set()
            for display, path, expected_real in candidates:
                if not os.path.lexists(path):
                    continue  # 目录不存在不算错误（登记保留历史）
                real = os.path.normcase(os.path.realpath(path))
                if real in seen:
                    continue  # 多来源同目录 → 只删一次
                seen.add(real)
                try:
                    if real != expected_real:
                        # 逃逸链接（junction/symlink）：只删链接本身，不跟随目标
                        if os.path.islink(path):
                            os.remove(path)
                        else:
                            os.rmdir(path)
                    elif real in forbidden_reals:
                        artifacts_errors.append(
                            {"path": display, "error": "拒绝删除分区目录"})
                        continue
                    elif os.path.isdir(path):
                        shutil.rmtree(path)
                    else:
                        os.remove(path)
                    artifacts_removed.append(display)
                except OSError as e:
                    artifacts_errors.append({"path": display, "error": str(e)})
        except Exception as e:
            print(f"[Task] Delete artifacts error: {e}")
            artifacts_errors.append({"path": "", "error": str(e)})
    # 任务行已删：连带删除检查点文件（I3——残留 .checkpoints/task_<id>.json
    # 会在任务 id 复用/恢复路径被误读；realpath 判据同一期：.checkpoints 若是
    # 逃逸链接父级则不跟随，只打印跳过；文件本身是 symlink 时 os.remove 只删
    # 链接不删目标）并清掉该任务全部交付物关联（放目录删除之后——legacy 分支
    # 与清单校验依赖读取检查点与关联；deliverables 行保留作历史）
    try:
        from api.routes.routes_sandbox import _sandbox_root as _ck_root_fn
        _ck_root = _ck_root_fn()
        _ck_path = os.path.join(_ck_root, ".checkpoints", f"task_{task_id}.json")
        if os.path.lexists(_ck_path):
            _ck_real = os.path.normcase(os.path.realpath(_ck_path))
            _ck_expected = os.path.normcase(os.path.join(
                os.path.normcase(os.path.realpath(_ck_root)),
                ".checkpoints", f"task_{task_id}.json"))
            if _ck_real == _ck_expected or os.path.islink(_ck_path):
                os.remove(_ck_path)
            else:
                print(f"[Task] 跳过逃逸链接检查点: {_ck_path}")
    except Exception as _ck_err:
        print(f"[Task] Delete checkpoint error: {_ck_err}")
    _dr.remove_task_links(task_id)
    return {"status": "success", "message": "Task deleted",
            "artifacts_deleted": bool(artifacts_removed),
            "artifacts_removed": artifacts_removed,
            "artifacts_errors": artifacts_errors,
            "skipped_shared": skipped_shared}


@router.post("/api/tasks/{task_id}/reset-resume")
async def reset_task_resume(task_id: int):
    """Reset a task's resume_count to 0."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE tasks SET resume_count=0, updated_at=CURRENT_TIMESTAMP WHERE id=?", (task_id,))
    conn.commit()
    conn.close()
    return {"status": "success"}


@router.post("/api/tasks/{task_id}/complete")
async def complete_task(task_id: int):
    """Manually mark a task as completed — also stops the running agent."""
    from api.state import _active_agents, _background_agents
    from tools.shell import interrupt_shell
    # Stop any running agent for this task
    for _agents in list(_active_agents.values()):
        for _aid, _a in list(_agents.items()):
            if _aid == task_id:
                _a.is_interrupted = True
                _a._completed_by_user = True
    # Stop any background agent
    for _tid, _bg_a in list(_background_agents.items()):
        if _tid == task_id:
            _bg_a.is_interrupted = True
    # Kill shell process
    interrupt_shell()
    # Update DB（顺带清掉历史中断原因：已收官任务不再属于中断语义）
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE tasks SET status='completed', interruption_reason=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (task_id,))
    conn.commit()
    conn.close()
    return {"status": "success", "message": "任务已标记为完成，agent 已停止"}


class ResumeTaskRequest(BaseModel):
    extra_instruction: Optional[str] = None


@router.post("/api/tasks/{task_id}/resume")
async def resume_task(task_id: int, req: ResumeTaskRequest = None):
    """手动恢复任务（任务详情页「▶ 继续」按钮，可带附加指令）。

    与 WS {type:'resume'} 同一恢复链路（api.background.resume_task_manual）：
    活 agent 排队投递 / CAS 认领 + 附加指令注入恢复上下文 + 后台恢复。
    只有 interrupted/backgrounded/background_failed/failed/completed 可恢复；
    running 或其他状态 409，任务不存在 404。
    """
    from api.background import resume_task_manual
    result = resume_task_manual(task_id, (req.extra_instruction or "") if req else "")
    if result.get("ok"):
        return {"status": "success", "resumed": result.get("status") == "resumed",
                "message": result["message"]}
    if result.get("error") == "not_found":
        raise HTTPException(status_code=404, detail=result["message"])
    # 不可恢复状态 / 认领冲突 —— 明确 409 而非含糊 200
    raise HTTPException(status_code=409, detail=result["message"])


# ── Schedule ──

def _next_run_utc(cron: str) -> str:
    """Compute next run time in UTC, DB format 'YYYY-MM-DD HH:MM:SS'.

    The scheduler (api/background.py start_task_scheduler) compares next_run_at
    against datetime.now(timezone.utc), so ALL write sites must store UTC."""
    from croniter import croniter
    return croniter(cron, datetime.now(timezone.utc)).get_next(datetime).strftime('%Y-%m-%d %H:%M:%S')


class ScheduleTaskRequest(BaseModel):
    title: str
    query: str
    cron: str
    session_id: int = 1


@router.post("/api/tasks/schedule")
async def create_scheduled_task(req: ScheduleTaskRequest):
    """Create a scheduled task."""
    try:
        from croniter import croniter
        croniter(req.cron)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid cron expression")
    task_id = create_task(
        title=req.title, user_query=req.query, task_type='scheduled',
        schedule_cron=req.cron, schedule_enabled=True, session_id=req.session_id
    )
    # create_task() does not set next_run_at — set it here (UTC, like update/toggle)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE tasks SET next_run_at=? WHERE id=?", (_next_run_utc(req.cron), task_id))
    conn.commit()
    conn.close()
    return {"status": "success", "task_id": task_id}


@router.post("/api/tasks/{task_id}/toggle-schedule")
async def toggle_schedule(task_id: int):
    """Toggle a scheduled task on/off."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT schedule_enabled FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Task not found")
    enabled = 0 if row[0] else 1
    if enabled:
        try:
            cron = conn.execute("SELECT schedule_cron FROM tasks WHERE id=?", (task_id,)).fetchone()
            if cron and cron[0]:
                next_run = _next_run_utc(cron[0])
                conn.execute("UPDATE tasks SET schedule_enabled=?, next_run_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                             (enabled, next_run, task_id))
            else:
                conn.execute("UPDATE tasks SET schedule_enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                             (enabled, task_id))
        except Exception as e:
            conn.close()
            raise HTTPException(status_code=500, detail=f"Failed to enable schedule: {e}")
    else:
        conn.execute("UPDATE tasks SET schedule_enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (enabled, task_id))
    conn.commit()
    conn.close()
    return {"status": "success", "enabled": bool(enabled)}


@router.put("/api/tasks/{task_id}/schedule")
async def update_schedule(task_id: int, req: ScheduleTaskRequest):
    """Update a scheduled task's config."""
    try:
        from croniter import croniter
        croniter(req.cron)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid cron expression")
    next_run = _next_run_utc(req.cron)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE tasks SET title=?, user_query=?, schedule_cron=?, next_run_at=?, "
        "updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (req.title, req.query, req.cron, next_run, task_id)
    )
    conn.commit()
    conn.close()
    return {"status": "success"}


# ── Process Management ──

def _sandbox_dir_from_config() -> Optional[str]:
    """与 tools/shell.py 口径一致：sandbox_mode 开启时返回 sandbox_dir 绝对路径。"""
    try:
        config = load_config()
        if not config.get("sandbox_mode", True):
            return None
        sandbox_dir = config.get("sandbox_dir") or os.path.abspath(
            os.path.join(os.getcwd(), "workspace"))
        return os.path.abspath(sandbox_dir)
    except Exception:
        return None


def _path_within(path: str, root_norm: str) -> bool:
    """path 等于 root 或为其子孙路径（root_norm 已 normcase+abspath+去尾斜杠）。

    路径边界匹配：`workspace2` 这类兄弟目录不会误命中（旧实现是子串
    包含，无边界）。
    """
    try:
        p = os.path.normcase(os.path.abspath(path))
    except Exception:
        return False
    return p == root_norm or p.startswith(root_norm + os.sep)


def _pid_matches_sandbox(proc, sandbox_dir: str) -> bool:
    """进程的 cwd 或 cmdline 中的路径 token 命中 sandbox 目录（路径边界匹配）。"""
    root = os.path.normcase(os.path.abspath(sandbox_dir)).rstrip("/\\") or \
        os.path.normcase(os.path.abspath(sandbox_dir))
    try:
        cwd = proc.cwd()
        if cwd and _path_within(cwd, root):
            return True
    except Exception:
        pass
    try:
        cmdline = proc.cmdline() or []
    except Exception:
        return False
    for token in cmdline:
        if not token or not isinstance(token, str):
            continue
        candidates = [token]
        if "=" in token:  # --dir=/path/to/x 形式
            candidates.append(token.split("=", 1)[1])
        for cand in candidates:
            t = cand.strip().strip('"').strip("'")
            if not t:
                continue
            # 只尝试像路径的 token（含路径分隔符），普通参数不当路径解析
            if os.sep not in t and (os.altsep is None or os.altsep not in t):
                continue
            if _path_within(t, root):
                return True
    return False


def _protected_pid_set() -> set:
    """服务进程自身 + 祖先链 pid 集合（每次扫描只算一次）。
    此前逐进程调 check_protected_pid，每个都重建 Process(server).parents()
    ——394 个进程 × ~90ms = 35s 同步阻塞事件循环（全站卡死根因）。"""
    protected = set()
    try:
        from api.state import _server_pid
        import psutil
        if _server_pid:
            protected.add(_server_pid)
            protected.update(a.pid for a in psutil.Process(_server_pid).parents()[:3])
    except Exception:
        pass
    return protected


def _discover_sandbox_processes(exclude_pids: set, limit: int = 50) -> list:
    """OS 扫描兜底：cwd/cmdline 命中 sandbox 目录、却不在追踪表里的进程。

    排除本服务进程自身及其祖先（保护集合每次扫描计算一次，见
    _protected_pid_set）。每项给 pid/name/cmdline(截断)/create_time/uptime。
    """
    import psutil
    sandbox_dir = _sandbox_dir_from_config()
    if not sandbox_dir:
        return []
    skip_pids = set(exclude_pids) | _protected_pid_set()
    found = []
    now = _time.time()
    for proc in psutil.process_iter(["pid", "name", "create_time"]):
        try:
            pid = proc.pid
            if pid in skip_pids:
                continue
            if not _pid_matches_sandbox(proc, sandbox_dir):
                continue
            try:
                cmdline = " ".join(str(c) for c in proc.cmdline())[:200]
            except Exception:
                cmdline = ""
            create_time = proc.create_time()
            found.append({
                "pid": pid,
                "name": proc.name(),
                "cmdline": cmdline,
                "create_time": create_time,
                "uptime": max(0, round(now - create_time, 1)),
            })
        except Exception:
            continue
    found.sort(key=lambda p: p["create_time"], reverse=True)
    return found[:limit]


# discovered 扫描结果缓存：进程页有轮询刷新，每次全量扫描（含 cmdline
# 读取）仍有秒级成本，TTL 内复用结果，请求时按最新追踪表过滤即可。
import threading as _threading
_discovered_lock = _threading.Lock()
_discovered_cache = {"ts": 0.0, "sandbox": None, "items": []}
_DISCOVERED_TTL = 10.0


def _discover_cached(tracked_pids: set) -> list:
    now = _time.time()
    sandbox_dir = _sandbox_dir_from_config()
    with _discovered_lock:
        fresh = (now - _discovered_cache["ts"] < _DISCOVERED_TTL
                 and _discovered_cache["sandbox"] == sandbox_dir)
        items = list(_discovered_cache["items"]) if fresh else None
    if items is None:
        items = _discover_sandbox_processes(set())
        with _discovered_lock:
            _discovered_cache["ts"] = now
            _discovered_cache["sandbox"] = sandbox_dir
            _discovered_cache["items"] = list(items)
    return [p for p in items if p["pid"] not in tracked_pids]


def _reaped_row(entry: dict) -> dict:
    """把僵尸回收条目整形成响应行：alive/reaped 标志明确；输出文件还在
    → 保留路径（想查日志有据可循），已删 → output_file_deleted 标记。"""
    of = entry.get("output_file", "")
    exists = bool(of) and os.path.exists(of)
    now = _time.time()
    started = entry.get("started_at")
    return {
        "pid": entry.get("pid"),
        "task_id": entry.get("task_id"),
        "command": entry.get("command", ""),
        "started_at": started,
        "alive": False,
        "reaped": True,
        "uptime": round(now - started, 1) if started else 0,
        "output_file": of if exists else "",
        "output_file_deleted": bool(of) and not exists,
    }


@router.get("/api/processes")
async def list_processes():
    """List all running background shell processes (tracked + orphans + OS scan).

    processes: 每进程一行——主表按 {task_id:pid} 展平（一任务可多个进程），
    orphan 池保持 orphan_id 键；每项含 task_id/pid/command/started_at/alive/uptime。
    返回前惰性回收死 pid 条目（任何状态的任务都可能留僵尸条目）；本次
    被回收的条目带 alive=false/reaped=true 标志返回一次，之后便不再出现。
    discovered: psutil 全盘扫描兜底（10s TTL 缓存），cwd/cmdline 命中 sandbox
    目录但未被追踪的野生进程（排除服务自身及祖先、已在 bg/orphan 表里的 pid）。
    整个处理移到执行器线程：同步 psutil/DB 操作不得在事件循环上跑（曾因此
    全站卡死 35s）。
    """
    return await asyncio.get_running_loop().run_in_executor(None, _build_processes_payload)


def _build_processes_payload() -> dict:
    from tools.shell_interact import _is_pid_alive
    from tools.shell import reap_dead_background_processes
    reaped = reap_dead_background_processes()
    procs = {}
    tracked_pids = set()
    for tid, task_procs in get_background_processes().items():
        for pid_key, info in task_procs.items():
            pinfo = dict(info)
            pid = pinfo.get("pid")
            if pid:
                tracked_pids.add(int(pid))
            pinfo["task_id"] = tid
            pinfo["alive"] = _is_pid_alive(pid) if pid else False
            pinfo["uptime"] = _time.time() - pinfo.get("started_at", _time.time()) if pinfo.get("started_at") else 0
            procs[f"{tid}:{pid_key}"] = pinfo
    for oid, info in get_orphan_processes().items():
        pinfo = dict(info)
        pid = pinfo.get("pid")
        if pid:
            tracked_pids.add(int(pid))
        pinfo["alive"] = _is_pid_alive(pid) if pid else False
        pinfo["uptime"] = _time.time() - pinfo.get("started_at", _time.time()) if pinfo.get("started_at") else 0
        procs[oid] = pinfo
    for entry in reaped:
        row = _reaped_row(entry)
        pid = row["pid"]
        key = (f"{entry['task_id']}:{pid}" if entry.get("task_id")
               else entry.get("orphan_id") or f"reaped:{pid}")
        procs[key] = row
    try:
        discovered = _discover_cached(tracked_pids)
    except Exception as e:
        print(f"[Processes] OS scan error: {e}")
        discovered = []
    return {"processes": procs, "discovered": discovered}


@router.post("/api/processes/{pid}/kill")
async def kill_wild_process(pid: int):
    """Kill a process tree by pid (kill_tree 整棵树)——进程页统一的终止入口。

    安全约束（服务端强制重校验，防端点被用来杀任意进程）：
    - Open-AGC 服务自身及其祖先：403（最先检查）；
    - pid 在注册表（主表或 orphan 池）：放行——登记即归属，与 sandbox
      开关无关（否则 sandbox_mode 关闭时进程页终止按钮全部 403）；
    - 不在注册表（discovered 野生进程）：必须 cwd/cmdline 命中 sandbox
      目录（路径边界匹配），否则 403；进程不存在 404；sandbox 未启用 403。

    杀完同步清追踪表；若 pid 属于某任务且是该任务最后一个存活进程，
    复用 /api/tasks/{id}/kill 的状态同步语义（守卫 UPDATE 置 interrupted、
    注入通知、置 agent 中断标志）——否则"条目被移除而非由活转死"会让
    BgMonitor 的"全死才恢复"永不触发，backgrounded 任务最长卡 6 小时。
    任务还有其他存活 pid 时只清该 pid 条目，不动任务。
    """
    import psutil
    from api.state import check_protected_pid
    from core.process import kill_tree as _kill_tree, pid_alive as _pid_alive
    from tools.shell import (find_task_for_pid, find_orphan_for_pid,
                             cleanup_background_pid, cleanup_orphan_pid,
                             cleanup_background_process,
                             get_background_processes_for_task)
    if pid <= 0:
        raise HTTPException(status_code=400, detail="Invalid pid")
    if check_protected_pid(pid):
        raise HTTPException(status_code=403,
                            detail=f"PID {pid} 是 Open-AGC 服务进程或其祖先，禁止终止")
    owner = find_task_for_pid(pid)                        # (task_id, info) | None
    orphan_hit = None if owner else find_orphan_for_pid(pid)  # (orphan_id, info) | None
    if owner is None and orphan_hit is None:
        # discovered 野生进程：必须命中 sandbox 归属（路径边界匹配）
        sandbox_dir = _sandbox_dir_from_config()
        if not sandbox_dir:
            raise HTTPException(status_code=403,
                                detail="Sandbox 未启用，无法校验进程归属，拒绝终止")
        try:
            proc = psutil.Process(pid)
            matched = _pid_matches_sandbox(proc, sandbox_dir)
        except psutil.NoSuchProcess:
            raise HTTPException(status_code=404, detail=f"PID {pid} 不存在")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"进程校验失败: {e}")
        if not matched:
            raise HTTPException(status_code=403,
                                detail=f"PID {pid} 的 cwd/cmdline 与 sandbox 目录无关，拒绝终止")
    info = owner[1] if owner else (orphan_hit[1] if orphan_hit else None)
    _kill_tree(pid)
    # 同步清出追踪表（若曾被追踪）
    cleanup_background_pid(pid)
    cleanup_orphan_pid(pid)
    resp = {"status": "success", "killed_pid": pid}
    if owner is not None:
        tid_str = owner[0]
        resp["task_id"] = tid_str
        try:
            tid_int = int(tid_str)
        except (TypeError, ValueError):
            tid_int = None
        if tid_int is not None:
            # 顺手回收该任务其余死条目，再判定是否还有存活进程
            remaining = get_background_processes_for_task(tid_int)
            for _k, _info in list(remaining.items()):
                if not _info.get("pid") or not _pid_alive(_info["pid"]):
                    cleanup_background_process(tid_str, _k)
                    remaining.pop(_k, None)
            if remaining:
                # 还有其他存活 pid：只清条目，不动任务
                resp["task_interrupted"] = False
                resp["remaining_pids"] = [i.get("pid") for i in remaining.values()]
            else:
                # 最后一个进程已被杀：复用 /api/tasks/{id}/kill 的状态同步语义
                output_text = ""
                if info and info.get("output_file"):
                    try:
                        with open(info["output_file"], "r", encoding="utf-8", errors="replace") as f:
                            output_text = f.read()
                    except Exception:
                        pass
                if output_text:
                    try:
                        from core.secrets import mask_secrets
                        output_text = mask_secrets(output_text)
                    except Exception:
                        pass
                resp["task_interrupted"] = _interrupt_task_after_process_kill(tid_int, output_text)
    return resp


@router.get("/api/agent/effectiveness")
async def get_agent_effectiveness():
    """Read-only aggregate agent effectiveness metrics from chat_history.db.

    All figures are computed with SELECT aggregates (no full-table loads):
    - status_counts: task count per status
    - tasks_last_7d / tasks_last_30d: recent task volume
    - avg_steps_per_task: average task_steps count per task that has steps
    - tool_success_rate: share of successful task_steps
    - top_tools: top 10 tools by call count with per-tool success rate
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        status_counts = {
            row[0]: row[1]
            for row in conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status").fetchall()
        }
        tasks_total = sum(status_counts.values())
        tasks_last_7d = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE created_at >= datetime('now', '-7 days')"
        ).fetchone()[0]
        tasks_last_30d = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE created_at >= datetime('now', '-30 days')"
        ).fetchone()[0]
        avg_steps = conn.execute(
            "SELECT AVG(cnt) FROM (SELECT COUNT(*) AS cnt FROM task_steps GROUP BY task_id)"
        ).fetchone()[0]
        tool_total, tool_ok = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(success), 0) FROM task_steps"
        ).fetchone()
        top_tools = [
            {"tool_name": r[0], "calls": r[1],
             "success_rate": round((r[2] or 0) / r[1], 4) if r[1] else 0.0}
            for r in conn.execute(
                "SELECT tool_name, COUNT(*) AS cnt, COALESCE(SUM(success), 0) AS ok "
                "FROM task_steps GROUP BY tool_name ORDER BY cnt DESC LIMIT 10"
            ).fetchall()
        ]
    finally:
        conn.close()
    return {
        "status_counts": status_counts,
        "tasks_total": tasks_total,
        "tasks_last_7d": tasks_last_7d,
        "tasks_last_30d": tasks_last_30d,
        "avg_steps_per_task": round(avg_steps, 2) if avg_steps is not None else 0.0,
        "tool_calls_total": tool_total,
        "tool_success_rate": round(tool_ok / tool_total, 4) if tool_total else 0.0,
        "top_tools": top_tools,
    }


@router.get("/api/tasks/{task_id}/process")
async def get_task_process(task_id: int):
    """Get process info for a task (ALL tracked processes,真实 alive 标志).

    返回前惰性回收 pid 已死的条目（任何状态的任务都可能留僵尸条目——
    BgMonitor 只盯 backgrounded，running 任务的死条目此前永远显示
    "运行中"）；本次被回收的条目带 alive=false/reaped=true 标志返回一次
    （输出文件还在→保留路径，已删→output_file_deleted 标记）。Also
    adopts orphans.
    """
    from tools.shell_interact import _is_pid_alive
    from tools.shell import reap_dead_background_processes
    # Try to adopt any orphans that might belong to this task
    adopt_orphan_processes(task_id)
    reaped = [e for e in reap_dead_background_processes()
              if e.get("task_id") == str(task_id)]
    procs = get_background_processes_for_task(task_id)
    orphan = get_orphan_processes().get(str(task_id))
    if orphan:
        procs = dict(procs)
        procs[str(orphan.get("pid") or "orphan")] = orphan
    now = _time.time()
    rows = [{
        "pid": info.get("pid"),
        "command": info.get("command", ""),
        "alive": _is_pid_alive(info.get("pid")) if info.get("pid") else False,
        "uptime": round(now - info.get("started_at", now), 1),
        "output_file": info.get("output_file", ""),
    } for info in procs.values()]
    rows.extend(_reaped_row(e) for e in reaped)
    if not rows:
        return {"process": None, "processes": []}
    # 兼容旧契约：process 返回第一个存活进程（无存活则给第一个回收行——
    # alive=false 如实呈现，不再谎称"运行中"）；processes 给出全部
    alive_rows = [r for r in rows if r.get("alive")]
    return {"process": alive_rows[0] if alive_rows else rows[0], "processes": rows}


@router.get("/api/tasks/{task_id}/logs")
async def get_task_logs(task_id: int, lines: int = 50):
    """Get tail of a task's process output file."""
    output_path = None
    for _pk, pinfo in get_background_processes_for_task(task_id).items():
        if pinfo.get("output_file"):
            output_path = pinfo["output_file"]
            break
    if not output_path:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT output_files FROM tasks WHERE id=?", (task_id,)).fetchone()
        conn.close()
        if row and row[0]:
            try:
                files = json.loads(row[0])
                output_path = files[0] if isinstance(files, list) and files else None
            except Exception:
                output_path = None
        else:
            output_path = None
    if not output_path or not os.path.exists(output_path):
        return {"logs": "", "lines": []}
    # 进程日志是原始字节，可能逐行混杂 UTF-8/GBK（cmd 内建 vs python 子进程），
    # 整块 utf-8+replace 会把 GBK 行全变 �；按行解码后再截取行数
    with open(output_path, "rb") as f:
        raw = f.read()
    all_lines = _decode_mixed(raw).splitlines(keepends=True)
    selected = all_lines[-lines:]
    # Raw shell output files may contain credentials — mask before returning
    try:
        from core.secrets import mask_secrets
        selected = [mask_secrets(line) for line in selected]
    except Exception:
        pass
    return {"logs": "".join(selected), "lines": selected}


def _interrupt_task_after_process_kill(task_id: int, output_text: str) -> bool:
    """进程被杀后的任务状态同步（/api/tasks/{id}/kill 与
    /api/processes/{pid}/kill 共用）。

    有输出则注入 "Process killed by user. + 输出尾部" 上下文；状态同步走
    守卫式 UPDATE（对齐 api/background.py 的 _flip_bg_to_interrupted
    先例）——仅 backgrounded/running 才翻转 interrupted（reason=user，
    resume_count 复位，清 wake_at），rowcount 判断是否真正翻转；窗口内
    刚 completed 的任务不被覆写（result_summary、resume_count 连带不动）。
    UPDATE 自身失败（库异常）时保持原有行为（仍置 interrupted）。翻转
    成功时同步置运行中 agent 的中断标志（机制同 WS/REST interrupt 路径，
    防双 agent 并行）。返回是否真正翻转。
    """
    if output_text:
        context = get_task_context(task_id) or []
        context.append({"role": "system", "content": f"Process killed by user.\n---Output---\n{output_text[-3000:]}"})
        save_task_context(task_id, context)
    task_interrupted = False
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(
            "UPDATE tasks SET status='interrupted', result_summary=?, "
            "interruption_reason='user', resume_count=0, wake_at=NULL, "
            "updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND status IN ('backgrounded','running')",
            (output_text[-200:], task_id))
        conn.commit()
        task_interrupted = cur.rowcount == 1
    except Exception:
        update_task_status(task_id, "interrupted", output_text[-200:], interruption_reason="user")
        conn.execute("UPDATE tasks SET resume_count=0 WHERE id=?", (task_id,))
        conn.commit()
        task_interrupted = True
    conn.close()
    if task_interrupted:
        # 同步置运行中 agent 的中断标志——否则 agent 继续跑而 UI 显示
        # interrupted，且 Guardian/WS resume 的 CAS 可认领同一任务导致双
        # agent 并行。找不到活实例（如重启后状态残留）时只翻转状态即可。
        for _agents in _active_agents.values():
            for _aid, _a in list(_agents.items()):
                if _aid == task_id:
                    _a.is_interrupted = True
                    _a._completed_by_user = True
        for _tid, _bg_a in list(_background_agents.items()):
            if _tid == task_id:
                _bg_a.is_interrupted = True
                _bg_a._completed_by_user = True
    return task_interrupted


@router.post("/api/tasks/{task_id}/kill")
async def kill_task_process(task_id: int):
    """Kill ALL background shell processes tracked for a task.

    手动杀进程入口（进程→任务方向的中断同步）：一任务可能登记多个后台
    进程（重试/多开），全部 kill_tree 终止后再清理跟踪表——顺序不能反；
    任务处于 backgrounded/running 时同步置 interrupted（reason=user），
    其他状态的任务保持原状态不变。
    """
    task_procs = get_background_processes_for_task(task_id)
    output_text = ""
    for _pk, pinfo in task_procs.items():
        if pinfo.get("output_file"):
            try:
                with open(pinfo["output_file"], "r", encoding="utf-8", errors="replace") as f:
                    output_text += f.read() + "\n"
            except Exception:
                pass
    # Raw shell output files may contain credentials — mask before the text
    # is sliced into task context / task status
    if output_text:
        try:
            from core.secrets import mask_secrets
            output_text = mask_secrets(output_text)
        except Exception:
            pass
    # 有跟踪记录才杀进程；helper 内部先杀整组进程树再 pop 跟踪表，
    # 杀失败不阻断后续状态同步。上下文通知由下方 output 注入承担，不重复注入。
    killed_pids = kill_tracked_background_process(task_id, notify=False) if task_procs else []
    task_interrupted = _interrupt_task_after_process_kill(task_id, output_text)
    if killed_pids:
        message = f"Process killed ({len(killed_pids)})" if len(killed_pids) > 1 else "Process killed"
    elif task_interrupted:
        message = "Task interrupted (no tracked process)"
    else:
        message = "No tracked process"
    return {"status": "success", "message": message,
            "killed_pid": killed_pids[0] if killed_pids else None,
            "killed_pids": killed_pids, "task_interrupted": task_interrupted}


@router.post("/api/tasks/{task_id}/reset-resume-count")
async def reset_task_resume_count(task_id: int):
    """Reset a task's resume_count to 0 so guardian loop can retry."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE tasks SET resume_count=0, updated_at=CURRENT_TIMESTAMP WHERE id=?", (task_id,))
    conn.commit()
    conn.close()
    from api.config import log_agent_error
    log_agent_error(f"Task #{task_id}: resume_count manually reset to 0")
    return {"status": "success"}


@router.post("/api/tasks/{task_id}/reply")
async def reply_to_background_task(task_id: int, body: dict):
    """Reply to a background task that called ask_user_question.
    Puts the answer into the agent's user_input_queue to unblock it.
    If no live agent holds the task (ask_user wait timed out and the task
    was background-paused), injects the answer into the task context and
    resumes it instead of returning 404.
    """
    answer = body.get("answer", "")
    if not answer:
        raise HTTPException(status_code=400, detail="answer is required")
    from api.state import _background_agents
    agent = _background_agents.get(task_id)
    if agent and not getattr(agent, "is_interrupted", False):
        try:
            agent.user_input_queue.put_nowait(answer)
            return {"status": "success", "message": f"Answer delivered to task #{task_id}"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to deliver answer: {e}")
    # No live agent — inject the answer into context and resume the task
    from api.background import resume_task_with_late_answer
    result = resume_task_with_late_answer(task_id, answer)
    if result.get("ok"):
        return {"status": "success", "message": result["message"], "resumed": True}
    if result.get("error") == "not_found":
        raise HTTPException(status_code=404, detail=result["message"])
    # Terminal/conflict states — explicit status instead of a bare 404
    return {"status": result.get("error"), "task_status": result.get("status"),
            "message": result["message"]}
