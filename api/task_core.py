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
from api.state import _pending_sandbox_approvals, _active_agents, _guardian_resume_lock

_CONTINUATION_PREFIXES = [
    "继续", "继续搞", "继续做", "继续下载", "接着", "retry", "continue",
    "再来", "再试", "重新", "唤醒", "恢复", "resume", "next", "yes",
]


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
    # Generate task goal asynchronously
    threading.Thread(
        target=_generate_task_goal_background,
        args=(task_id, user_query, session_id),
        daemon=True
    ).start()
    return task_id


def _generate_task_goal_background(task_id: int, query: str, session_id: int):
    """Use LLM to generate a task goal summary asynchronously."""
    try:
        from core.llm_client import LLMClient
        cfg = load_config()
        llm = LLMClient(default_model=cfg.get("default_model", "moonshot/kimi-latest"))
        resp, _ = llm.chat([{"role": "user", "content": (
            f"根据用户的问题，用一句话概括任务目标（不超过 50 字）：\n\n{query[:500]}"
        )}])
        goal = (resp.choices[0].message.content or "").strip()
        if goal and len(goal) > 10:
            conn = db_connect()
            conn.execute("UPDATE tasks SET task_goal=? WHERE id=?", (goal[:500], task_id))
            conn.commit()
            conn.close()
    except Exception:
        pass


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

    Flips status to 'running' only when the task's current status is in
    ``allowed_statuses``. SQLite serializes writers, so under concurrent
    resume paths exactly one caller gets True. Every resume path must call
    this BEFORE spawning its worker thread; losers must skip the resume.
    """
    try:
        conn = db_connect()
        placeholders = ",".join("?" for _ in allowed_statuses)
        cursor = conn.execute(
            f"UPDATE tasks SET status='running', updated_at=CURRENT_TIMESTAMP "
            f"WHERE id=? AND status IN ({placeholders})",
            (task_id, *allowed_statuses))
        claimed = cursor.rowcount == 1
        conn.commit()
        conn.close()
        return claimed
    except Exception as e:
        print(f"[Task] Claim resume error: {e}")
        return False


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
                    print(f"[Task] Reusing running task {tid} for session {session_id}")
                    return tid
            elif status in ('completed', 'interrupted', 'backgrounded', 'background_failed'):
                try:
                    created_dt = datetime.strptime(created, '%Y-%m-%d %H:%M:%S')
                    created_dt = created_dt.replace(tzinfo=timezone.utc)
                    now = datetime.now(timezone.utc)
                    is_recent = (now - created_dt) < timedelta(minutes=30)
                except Exception:
                    is_recent = False

                if is_recent and len(query.strip()) > 10:
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
                    return result
            except Exception:
                pass

        # Fallback: reconstruct from task_steps + messages
        user_query = row["user_query"] or ""
        steps = conn.execute(
            "SELECT tool_name, tool_call_id, args_preview, full_args, result_preview, "
            "full_result, success, created_at FROM task_steps WHERE task_id=? ORDER BY created_at",
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
        return entries
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


def increment_task_resume(task_id: int):
    """Increment the resume_count for a task."""
    try:
        conn = db_connect()
        conn.execute("UPDATE tasks SET resume_count = resume_count + 1 WHERE id=?", (task_id,))
        conn.commit()
        conn.close()
    except Exception:
        pass


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

def _check_goal_completeness(task_id: int) -> int:
    """Check if the goal containing this task_id has all tasks completed.
    If all done, use LLM to judge if the goal is fulfilled.
    Returns: 0=incomplete, 1=confirmed complete and archived, -1=unknown.
    """
    try:
        from tools.task_plan import load_goals, save_goals
        goals = load_goals()
        goal = None
        for g in goals.get("items", []):
            if task_id in g.get("task_ids", []):
                goal = g
                break
        if not goal:
            return 0

        task_ids = goal.get("task_ids", [])
        if not task_ids:
            return 0

        conn = db_connect()
        incomplete = conn.execute(
            f"SELECT COUNT(*) FROM tasks WHERE id IN ({','.join('?' for _ in task_ids)}) "
            f"AND status NOT IN ('completed', 'failed')",
            task_ids
        ).fetchone()[0]
        conn.close()

        if incomplete > 0:
            return 0

        from core.llm_client import LLMClient
        cfg = load_config()
        llm = LLMClient(default_model=cfg.get("default_model", "moonshot/kimi-latest"))

        result_summaries = []
        conn2 = db_connect()
        conn2.row_factory = sqlite3.Row
        for _tid in task_ids:
            _row = conn2.execute(
                "SELECT user_query, result_summary FROM tasks WHERE id=?", (_tid,)
            ).fetchone()
            if _row:
                summary = _row["result_summary"] or _row["user_query"] or ""
                result_summaries.append(f"任务 #{_tid}: {summary}")
        conn2.close()

        prompt = (
            f"大目标：{goal.get('desc', '')}\n\n"
            f"已完成子任务：\n" + "\n".join(result_summaries) + "\n\n"
            f"问题：此大目标是否已完成？如果是，仅回复 YES；如果否、仅完成部分或不确定，仅回复 NO。"
        )
        resp, _ = llm.chat([{"role": "user", "content": prompt}])
        text = (resp.choices[0].message.content or "").strip().upper()

        if text.startswith("YES"):
            goal["status"] = "done"
            from datetime import datetime as _dt
            goal["updated"] = _dt.now().strftime("%Y-%m-%d %H:%M")
            save_goals(goals)
            print(f"[Goal] Goal #{goal['id']} completed and archived")
            return 1
        else:
            print(f"[Goal] Goal #{goal['id']} not yet complete (LLM judged NO)")
            return -1
    except Exception as e:
        print(f"[Goal] Completeness check error: {e}")
        return -1
