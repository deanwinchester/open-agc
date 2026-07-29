"""Core task CRUD and helper functions for Open-AGC."""
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional

from api.db import DB_PATH, db_connect
from api.config import load_config
from api.state import (
    _pending_sandbox_approvals, _active_agents, _background_agents,
    _guardian_resume_lock,
)

_CONTINUATION_PREFIXES = [
    "继续", "继续搞", "继续做", "继续下载", "接着", "retry", "continue",
    "再来", "再试", "重新", "唤醒", "恢复", "resume", "next", "yes",
]

# _resolve_task_for_query 返回哨兵：消息已排入存活的后台 agent，
# 调用方不得再为该消息开启新的 agent 循环（同一任务双 agent 会写乱步骤流）
QUEUED_TO_LIVE_AGENT = -1


# Staleness threshold for 'running' tasks. Must exceed the worst-case
# no-step window of a HEALTHY task: one LLM call can take timeout=600s
# (llamacpp non-stream, core/llm_client.py:621) x 3 retries (:647) ~= 30min
# without any add_task_step heartbeat. 35min covers that window + margin, so
# only genuinely dead worker threads are flagged (avoids double-agent resume).
_STALE_RUNNING_MINUTES = 35


def _is_task_stale(updated_at_str, minutes: int = _STALE_RUNNING_MINUTES,
                   now: datetime = None) -> bool:
    """True when a 'running' task's updated_at is older than `minutes`.

    add_task_step() heartbeats tasks.updated_at on every recorded step, so a
    live agent keeps it fresh. A 'running' task with no update for longer than
    `minutes` has no live worker (its thread died without resetting the
    status). Missing or unparseable timestamps are treated as stale.
    """
    if not updated_at_str:
        return True
    try:
        s = str(updated_at_str).strip().replace('T', ' ')[:19]
        updated = datetime.strptime(s, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        ref = now or datetime.now(timezone.utc)
        return (ref - updated).total_seconds() > minutes * 60
    except Exception:
        return True


def _get_step_offset(task_id: int) -> int:
    """Step-number offset for (re)joining an existing task.

    The agent numbers steps 1-based per run, so new steps must continue at
    MAX(step_number)+1 — i.e. the offset to add is COALESCE(MAX(step_number), 0).
    Shared by ws.py and background.py so every resume path numbers identically.
    """
    try:
        conn = db_connect()
        max_step = conn.execute(
            "SELECT COALESCE(MAX(step_number), 0) FROM task_steps WHERE task_id=?",
            (task_id,)).fetchone()[0]
        conn.close()
        return max_step or 0
    except Exception:
        return 0


def _extract_task_title(response: str) -> str:
    """Extract a clean task title from the agent's first response line."""
    if not response:
        return ""
    first_line = response.strip().split('\n')[0]
    # Remove common markdown formatting
    title = re.sub(r'^#+\s*', '', first_line)
    title = re.sub(r'\*\*', '', title)
    title = title.strip()
    # Truncate to reasonable length
    if len(title) > 120:
        title = title[:117] + '...'
    return title


def create_task(title: str, user_query: str, task_type: str = 'oneshot',
                schedule_cron: str = None, schedule_enabled: bool = False,
                session_id: int = 1) -> int:
    """Insert a new task row and return its ID."""
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tasks (title, user_query, task_type, schedule_cron, schedule_enabled, session_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (title[:200], user_query, task_type, schedule_cron,
         1 if schedule_enabled else 0, session_id)
    )
    task_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return task_id


def _record_task_deliverables(task_id: int):
    """Extract deliverables from task_steps and update task's result_summary and output_files."""
    try:
        conn = db_connect()
        conn.row_factory = sqlite3.Row
        steps = conn.execute(
            "SELECT tool_name, args_preview, full_result, generated_files, result_preview "
            "FROM task_steps WHERE task_id=? ORDER BY created_at",
            (task_id,)
        ).fetchall()
        if not steps:
            conn.close()
            return

        generated_files = []
        summaries = []

        for s in steps:
            # Collect generated files
            gf = s["generated_files"]
            if gf:
                try:
                    parsed = json.loads(gf) if isinstance(gf, str) else gf
                    if isinstance(parsed, list):
                        generated_files.extend(parsed)
                except Exception:
                    pass

            # Collect result summaries (only from write_file/edit_file)
            if s["tool_name"] in ("write_file", "edit_file"):
                preview = s["args_preview"] or s["result_preview"] or ""
                if preview:
                    summaries.append(preview[:200])

            # Collect the final summary from the last self_review or key result
            if s["tool_name"] == "self_review" and s["result_preview"]:
                summaries.append(s["result_preview"][:500])

        # Deduplicate files
        seen_paths = set()
        unique_files = []
        for f in generated_files:
            path = f.get("path", "") if isinstance(f, dict) else str(f)
            if path and path not in seen_paths:
                seen_paths.add(path)
                unique_files.append(f)

        # Update task
        summary = "\n".join(summaries[-5:])[:2000] if summaries else ""
        output_files_json = json.dumps(unique_files, ensure_ascii=False) if unique_files else "[]"

        conn.execute(
            "UPDATE tasks SET result_summary=?, output_files=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (summary, output_files_json, task_id)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[Task] Record deliverables error: {e}")


def update_task_status(task_id: int, status: str,
                       result_summary: str = None,
                       interruption_reason: str = None):
    """Update task status and optional result_summary/interruption_reason."""
    try:
        conn = db_connect()
        fields = ["status=?", "updated_at=CURRENT_TIMESTAMP"]
        params = [status]
        if result_summary is not None:
            fields.append("result_summary=?")
            params.append(result_summary)
        if interruption_reason is not None:
            fields.append("interruption_reason=?")
            params.append(interruption_reason)
        elif status in ('running', 'completed'):
            # 翻转到 running/completed 时清掉历史中断原因——那是上一次中断的
            # 记录，任务已恢复执行/已收官，继续保留会误导前端展示。
            fields.append("interruption_reason=NULL")
        # Clear wake_at when interrupting or completing
        if status in ('interrupted', 'completed', 'background_failed'):
            fields.append("wake_at=NULL")
        params.append(task_id)
        conn.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id=?", params)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[Task] Update status error: {e}")


def update_task_type(task_id: int, task_type: str):
    """Change the task_type column."""
    try:
        conn = db_connect()
        conn.execute("UPDATE tasks SET task_type=? WHERE id=?", (task_type, task_id))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[Task] Update type error: {e}")


def claim_task_for_resume(task_id: int, allowed_statuses: tuple) -> bool:
    """Atomically claim a task for resume (compare-and-set).

    Flips status to 'running' and increments ``resume_count`` only when the
    task's current status is in ``allowed_statuses``. 认领同时把
    ``interruption_reason`` 清 NULL——历史中断原因属于上一次中断，任务已
    恢复执行，继续保留会误导前端「中断原因」区块。SQLite serializes
    writers, so under concurrent resume paths exactly one caller gets True.
    Every resume path must call this BEFORE spawning its worker thread;
    losers must skip the resume. 统一语义「认领即 running，不再降级」：
    认领成功后任何路径都不得再把状态写回 interrupted；resume_count 收敛到
    本函数唯一计数，wake/shell/下载/Guardian/Scheduler 各路径自然计数。
    """
    try:
        conn = db_connect()
        placeholders = ",".join("?" for _ in allowed_statuses)
        cursor = conn.execute(
            f"UPDATE tasks SET status='running', resume_count=resume_count+1, "
            f"interruption_reason=NULL, updated_at=CURRENT_TIMESTAMP "
            f"WHERE id=? AND status IN ({placeholders})",
            (task_id, *allowed_statuses))
        claimed = cursor.rowcount == 1
        conn.commit()
        conn.close()
        return claimed
    except Exception as e:
        print(f"[Task] Claim resume error: {e}")
        return False


def kill_tracked_background_process(task_id, notify: bool = True) -> list:
    """Kill ALL tracked background shell processes of a task being interrupted.

    任务→进程方向的中断同步：一任务可能登记多个后台进程（重试/多开），
    全部都要随任务终止。内部先 kill_tree 终止进程树、再清跟踪表（顺序
    不能反，失败也会清表）；杀进程失败不阻断中断流程本身。notify 时在
    任务上下文注入系统通知——成功列出全部被终止的 pid，失败如实说明
    "终止失败、进程可能仍在运行"。返回被终止的 pid 列表；无跟踪进程
    或杀进程异常时返回空列表。
    """
    from tools.shell import kill_background_process_for_task
    try:
        pids = kill_background_process_for_task(task_id)
    except Exception as e:
        print(f"[Task] kill_tree failed for task {task_id}: {e}")
        if notify:
            try:
                ctx = get_task_context(task_id)
                if ctx is not None:
                    ctx.append({"role": "user", "content": (
                        "【系统通知】任务已中断，但关联的后台进程终止失败，"
                        "进程可能仍在运行，请检查并手动处理残留进程。"
                    )})
                    save_task_context(task_id, ctx)
            except Exception:
                pass
        return []
    if pids and notify:
        try:
            ctx = get_task_context(task_id)
            if ctx is not None:
                pid_list = ", ".join(str(p) for p in pids)
                ctx.append({"role": "user", "content": (
                    f"【系统通知】关联的后台进程（PID {pid_list}）已随任务中断一并终止。"
                )})
                save_task_context(task_id, ctx)
        except Exception:
            pass
    return pids


def _resolve_task_goal_via_llm(session_id: int, query: str) -> str:
    """When user confirms a proposal (agent last msg ends with ?), ask LLM to extract the goal."""
    try:
        conn = db_connect()
        conn.row_factory = sqlite3.Row
        last_agent = conn.execute(
            "SELECT content FROM messages WHERE session_id=? AND role='agent' ORDER BY id DESC LIMIT 1",
            (session_id,)
        ).fetchone()
        conn.close()
        if not last_agent:
            return ""
        content = last_agent["content"] or ""
        if not content.strip().endswith(("？", "?", "？ ", "? ")):
            return ""

        from core.llm_client import LLMClient
        cfg = load_config()
        llm = LLMClient(default_model=cfg.get("default_model", "moonshot/kimi-latest"))
        conn2 = db_connect()
        conn2.row_factory = sqlite3.Row
        recent_msgs = conn2.execute(
            "SELECT role, content FROM (SELECT * FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 10) ORDER BY id ASC",
            (session_id,)
        ).fetchall()
        conn2.close()
        context = "\n".join(
            f"{'用户' if m['role'] == 'user' else '助手'}: {m['content'][:200]}"
            for m in recent_msgs
        )
        prompt = (
            f"以下是最近对话：\n{context}\n\n"
            f"判断用户最新的回复是否接受了助手的提议。如果接受，用一句话概括达成一致的任务目标（20字以内），"
            f"只返回目标文本；否则返回空字符串。"
        )
        resp, _ = llm.chat([{"role": "user", "content": prompt}])
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        print(f"[Task] Goal extraction error: {e}")
        return ""


def _resolve_task_for_query(session_id: int, query: str) -> int:
    """Determine the task_id for an incoming query BEFORE agent execution."""
    try:
        conn = db_connect()
        existing = conn.execute(
            "SELECT id, status, created_at, updated_at FROM tasks WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (session_id,)
        ).fetchone()
        conn.close()

        if existing:
            tid, status, created, updated_at = existing
            if status == 'running':
                if _is_task_stale(updated_at):
                    # Running but silent for too long — the worker thread is
                    # dead. Reset it (same rule as the guardian) and fall
                    # through to create a fresh task for this query.
                    print(f"[Task] Latest task {tid} running but stale "
                          f"(>{_STALE_RUNNING_MINUTES}min no update) — not reusing")
                    update_task_status(tid, "interrupted",
                        "执行线程失联（无步骤更新超时），已自动标记",
                        interruption_reason="stale_running")
                else:
                    # 复用前先查活：存活的后台 agent 已持有该任务，直接把消息
                    # 排给它，而不是再开一个 agent 复用同一任务（双 agent 写
                    # 同一任务会写乱步骤流）。调用方见哨兵后不再开跑。
                    _bg = _background_agents.get(tid)
                    if _bg is not None and not getattr(_bg, 'is_interrupted', False):
                        try:
                            _bg.queue_message(query)
                            print(f"[Task] Task {tid} owned by a live background agent — "
                                  f"queued message instead of reusing (session {session_id})")
                            return QUEUED_TO_LIVE_AGENT
                        except Exception as _q_err:
                            print(f"[Task] Queue to live background agent failed: {_q_err}")
                    print(f"[Task] Reusing running task {tid} for session {session_id}")
                    return tid
            elif status in ('completed', 'interrupted', 'backgrounded', 'background_failed'):
                # 窗口看 updated_at 而非 created_at：add_task_step 心跳持续刷新
                # updated_at，长寿命任务的 created_at 早已掉出窗口（误判新话题）
                try:
                    updated_dt = datetime.strptime(updated_at, '%Y-%m-%d %H:%M:%S')
                    updated_dt = updated_dt.replace(tzinfo=timezone.utc)
                    now = datetime.now(timezone.utc)
                    is_recent = (now - updated_dt) < timedelta(minutes=30)
                except Exception:
                    is_recent = False

                _q = query.strip().lower()
                # 显式续接词（继续/接着/resume…）无视长度一律续接
                _is_continuation = any(
                    kw and (_q.startswith(kw) or _q == kw)
                    for kw in _CONTINUATION_PREFIXES)
                # >10 字盲续仅限未完结状态（interrupted/backgrounded）——
                # completed/background_failed 已收官，长消息按新话题开新任务
                _blind_continue = (len(query.strip()) > 10
                                   and status in ('interrupted', 'backgrounded'))

                if is_recent and (_is_continuation or _blind_continue):
                    print(f"[Task] Continuing task {tid} for session {session_id}")
                    update_task_status(tid, "running")
                    return tid
    except Exception as e:
        print(f"[Task] Error resolving task: {e}")

    task_title = query if len(query.strip()) > 15 else _resolve_task_goal_via_llm(session_id, query)
    if not task_title:
        task_title = _extract_task_title(query) or query[:120]
    if len(task_title) > 120:
        task_title = task_title[:117] + '...'
    tid = create_task(task_title, query, session_id=session_id)
    print(f"[Task] Created task {tid} for session {session_id}")

    try:
        from tools.shell import adopt_orphan_processes
        adopted = adopt_orphan_processes(tid, session_id=session_id)
        if adopted:
            print(f"[Task] Adopted {adopted} orphan process(es) for task {tid}")
    except Exception as e:
        print(f"[Task] Orphan adoption error: {e}")

    return tid


def _load_session_context(session_id: int, limit: int = 50) -> list:
    """Load the last N messages for a session as conversation context."""
    try:
        conn = db_connect()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT role, content FROM ("
            "SELECT role, content FROM messages "
            "WHERE session_id=? AND role IN ('user','agent') "
            "ORDER BY id DESC LIMIT ?"
            ") ORDER BY id ASC",
            (session_id, limit)
        ).fetchall()
        conn.close()
        result = []
        for r in rows:
            role = "assistant" if r["role"] == "agent" else r["role"]
            result.append({"role": role, "content": r["content"]})
        return result
    except Exception as e:
        print(f"[Context] Failed to load session context: {e}")
        return []


def handle_task_completion(task_id: int, response: str, agent_messages: list,
                           session_id: int = 1, update_title: bool = True,
                           wake_minutes: int = None) -> str:
    """Unified state transition handler after agent.run_turn() completes.

    Called by ws.py, background.py _run_background_task, and guardian.
    Parses the response for special prefixes and updates task status accordingly.

    Returns: 'completed', 'interrupted', 'backgrounded', 'interrupted_user'
    """
    if not response:
        update_task_status(task_id, "failed", "No response from agent", interruption_reason="error")
        return 'failed'

    is_max_iter = response.startswith("[MAX_ITERATIONS_REACHED]")
    is_backgrounded = response.startswith("[TASK_BACKGROUNDED]")
    is_user_int = "interrupted by user" in response.lower()
    summary = response[:200]

    # Extract and save title from first response line
    if update_title and response and not is_max_iter and not is_backgrounded:
        title = _extract_task_title(response)
        if title:
            try:
                conn = db_connect()
                conn.execute("UPDATE tasks SET title=? WHERE id=?", (title, task_id))
                conn.commit()
                conn.close()
            except Exception:
                pass

    # -- Backgrounded --
    if is_backgrounded:
        save_task_context(task_id, agent_messages)
        # Parse WAKE_IN=N
        _wake_match = re.search(r'WAKE_IN=(\d+)', response)
        _wake_min = int(_wake_match.group(1)) if _wake_match else wake_minutes
        if _wake_min:
            _wake_dt = (datetime.utcnow() + timedelta(minutes=_wake_min)).strftime('%Y-%m-%d %H:%M:%S')
            try:
                conn = db_connect()
                conn.execute("UPDATE tasks SET wake_at=? WHERE id=?", (_wake_dt, task_id))
                conn.commit()
                conn.close()
                print(f"[TaskCore] Set wake_at={_wake_dt} for task {task_id}")
            except Exception as _wke:
                print(f"[TaskCore] Failed to set wake_at: {_wke}")
        _body = response[len("[TASK_BACKGROUNDED] "):].strip() or "任务进入后台"
        update_task_status(task_id, "backgrounded", _body, interruption_reason="backgrounded")
        return 'backgrounded'

    # -- User interrupted --
    if is_user_int:
        update_task_status(task_id, "interrupted", summary, interruption_reason="user")
        return 'interrupted_user'

    # -- Max iterations --
    if is_max_iter:
        save_task_context(task_id, agent_messages)
        update_task_status(task_id, "interrupted", summary, interruption_reason="max_iterations")
        # Promote oneshot to longrun
        try:
            conn = db_connect()
            row = conn.execute("SELECT task_type FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row and row[0] == 'oneshot':
                conn.execute("UPDATE tasks SET task_type='longrun' WHERE id=?", (task_id,))
            conn.commit()
            conn.close()
        except Exception:
            pass
        return 'interrupted'

    # -- Normal completion --
    save_task_context(task_id, agent_messages)
    _record_task_deliverables(task_id)
    update_task_status(task_id, "completed", summary)
    # 成功完成即清零恢复计数：Scheduler 点火/各恢复路径的 CAS 每次 +1，
    # 不清零会单调累积——长寿命 cron 任务一次普通中断就会被 Guardian
    # 判超限置 background_failed（且高计数触发长退避）
    try:
        conn = db_connect()
        conn.execute("UPDATE tasks SET resume_count=0 WHERE id=?", (task_id,))
        conn.commit()
        conn.close()
    except Exception:
        pass
    _check_goal_completeness(task_id)
    return 'completed'


def save_message(role: str, content: str, session_id: int = 1, task_id: int = None):
    """Save a chat message. If task_id is provided, links the message to its task."""
    try:
        conn = db_connect()
        if task_id:
            conn.execute(
                "INSERT INTO messages (role, content, session_id, task_id) VALUES (?, ?, ?, ?)",
                (role, content, session_id, task_id)
            )
        else:
            conn.execute(
                "INSERT INTO messages (role, content, session_id) VALUES (?, ?, ?)",
                (role, content, session_id)
            )
        conn.execute("UPDATE sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (session_id,))
        conn.commit()
        conn.close()
    except Exception as _e:
        print(f"[TaskCore] save_message error: {_e}")


def save_task_context(task_id: int, messages: list):
    """Save agent conversation messages as a JSON snapshot for resume.

    Safety: does NOT overwrite if the new snapshot has fewer than 10 messages
    AND is less than half the size of the existing one.
    """
    try:
        conn = db_connect()
        cursor = conn.cursor()
        existing = cursor.execute(
            "SELECT context_snapshot FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if existing and existing[0]:
            try:
                old = json.loads(existing[0])
                old_len = len(old)
                new_len = len(messages)
                if 0 < new_len < 10 and new_len < old_len / 2:
                    print(f"[Task] Safety: NOT overwriting context snapshot for task {task_id} "
                          f"(new={new_len} < old={old_len} and new < old/2)")
                    conn.close()
                    return
            except Exception:
                pass
        snapshot = json.dumps(messages, ensure_ascii=False)
        cursor.execute(
            "UPDATE tasks SET context_snapshot=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (snapshot, task_id)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[Task] Save context error: {e}")


# ── 大任务检查点（断点续跑）──
# 约定：agent 执行大批量/长耗时任务（大规模导出、批量处理等）时，在沙箱工作
# 目录下维护 .checkpoints/task_<task_id>.json（字段 task/total/done/
# last_cursor/phase/files_dir/updated_at），每处理完一批就更新；任务恢复时
# 由本模块读取并注入上下文，agent 从 last_cursor 继续而非从头重跑。
_CHECKPOINT_NOTICE_PREFIX = "【系统提示】大任务检查点"


def get_checkpoint_dir() -> str:
    """沙箱工作目录下的检查点目录（sandbox_dir 解析口径与 routes/server 一致）。"""
    try:
        cfg = load_config() or {}
    except Exception:
        cfg = {}
    sandbox = cfg.get("sandbox_dir") or os.path.abspath(os.path.join(os.getcwd(), "workspace"))
    return os.path.join(sandbox, ".checkpoints")


def read_task_checkpoint(task_id: int) -> Optional[dict]:
    """读取 .checkpoints/task_<task_id>.json；缺失/损坏/非 JSON 对象时返回 None，不抛异常。"""
    try:
        path = os.path.join(get_checkpoint_dir(), f"task_{task_id}.json")
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def format_checkpoint_notice(task_id: int) -> str:
    """把任务检查点格式化为恢复注入文本；无有效检查点时返回空串。

    三条恢复路径（ws 手动恢复 / _run_background_task / 重启 reconcile）共用：
    主注入点在 get_task_context；无上下文快照时的兜底注入见各恢复查询拼接处。
    """
    data = read_task_checkpoint(task_id)
    if not data:
        return ""
    return (
        f"{_CHECKPOINT_NOTICE_PREFIX}：检测到大任务进度检查点 "
        f".checkpoints/task_{task_id}.json，内容如下：\n"
        f"{json.dumps(data, ensure_ascii=False)}\n"
        f"上次进度 done={data.get('done', '?')}/total={data.get('total', '?')}，"
        f"last_cursor={data.get('last_cursor')}。"
        "请从 last_cursor 游标处继续处理，严禁清理现场从头重跑、"
        "严禁重复处理已完成部分；后续每处理完一批请继续更新该检查点文件。"
    )


def _with_checkpoint_notice(task_id: int, messages: list) -> list:
    """在恢复上下文末尾注入最新检查点提示。

    快照里可能已存历次恢复留下的旧提示（进度/updated_at 已过时），先剔除
    再追加，保证上下文里至多一条且永远是最新读盘结果。
    """
    notice = format_checkpoint_notice(task_id)
    if not notice:
        return messages
    kept = [
        m for m in messages
        if not (m.get("role") == "user"
                and isinstance(m.get("content"), str)
                and m["content"].startswith(_CHECKPOINT_NOTICE_PREFIX))
    ]
    kept.append({"role": "user", "content": notice})
    return kept


def get_task_context(task_id: int) -> Optional[list]:
    """Load saved context with fallback reconstruction from task_steps + messages."""
    try:
        conn = db_connect()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT context_snapshot, user_query, status FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if not row:
            conn.close()
            return None

        snapshot = row["context_snapshot"]
        if snapshot:
            try:
                result = json.loads(snapshot)
                if isinstance(result, list) and len(result) > 1:
                    conn.close()
                    return _with_checkpoint_notice(task_id, result)
            except Exception:
                pass

        # Fallback: reconstruct from task_steps + messages
        user_query = row["user_query"] or ""
        steps = conn.execute(
            "SELECT tool_name, tool_call_id, args_preview, full_args, result_preview, "
            "full_result, success, created_at FROM task_steps WHERE task_id=? ORDER BY id",
            (task_id,)
        ).fetchall()
        conn.close()

        entries = [{"role": "user", "content": user_query}]
        for s in steps:
            tool_call_id = s["tool_call_id"] or f"call_{s['created_at']}"
            entries.append({
                "role": "assistant",
                "tool_calls": [{
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": s["tool_name"],
                        "arguments": s["full_args"] or s["args_preview"] or "{}"
                    }
                }]
            })
            entries.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": s["tool_name"],
                "content": s["full_result"] or s["result_preview"] or ""
            })
        return _with_checkpoint_notice(task_id, entries)
    except Exception as e:
        print(f"[Task] Get context error: {e}")
        return None


def _get_task_step_count(task_id: int) -> int:
    """Count the number of steps for a task."""
    try:
        conn = db_connect()
        count = conn.execute(
            "SELECT COUNT(*) FROM task_steps WHERE task_id=?", (task_id,)
        ).fetchone()[0]
        conn.close()
        return count
    except Exception:
        return 0



def add_task_step(task_id: int, step_number: int, tool_name: str, tool_label: str = None,
                  args_preview: str = None, result_preview: str = None, full_result: str = None,
                  success: bool = True, thinking_content: str = None, session_id: int = None,
                  tool_call_id: str = None, full_args: str = None,
                  generated_files: str = None, sub_task: str = None):
    """Insert a task step record."""
    try:
        conn = db_connect()
        conn.execute(
            "INSERT INTO task_steps (task_id, step_number, tool_name, tool_label, args_preview, "
            "result_preview, full_result, success, thinking_content, session_id, tool_call_id, "
            "full_args, generated_files, sub_task) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, step_number, tool_name, tool_label, args_preview, result_preview,
             full_result, 1 if success else 0, thinking_content, session_id, tool_call_id,
             full_args, generated_files or "", sub_task or "")
        )
        # Liveness heartbeat: a live agent records steps, so keep the parent
        # task's updated_at fresh. _is_task_stale() relies on this to tell a
        # healthy long-running task apart from one whose worker thread died.
        conn.execute("UPDATE tasks SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (task_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[Task] Add step error: {e}")


def _resolve_goal_for_query(query: str, recent_context: str = "") -> int:
    """Determine which goal (if any) this query is continuing.
    recent_context: last 2-3 conversation turns to distinguish follow-up from goal.
    Returns goal_id, or 0 for new task.
    """
    q = query.strip().lower()
    for kw in _CONTINUATION_PREFIXES:
        if kw and (q.startswith(kw) or q == kw):
            try:
                from tools.task_plan import load_goals
                items = load_goals().get("items", [])
                for st in ("doing", "pending"):
                    for item in items:
                        if item.get("status") == st:
                            return item["id"]
            except Exception:
                pass
            return 0

    try:
        from tools.task_plan import load_goals
        goals = load_goals()
        active = sorted(
            [i for i in goals.get("items", []) if i.get("status") in ("doing", "pending")],
            key=lambda x: (0 if x.get("status") == "doing" else 1, -x.get("id", 0))
        )
    except Exception:
        active = []

    if not active:
        return 0

    try:
        from core.llm_client import LLMClient
        model = load_config().get("default_model", "moonshot/kimi-latest")

        goal_lines = "\n".join(f"{i['id']}. {i['desc']} ({i['status']})" for i in active)
        _nl = "\n"
        prompt = (
            f"当前大目标：{_nl}{goal_lines}{_nl}{_nl}"
            + (f"最近对话：{_nl}{recent_context[:500]}{_nl}{_nl}" if recent_context else "")
            + f"用户新输入：\u300c{query[:200]}\u300d{_nl}{_nl}"
            + f"分析：用户输入是对最近对话的延续，还是对大目标的续接？{_nl}"
            + f"如果是最近对话的延续 \u2192 回复 0（全新任务）{_nl}"
            + f"如果是明确续接某个大目标 \u2192 仅回复该目标数字 id{_nl}"
            + f"不确定 \u2192 回复 0"
        )
        llm = LLMClient(default_model=model)
        resp, _ = llm.chat([{"role": "user", "content": prompt}])
        text = resp.choices[0].message.content.strip()
        nums = re.findall(r'\d+', text)
        if nums:
            goal_id = int(nums[0])
            if any(i["id"] == goal_id for i in active):
                return goal_id
    except Exception:
        pass

    return 0


_GOAL_MAX_REMEDIATION = 3


def _spawn_goal_task_run(task_id: int, query: str) -> None:
    """Fire-and-forget background run for a goal-created task (patrol/remediation).

    Deferred import: api.background imports api.task_core at module level."""
    try:
        from api.background import _run_background_task
        threading.Thread(target=_run_background_task,
                         args=(task_id, query, None, False), daemon=True).start()
    except Exception as e:
        print(f"[Goal] Failed to spawn background run for task {task_id}: {e}")


def _link_task_to_goal(goal_id: int, task_id: int) -> bool:
    """Append task_id to goal.task_ids under the goals lock. Returns True if linked."""
    from tools.task_plan import update_goals

    def _link(data):
        for g in data.get("items", []):
            if g.get("id") == goal_id:
                tids = g.setdefault("task_ids", [])
                if not isinstance(tids, list):
                    tids = g["task_ids"] = []
                if task_id not in tids:
                    tids.append(task_id)
                g["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                return True, True
        return False, False

    return bool(update_goals(_link))


def remediate_goal(goal_id: int, reason: str, summaries: str = "",
                   session_id: int = 1, spawn: bool = True) -> str:
    """判 NO / 巡检发现目标未完成时的补救入口（带 resume_count 上限）。

    goal.resume_count < _GOAL_MAX_REMEDIATION：创建补救任务（query 带目标
    desc + 已有任务摘要 + 判 NO 理由），resume_count+1，回链后后台开跑。
    超限：置 stuck 并写 reason。
    返回 'remediated' | 'stuck' | 'missing'。
    """
    from tools.task_plan import update_goals

    def _bump(data):
        for g in data.get("items", []):
            if g.get("id") == goal_id:
                rc = g.get("resume_count", 0) or 0
                if rc >= _GOAL_MAX_REMEDIATION:
                    g["status"] = "stuck"
                    g["reason"] = reason[:200]
                    g["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                    return True, ("stuck", g.get("desc", ""))
                g["resume_count"] = rc + 1
                g["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                return True, ("remediate", g.get("desc", ""))
        return False, ("missing", "")

    action, desc = update_goals(_bump)
    if action == "missing":
        return "missing"
    if action == "stuck":
        print(f"[Goal] Goal #{goal_id} remediation limit "
              f"({_GOAL_MAX_REMEDIATION}) exceeded — marked stuck")
        return "stuck"

    query = (
        f"【系统自动创建】补救大目标 #{goal_id}: {desc}\n\n"
        + (f"已有任务摘要：\n{summaries}\n\n" if summaries else "")
        + f"判定未完成/需继续的理由：{reason}\n\n"
        f"请分析已有进展，继续完成该大目标。"
    )
    new_tid = create_task(f"补救目标: {desc[:80]}", query, session_id=session_id)
    _link_task_to_goal(goal_id, new_tid)
    if spawn:
        _spawn_goal_task_run(new_tid, query)
    print(f"[Goal] Created remediation task #{new_tid} for goal #{goal_id}")
    return "remediated"


def _check_goal_completeness(task_id: int) -> int:
    """Check if the goal containing this task_id has all tasks completed.
    If all done, use LLM to judge if the goal is fulfilled; 判 NO 时走
    remediate_goal 补救（超限置 stuck）。
    Returns: 0=incomplete, 1=confirmed complete and archived, -1=unknown/judged NO.
    """
    try:
        from tools.task_plan import load_goals, update_goals
        goals = load_goals()
        goal = None
        for g in goals.get("items", []):
            if task_id in g.get("task_ids", []):
                goal = g
                break
        if not goal:
            return 0

        goal_id = goal["id"]
        goal_desc = goal.get("desc", "")
        task_ids = list(goal.get("task_ids", []))
        if not task_ids:
            return 0

        conn = db_connect()
        conn.row_factory = sqlite3.Row
        # failed 不算"已完结"：有失败任务的目标不得判完成
        incomplete = conn.execute(
            f"SELECT COUNT(*) FROM tasks WHERE id IN ({','.join('?' for _ in task_ids)}) "
            f"AND status != 'completed'",
            task_ids
        ).fetchone()[0]
        if incomplete > 0:
            conn.close()
            return 0

        result_summaries = []
        session_id = 1
        for _tid in task_ids:
            _row = conn.execute(
                "SELECT user_query, result_summary, session_id FROM tasks WHERE id=?", (_tid,)
            ).fetchone()
            if _row:
                summary = _row["result_summary"] or _row["user_query"] or ""
                result_summaries.append(f"任务 #{_tid}: {summary}")
                if _tid == task_id and _row["session_id"]:
                    session_id = _row["session_id"]
        conn.close()
        summaries_text = "\n".join(result_summaries)

        # LLM 调用前不持有 goals 写锁；判完再重新 load 改单条 save（缩小竞态窗口）
        from core.llm_client import LLMClient
        cfg = load_config()
        llm = LLMClient(default_model=cfg.get("default_model", "moonshot/kimi-latest"))

        prompt = (
            f"大目标：{goal_desc}\n\n"
            f"已完成子任务：\n" + summaries_text + "\n\n"
            f"问题：此大目标是否已完成？如果是，仅回复 YES；如果否、仅完成部分或不确定，仅回复 NO。"
        )
        resp, _ = llm.chat([{"role": "user", "content": prompt}])
        text = (resp.choices[0].message.content or "").strip().upper()

        if text.startswith("YES"):
            def _mark_done(data):
                for g in data.get("items", []):
                    if g.get("id") == goal_id:
                        g["status"] = "done"
                        g["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                        g.pop("reason", None)
                        return True, True
                return False, False

            if update_goals(_mark_done):
                print(f"[Goal] Goal #{goal_id} completed and archived")
                return 1
            return 0

        # 判 NO：创建补救任务（超限置 stuck）
        print(f"[Goal] Goal #{goal_id} not yet complete (LLM judged NO)")
        remediate_goal(goal_id, reason=f"LLM 判定目标未完成（{text[:80]}）",
                       summaries=summaries_text, session_id=session_id)
        return -1
    except Exception as e:
        print(f"[Goal] Completeness check error: {e}")
        return -1
