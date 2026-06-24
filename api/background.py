"""Background task execution, monitoring, guardian and email listener."""
import os, json, re, sqlite3, threading, shutil
from datetime import datetime, timezone, timedelta
from typing import Optional
from api.db import DB_PATH
from api.config import load_config
from api.state import (
    connected_websockets, _main_event_loop, _active_agents, _background_agents,
    _pending_sandbox_approvals, _guardian_resume_lock, _SERVER_START_TIME,
    _broadcast_to_websockets, _ws_send_safe, _apply_pending_sandbox_approvals,
    _broadcast_task_history,
)
from api.task_core import (
    create_task, update_task_status, get_task_context, save_task_context,
    save_message, handle_task_completion,
    add_task_step, _extract_task_title, _record_task_deliverables,
    _load_session_context, _get_task_step_count, _check_goal_completeness,
)
from tools.shell import (
    get_background_processes, cleanup_background_process,
    get_orphan_processes, cleanup_orphan_process,
    adopt_orphan_processes, interrupt_shell,
)

_time = __import__('time')


def start_email_listener():
    def email_listener_loop():
        from core.email_service import fetch_emails, send_email
        while True:
            try:
                config = load_config()
                try:
                    conn = sqlite3.connect(DB_PATH)
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        "SELECT id, email_account, email_password, email_imap_server, "
                        "email_smtp_server, owner_email FROM sessions "
                        "WHERE email_enabled=1 AND email_account!='' AND "
                        "email_password!='' AND email_imap_server!=''"
                    ).fetchall()
                    conn.close()
                except Exception:
                    rows = []

                for row in rows:
                    sess_id = row["id"]
                    smtp_server = row["email_smtp_server"]
                    email_account = row["email_account"]
                    email_password = row["email_password"]
                    owner = row["owner_email"] or ""
                    try:
                        criteria = f'UNSEEN FROM "{owner}"' if owner else 'UNSEEN'
                        emails = fetch_emails(
                            row["email_imap_server"], email_account, email_password,
                            criteria=criteria, limit=5, mark_seen=True)
                        for e in emails:
                            print(f"[Email Listener] Session {sess_id}: email from {owner}: {e['subject']}")

                            # Create a task for this email
                            email_body = e.get("body", "")
                            full_query = f"📧 邮件指令: {e['subject']}\n\n{email_body}"
                            task_id = create_task(f"邮件指令: {e['subject']}", full_query, session_id=sess_id)

                            # Save as user message (collapsible in chat)
                            save_message("user",
                                f"📧 **来自 {owner} 的邮件指令**\n**主题**: {e['subject']}\n```\n{email_body[:2000]}\n```",
                                sess_id)

                            # Build context and run as background task
                            ctx = _load_session_context(sess_id, limit=50)
                            prompt = (f"You received an email instruction from your owner ({owner}).\n"
                                      f"Subject: {e['subject']}\nBody: {email_body}\n\n"
                                      f"Execute this instruction. After completion, the system will reply to the email.")

                            import threading as _em_thr
                            _em_thr.Thread(target=_run_background_task,
                                args=(task_id, prompt, ctx, False), daemon=True).start()

                            # Poll for completion, then reply
                            _max_wait = 600
                            _final_response = ""
                            for _ in range(_max_wait // 2):
                                _time.sleep(2)
                                try:
                                    _trow = sqlite3.connect(DB_PATH).execute(
                                        "SELECT status, result_summary FROM tasks WHERE id=?",
                                        (task_id,)).fetchone()
                                    if _trow and _trow[0] in ("completed", "failed", "interrupted"):
                                        _final_response = _trow[1] or ""
                                        break
                                except Exception:
                                    pass

                            try:
                                ok = send_email(smtp_server, email_account, email_password, owner,
                                    f"Re: {e['subject']} - Task #{task_id}",
                                    f"Task #{task_id} completed.\n\nSummary:\n{_final_response[:3000]}")
                                if ok:
                                    save_message("system", f"📧 邮件指令任务 #{task_id} 完成，已回复 {owner}", sess_id)
                                else:
                                    save_message("system", f"⚠️ 邮件回复发送失败（任务 #{task_id}）", sess_id)
                            except Exception as _me:
                                print(f"[Email Listener] Send error: {_me}")
                    except Exception as e:
                        print(f"[Email Listener] Session {sess_id} error: {e}")
            except Exception as e:
                print(f"Email listener error: {e}")
            _time.sleep(60)

    threading.Thread(target=email_listener_loop, daemon=True).start()

# ==========================================
# Task Scheduler (Background Thread)
# ==========================================

# _broadcast_to_websockets and _ws_send_safe are imported from api.state




def _run_background_task(task_id: int, user_query: str, context_messages: list = None,
                         is_resume: bool = False):
    """Execute a task in background (no WebSocket). Results saved to DB and pushed to clients."""
    from agent.agent import OpenAGCAgent

    config = load_config()
    model = config.get("default_model", "moonshot/kimi-latest")

    # Look up session_id BEFORE creating agent, so agent has correct session_id
    # for sandbox auth (_sandbox_waits key must match frontend's session_id)
    bg_session_id = 1
    try:
        bg_conn = sqlite3.connect(DB_PATH)
        row = bg_conn.execute("SELECT session_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row and row[0]:
            bg_session_id = row[0]
        bg_conn.close()
    except Exception:
        pass

    agent = OpenAGCAgent(model=model, session_id=bg_session_id)
    _apply_pending_sandbox_approvals(agent, bg_session_id)

    step_counter = 0
    step_offset = 0
    if is_resume:
        try:
            off_conn = sqlite3.connect(DB_PATH)
            off_conn.row_factory = sqlite3.Row
            max_step = off_conn.execute(
                "SELECT COALESCE(MAX(step_number), 0) FROM task_steps WHERE task_id=?",
                (task_id,)).fetchone()[0]
            step_offset = max_step
            off_conn.close()
        except Exception:
            pass

    # Detect heartbeat tasks — suppress all progress broadcasts and chat messages
    _is_heartbeat = False
    try:
        _hb_conn = sqlite3.connect(DB_PATH)
        _hb_row = _hb_conn.execute("SELECT task_type FROM tasks WHERE id=?", (task_id,)).fetchone()
        if _hb_row and _hb_row[0] == 'heartbeat':
            _is_heartbeat = True
        _hb_conn.close()
    except Exception:
        pass

    def progress_cb(event: dict):
        nonlocal step_counter
        # Persist sandbox approvals so they survive agent recreation on task resume
        if event.get("event") == "sandbox_approved":
            _path = event.get("path", "")
            _sid = event.get("session_id") or bg_session_id
            if _path:
                _pending_sandbox_approvals.setdefault(_sid, []).append({
                    "action": "approve_dir", "path": _path
                })
            return
        # Suppress all progress broadcasts for heartbeat tasks
        if _is_heartbeat:
            return
        if event.get("event") == "tool_start":
            step_counter += 1
            display_step = event.get("step", step_counter) + step_offset
            event["step"] = display_step
            try:
                add_task_step(
                    task_id=task_id,
                    step_number=display_step,
                    tool_name=event.get("tool", ""),
                    tool_label=event.get("tool_label", ""),
                    args_preview=event.get("args_preview", ""),
                    session_id=bg_session_id,
                    tool_call_id=event.get("tool_call_id"),
                    full_args=event.get("tool_args")
                )
            except Exception:
                pass
        elif event.get("event") == "tool_done":
            done_step = event.get("step", step_counter) + step_offset
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE task_steps SET result_preview=?, full_result=?, success=? WHERE task_id=? AND step_number=?",
                    (event.get("result_preview", ""), event.get("full_result", event.get("result_preview", "")),
                     1 if event.get("success") else 0, task_id, done_step)
                )
                conn.commit()
                conn.close()
            except Exception:
                pass

        # Push progress to connected clients with session_id for proper routing
        event["task_id"] = task_id
        event["session_id"] = bg_session_id
        event["background"] = True
        _broadcast_to_websockets({"type": "progress", **event})
    
    # Inject saved context if resuming
    if context_messages:
        context_messages = [{k:v for k,v in m.items() if k != '_timestamp'} for m in context_messages]
        agent.messages.extend(context_messages)
    elif not is_resume:
        # Load last 50 messages as conversation context for new background/spawned tasks
        _ctx = _load_session_context(bg_session_id, limit=50)
        if _ctx:
            agent.messages.extend(_ctx)
            print(f"[BgTask] Loaded {len(_ctx)} session messages as context for new task #{task_id}")

    query = user_query
    if is_resume and not _is_heartbeat:
        query = (f"【系统指令 - 自动恢复】你之前因为执行步骤过多被系统自动中断了。"
                 f"请根据之前的上下文继续完成未完成的任务。"
                 f"原始任务: {user_query}")

        # Broadcast reconstructed history steps so they appear in the session
        _broadcast_task_history(task_id, bg_session_id, "running")

    update_task_status(task_id, "running")

    # Register so the WS/REST interrupt handler can stop this agent
    _background_agents[task_id] = agent

    # Adopt any orphan shell processes waiting for this task
    try:
        from tools.shell import adopt_orphan_processes
        adopted = adopt_orphan_processes(task_id, session_id=bg_session_id)
        if adopted:
            print(f"[BgTask] Task #{task_id}: adopted {adopted} orphan process(es)")
    except Exception as e:
        print(f"[BgTask] Orphan adoption error: {e}")

    # Notify connected clients (skip for heartbeat tasks)
    if not _is_heartbeat:
        step_count_str = ""
        if is_resume:
            try:
                _sc = _get_task_step_count(task_id)
                if _sc:
                    step_count_str = f"，共 {_sc} 个历史步骤"
            except Exception:
                pass

        resume_msg = (
            f"🔄 **任务自动恢复**\n\n"
            f"任务 **#{task_id}** 已自动恢复执行{step_count_str}，正在继续之前未完成的工作。\n\n"
            f"[📋 查看任务详情](task://{task_id}) · [🔗 任务中心](switch:view/tasks)"
        ) if is_resume else (
            f"⏰ **定时任务执行**\n\n"
            f"任务 **#{task_id}**: {user_query[:80]}"
        )
        _broadcast_to_websockets({
            "type": "message",
            "role": "system",
            "session_id": bg_session_id,
            "content": resume_msg
        })
    
    try:
        msg_count_before = len(agent.messages)
        response = agent.run_turn(query, False, progress_cb, task_id=task_id, skip_rag=bool(context_messages))

        # If user already interrupted this task, don't overwrite the status
        try:
            chk_conn = sqlite3.connect(DB_PATH)
            chk_row = chk_conn.execute("SELECT status, interruption_reason FROM tasks WHERE id=?", (task_id,)).fetchone()
            chk_conn.close()
            if chk_row and chk_row[0] == "interrupted" and chk_row[1] == "user":
                print(f"[BgTask] Task #{task_id} was user-interrupted, skipping status update")
                return response or ""
        except Exception:
            pass

        # Delegate state transitions to shared handler
        _bg_result = handle_task_completion(
            task_id, response, agent.messages[msg_count_before:] if agent else [],
            bg_session_id,
        )
        if _bg_result == 'backgrounded':
            _broadcast_to_websockets({
                "type": "task_backgrounded",
                "task_id": task_id,
                "message": "后台命令执行中，完成后自动恢复",
                "session_id": bg_session_id,
            })
            return response

        # Push final result to clients (skip heartbeat tasks entirely)
        if not _is_heartbeat:
            _broadcast_to_websockets({
                "type": "message",
                "role": "agent",
                "background": True,
                "session_id": bg_session_id,
                "content": f"**{'🔄 自动恢复' if is_resume else '⏰ 定时'}任务完成**: {user_query[:40]}...\n\n{response[:500]}"
            })

        return response
    except Exception as e:
        update_task_status(task_id, "failed", str(e)[:200], interruption_reason="error")
        _broadcast_to_websockets({
            "type": "error",
            "session_id": bg_session_id,
            "content": f"后台任务失败: {str(e)[:100]}"
        })
        return None
    finally:
        _background_agents.pop(task_id, None)

def start_task_scheduler():
    """Background thread that handles scheduled tasks only.
    Auto-resume is handled by the guardian loop (Phase 1 + Phase 2)."""
    def scheduler_loop():
        print("[TaskScheduler] Started")
        while True:
            try:
                conn = sqlite3.connect(DB_PATH)
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
                
                # 1. Check scheduled tasks due for execution
                cursor.execute(
                    "SELECT * FROM tasks WHERE task_type='scheduled' AND schedule_enabled=1 AND next_run_at <= ? AND status != 'running'",
                    (now_utc,)
                )
                due_tasks = cursor.fetchall()
                
                for task in due_tasks:
                    task_id = task["id"]
                    print(f"[TaskScheduler] Executing scheduled task #{task_id}: {task['title']}")
                    
                    # Update next_run_at and run_count
                    try:
                        from croniter import croniter
                        next_run = croniter(task["schedule_cron"], datetime.now(timezone.utc)).get_next(datetime).strftime('%Y-%m-%d %H:%M:%S')
                        cursor.execute(
                            "UPDATE tasks SET next_run_at=?, last_run_at=?, run_count=run_count+1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (next_run, now_utc, task_id)
                        )
                        conn.commit()
                    except Exception as e:
                        print(f"[TaskScheduler] Cron error for task #{task_id}: {e}")
                        continue
                    
                    # Execute in a separate thread to avoid blocking the scheduler
                    threading.Thread(
                        target=_run_background_task,
                        args=(task_id, task["user_query"]),
                        daemon=True
                    ).start()


                conn.close()
            except Exception as e:
                print(f"[TaskScheduler] Error: {e}")
            
            _time.sleep(30)  # Check every 30 seconds
    
    threading.Thread(target=scheduler_loop, daemon=True).start()



# ── Backoff resume helpers ──

_BACKOFF_SCHEDULE = [30, 30, 120, 120, 300, 300]
_MAX_RESUME_WITH_PROGRESS = 50
_MAX_RESUME_NO_PROGRESS = 10

def _has_recent_progress(task_id: int) -> bool:
    """Check if a task has any successful tool steps in its history."""
    try:
        conn = sqlite3.connect(DB_PATH)
        count = conn.execute(
            "SELECT COUNT(*) FROM task_steps WHERE task_id=? AND success=1", (task_id,)
        ).fetchone()[0]
        conn.close()
        return count > 0
    except Exception:
        return False

def _get_backoff_delay(resume_count: int) -> int:
    """Return seconds to wait before next resume. Returns -1 when maxed out."""
    if resume_count >= len(_BACKOFF_SCHEDULE):
        return -1
    return _BACKOFF_SCHEDULE[resume_count]

def _is_backoff_elapsed(updated_at_str: str, resume_count: int) -> bool:
    """Check if backoff period has elapsed since task was last updated."""
    delay = _get_backoff_delay(resume_count)
    if delay < 0:
        return False
    try:
        from datetime import datetime, timezone
        if not updated_at_str:
            return True
        if 'T' not in updated_at_str:
            updated_at_str = updated_at_str.replace(' ', 'T')
        if not updated_at_str.endswith('Z') and '+' not in updated_at_str:
            updated_at_str += 'Z'
        updated = datetime.fromisoformat(updated_at_str)
        now = datetime.now(timezone.utc)
        return (now - updated).total_seconds() >= delay
    except Exception:
        return True


# _SERVER_START_TIME imported from api.state

def start_background_monitor():
    """Monitor backgrounded tasks — check download/process completion and auto-resume."""
    def monitor_loop():
        import time as _t
        import os as _os
        _output_staleness = {}  # {task_id: {"size": int, "count": int}} for output file growth tracking
        while True:
            try:
                conn = sqlite3.connect(DB_PATH)
                conn.row_factory = sqlite3.Row
                bg_tasks = conn.execute(
                    "SELECT id, user_query, resume_count, max_resume_count, created_at, updated_at, wake_at FROM tasks "
                    "WHERE status='backgrounded'"
                ).fetchall()
                if bg_tasks:
                    print(f"[BgMonitor] Found {len(bg_tasks)} backgrounded task(s) to check")
                for task in bg_tasks:
                    tid = task["id"]

                    # 0. Wake-up timer check — if wake_at is set and time has passed, resume
                    _wake_at = task["wake_at"]
                    if _wake_at:
                        try:
                            _wake_dt = datetime.strptime(_wake_at, '%Y-%m-%d %H:%M:%S')
                            _wake_dt = _wake_dt.replace(tzinfo=timezone.utc)
                            if datetime.now(timezone.utc) >= _wake_dt:
                                print(f"[BgMonitor] Task {tid}: wake timer expired ({_wake_at}), resuming")
                                conn.execute("UPDATE tasks SET wake_at=NULL WHERE id=?", (tid,))
                                conn.commit()
                                ctx = get_task_context(tid)
                                if ctx:
                                    ctx.append({"role": "user", "content": (
                                        "【系统通知】定时唤醒时间已到，请继续执行之前未完成的任务。"
                                    )})
                                    save_task_context(tid, ctx)
                                # Directly resume the task (not just mark interrupted)
                                _uq = conn.execute(
                                    "SELECT user_query FROM tasks WHERE id=?", (tid,)
                                ).fetchone()
                                user_query = _uq[0] if _uq else ""
                                update_task_status(tid, "interrupted",
                                    "定时唤醒", interruption_reason="background_complete")
                                threading.Thread(
                                    target=lambda _tid=tid, _uq=user_query, _ctx=ctx: (
                                        _run_background_task(_tid, _uq, _ctx, True)
                                    ),
                                    daemon=True
                                ).start()
                                continue
                        except Exception as _wake_err:
                            print(f"[BgMonitor] Task {tid}: wake_at parse error: {_wake_err}")

                    # 1. Check downloads linked to this task
                    dl = conn.execute(
                        "SELECT id, status FROM downloads WHERE task_id=? AND status='completed' "
                        "AND background_resumed=0 ORDER BY id DESC LIMIT 1",
                        (tid,)).fetchone()
                    if dl:
                        print(f"[BgMonitor] Task {tid}: download {dl['id']} done — resuming")
                        conn.execute("UPDATE downloads SET background_resumed=1 WHERE id=?",
                                     (dl["id"],))
                        conn.commit()
                        ctx = get_task_context(tid)
                        if ctx:
                            ctx.append({"role": "user", "content": (
                                "【系统通知】后台下载任务已完成，文件已就绪。"
                                "请继续执行之前未完成的任务，不要重复下载已有文件。"
                            )})
                            update_task_status(tid, "interrupted",
                                "后台任务已完成", interruption_reason="background_complete")
                            save_task_context(tid, ctx)
                        continue
                    dl_fail = conn.execute(
                        "SELECT id, error_message FROM downloads WHERE task_id=? "
                        "AND status='failed' AND background_resumed=0 "
                        "ORDER BY id DESC LIMIT 1",
                        (tid,)).fetchone()
                    if dl_fail:
                        conn.execute("UPDATE downloads SET background_resumed=1 WHERE id=?",
                                     (dl_fail["id"],))
                        conn.commit()
                        err = dl_fail["error_message"] or "未知错误"
                        update_task_status(tid, "background_failed",
                            f"下载失败: {err}", interruption_reason="download_failed")
                        ctx = get_task_context(tid)
                        if ctx:
                            ctx.append({"role": "user", "content": (
                                f"【系统通知】后台下载任务失败了。错误信息: {err}\n"
                                "请检查下载管理器中的详细错误，尝试修复后重新下载。"
                            )})
                            save_task_context(tid, ctx)
                        print(f"[BgMonitor] Task {tid}: download failed — notifying agent")
                        continue

                    # 2. Check shell background processes
                    try:
                        from tools.shell import (get_background_processes, cleanup_background_process,
                                                  get_orphan_processes, cleanup_orphan_process,
                                                  adopt_orphan_processes)
                        bg_procs = get_background_processes()
                        pinfo = bg_procs.get(str(tid))

                        # Fallback 1: try orphan pool if main pool misses
                        if not pinfo:
                            orphan_procs = get_orphan_processes()
                            if orphan_procs:
                                # Try to adopt orphans matching this task's session/time
                                task_row = conn.execute(
                                    "SELECT session_id FROM tasks WHERE id=?", (tid,)
                                ).fetchone()
                                task_session = task_row["session_id"] if task_row else None
                                adopted = adopt_orphan_processes(tid, session_id=task_session)
                                if adopted:
                                    # Re-read after adoption
                                    bg_procs = get_background_processes()
                                    pinfo = bg_procs.get(str(tid))
                                    if pinfo:
                                        print(f"[BgMonitor] Task {tid}: adopted orphan process")

                        # Fallback 2: handle backgrounded tasks with no process info (e.g. after restart)
                        if not pinfo:
                            try:
                                updated_str = task["updated_at"] or task["created_at"]
                                if updated_str:
                                    updated_dt = datetime.strptime(updated_str, '%Y-%m-%d %H:%M:%S')
                                    updated_dt = updated_dt.replace(tzinfo=timezone.utc)
                                    now = datetime.now(timezone.utc)
                                    age = now - updated_dt

                                    # Only fail if task was backgrounded BEFORE this server started
                                    # (process info was lost during restart)
                                    if updated_dt < _SERVER_START_TIME:
                                        # Give 2h grace period — the shell process might still be running
                                        if age > timedelta(hours=2):
                                            print(f"[BgMonitor] Task {tid}: bg'd before server restart, no process info for {age.total_seconds()/60:.0f}min — marking failed")
                                            update_task_status(tid, "background_failed",
                                                "后台进程信息因服务重启丢失，无法恢复",
                                                interruption_reason="process_lost")
                                            ctx = get_task_context(tid)
                                            if ctx:
                                                ctx.append({"role": "user", "content": (
                                                    "【系统通知】后台命令的进程信息已丢失（可能因服务重启）。"
                                                    "请重新开始任务，或检查是否有残留进程需要手动处理。"
                                                )})
                                                save_task_context(tid, ctx)
                                            continue
                                    else:
                                        # Task bg'd after startup — process info should exist, don't fail.
                                        # Log periodically to aid debugging.
                                        mins = age.total_seconds() / 60
                                        if mins > 60 and int(mins) % 10 == 0:
                                            print(f"[BgMonitor] Task {tid}: bg'd post-startup, no process info for {mins:.0f}min (waiting)")
                            except Exception as ts_err:
                                print(f"[BgMonitor] Task {tid}: time-check error: {ts_err}")

                        if pinfo:
                            pid = pinfo.get("pid")
                            out_file = pinfo.get("output_file", "")
                            command = pinfo.get("command", "")
                            started_at = pinfo.get("started_at", 0)
                            uptime = _time.time() - started_at if started_at else 0
                            is_long_running = uptime > 1800  # 30+ minutes
                            should_resume = False
                            try:
                                os.kill(pid, 0)  # No signal, just check existence
                                # Process still running — check if output file stopped growing
                                if out_file and _os.path.exists(out_file):
                                    cur_size = _os.path.getsize(out_file)
                                    prev = _output_staleness.get(str(tid), {})
                                    prev_size = prev.get("size", -1)
                                    if cur_size == prev_size and cur_size >= 0:
                                        # File not growing — increment staleness counter
                                        new_count = prev.get("count", 0) + 1
                                        _output_staleness[str(tid)] = {"size": cur_size, "count": new_count}
                                        if is_long_running:
                                            # Long-running server: 15-min output freeze
                                            # Clean up process tracking so BgMonitor stops checking it.
                                            # The task stays backgrounded — user can resume via UI if needed.
                                            if new_count >= 90:  # 90 * 10s = 15min
                                                print(f"[BgMonitor] Task {tid}: long-running ({uptime/60:.0f}min), output frozen 15min — removing process tracking")
                                                cleanup_background_process(str(tid))
                                                _output_staleness.pop(str(tid), None)
                                                ctx = get_task_context(tid)
                                                if ctx:
                                                    ctx.append({"role": "user", "content": (
                                                        f"【系统通知】后台进程（PID {pid}）已持续运行 {uptime/60:.0f} 分钟无输出，"
                                                        f"已解除进程追踪。进程可能仍在后台运行，也可手动在任务管理中终止。"
                                                    )})
                                                    save_task_context(tid, ctx)
                                                continue
                                        else:
                                            # Normal process: 30s output freeze → resume
                                            if new_count >= 3:
                                                should_resume = True
                                                print(f"[BgMonitor] Task {tid}: output stale 30s — treating as done")
                                    else:
                                        # File still growing — reset staleness
                                        _output_staleness[str(tid)] = {"size": cur_size, "count": 0}
                            except OSError:
                                # Process has terminated — resume task
                                should_resume = True
                                cleanup_background_process(str(tid))

                            if not should_resume:
                                continue  # Process still active, skip this check

                            # ── Common resume path (process dead OR output stalled) ──
                            _output_staleness.pop(str(tid), None)
                            cleanup_background_process(str(tid))
                            full_out = ""
                            if out_file and _os.path.exists(out_file):
                                try:
                                    with open(out_file, "r", encoding="utf-8", errors="replace") as rf:
                                        full_out = rf.read()[-5000:]
                                except Exception:
                                    pass
                                try:
                                    _os.remove(out_file)
                                except Exception:
                                    pass
                            ctx = get_task_context(tid)
                            if ctx:
                                ctx.append({"role": "user", "content": (
                                    f"【系统通知】后台命令已执行完毕。\n"
                                    f"命令: `{command[:100]}`\n"
                                    f"输出:\n```\n{full_out[:2000]}\n```\n"
                                    f"请根据输出结果继续执行之前未完成的任务。"
                                )})
                                save_task_context(tid, ctx)
                            task_row = conn.execute(
                                "SELECT user_query FROM tasks WHERE id=?", (tid,)
                            ).fetchone()
                            user_query = task_row["user_query"] if task_row else ""
                            print(f"[BgMonitor] Task {tid}: shell process done — resuming")
                            threading.Thread(
                                target=lambda _tid=tid, _uq=user_query, _ctx=ctx: (
                                    update_task_status(_tid, "interrupted",
                                        "后台命令完成", interruption_reason="background_complete"),
                                    _run_background_task(_tid, _uq, _ctx, True)
                                ),
                                daemon=True
                            ).start()
                    except Exception as e:
                        print(f"[BgMonitor] Shell process check error: {e}")

                conn.close()
            except Exception as e:
                print(f"[BgMonitor] Error: {e}")
            _t.sleep(10)
    threading.Thread(target=monitor_loop, daemon=True).start()
# _check_goal_completeness imported from api.task_core
def _guardian_resume_task(task_id: int) -> None:
    """Resume an interrupted task: load context, mark running, execute one turn."""
    if not _guardian_resume_lock.acquire(blocking=False):
        print(f"[Guardian] Resume #{task_id}: lock held, skipping")
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        conn.close()
        if not row or row[0] != 'interrupted':
            print(f"[Guardian] Resume #{task_id}: not found or not interrupted")
            return

        cfg = load_config()
        model = cfg.get("default_model", "moonshot/kimi-latest")
        print(f"[Guardian] Resume #{task_id}: model={model}")
        from agent.agent import OpenAGCAgent

        # Look up session_id BEFORE creating agent, so sandbox auth (_sandbox_waits)
        # uses the correct key that matches the frontend's session_id
        _hb_session = 1
        try:
            _hb_c = sqlite3.connect(DB_PATH)
            _hb_r = _hb_c.execute("SELECT session_id FROM tasks WHERE id=?", (task_id,)).fetchone()
            if _hb_r: _hb_session = _hb_r[0]
            _hb_c.close()
        except Exception:
            pass
        print(f"[Guardian] Resume #{task_id}: session={_hb_session}")

        agent = OpenAGCAgent(model=model, session_id=_hb_session)
        _apply_pending_sandbox_approvals(agent, _hb_session)
        try:
            ctx = get_task_context(task_id)
            if ctx:
                print(f"[Guardian] Resume #{task_id}: loaded context ({len(ctx)} msgs)")
                # Strip timestamp metadata that may have been serialized
                ctx = [{k:v for k,v in m.items() if k != '_timestamp'} for m in ctx]
                agent.messages.extend(ctx)
            else:
                print(f"[Guardian] Resume #{task_id}: no context found")
        except Exception as e:
            print(f"[Guardian] Resume #{task_id}: context error: {e}")
        update_task_status(task_id, "running")
        print(f"[Guardian] Resume #{task_id}: status set to running, starting run_turn...")

        def _hb_cb(e):
            if e.get("event") == "tool_start":
                add_task_step(task_id, e.get("step", 0), e.get("tool", ""), e.get("tool_label", ""), args_preview=e.get("args_preview", ""), session_id=_hb_session)
            # Persist sandbox approvals so they survive agent recreation
            if e.get("event") == "sandbox_approved":
                _path = e.get("path", "")
                if _path:
                    _pending_sandbox_approvals.setdefault(_hb_session, []).append({
                        "action": "approve_dir", "path": _path
                    })
                return
            _broadcast_to_websockets({"type": "progress", "session_id": _hb_session, "task_id": task_id, **e})

        _background_agents[task_id] = agent
        try:
            print(f"[Guardian] Resume #{task_id}: calling agent.run_turn()...")
            resp = agent.run_turn(
                "【系统恢复】之前执行中断，请继续完成原任务目标。",
                verbose=False,
                progress_callback=_hb_cb,
                skip_rag=True,
                task_id=task_id,
            )
            print(f"[Guardian] Resume #{task_id}: run_turn returned ({len(str(resp or ''))} chars)")
        except Exception as _rt_err:
            print(f"[Guardian] Resume #{task_id}: run_turn crashed: {_rt_err}")
            resp = None
        finally:
            _background_agents.pop(task_id, None)

        # Update task status after run_turn completes — delegate to shared handler
        _resp_str = str(resp or "")[:200]
        print(f"[Guardian] Resume #{task_id}: response prefix: {_resp_str[:100]}")

        if agent.is_interrupted:
            try:
                save_task_context(task_id, agent.messages[1:])
            except Exception:
                pass
            update_task_status(task_id, "interrupted", _resp_str, interruption_reason="user")
        elif hasattr(agent, '_consecutive_failures') and agent._consecutive_failures >= 3:
            try:
                save_task_context(task_id, agent.messages[1:])
            except Exception:
                pass
            update_task_status(task_id, "interrupted", _resp_str, interruption_reason="error")
        else:
            _g_result = handle_task_completion(
                task_id, resp or "", agent.messages[1:] if agent else [],
                update_title=False,
            )
            # Increment resume_count on max_iterations
            if _g_result == 'interrupted':
                try:
                    _hb_c2 = sqlite3.connect(DB_PATH)
                    _hb_c2.execute("UPDATE tasks SET resume_count = resume_count + 1 WHERE id=?", (task_id,))
                    _hb_c2.commit()
                    _hb_c2.close()
                except Exception:
                    pass

        # Broadcast completion to the session's WebSocket clients
        if resp:
            try:
                _broadcast_to_websockets({
                    "type": "message",
                    "role": "agent",
                    "session_id": _hb_session,
                    "content": f"**🔄 自动恢复任务完成**\n\n{resp[:500]}"
                })
            except Exception as _bc_e:
                print(f"[Guardian] Broadcast error: {_bc_e}")
    except Exception as e:
        print(f"[Guardian] Resume #{task_id} error: {e}")
        try:
            update_task_status(task_id, "background_failed",
                f"自动恢复失败: {str(e)[:100]}",
                interruption_reason="error")
        except Exception:
            pass
    finally:
        _guardian_resume_lock.release()


def start_guardian_loop():
    """Background guardian — code-based polling with goal patrol."""
    def _guardian_loop():
        _patrol_last = 0
        while True:
            try:
                cfg = load_config()
                if not cfg.get("heartbeat_enabled", False):
                    _time.sleep(30)
                    continue
                interval = cfg.get("heartbeat_interval", 180)

                conn = sqlite3.connect(DB_PATH)
                _running_count = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='running' AND task_type NOT IN ('heartbeat', 'goal_resume')").fetchone()[0]
                if _running_count > 0:
                    conn.close()
                    _time.sleep(max(interval, 10))
                    continue
                row = conn.execute(
                    "SELECT id, status, interruption_reason, resume_count, max_resume_count, updated_at FROM tasks WHERE status='interrupted' AND (interruption_reason IS NULL OR interruption_reason != 'user') ORDER BY updated_at DESC LIMIT 5"
                ).fetchall()
                if not row:
                    conn.close()
                else:
                    _cfg_mrc = load_config().get("max_resume_count", 10)
                    _chosen = None
                    for r in row:
                        _tid, _status, _reason, _rc, _mrc, _updated = r
                        if _mrc != _cfg_mrc:
                            conn.execute("UPDATE tasks SET max_resume_count=? WHERE id=?", (_cfg_mrc, _tid))
                            _mrc = _cfg_mrc
                        if _rc < _mrc:
                            _chosen = (_tid, _status, _reason, _rc, _mrc, _updated)
                            break
                    if _chosen:
                        tid, t_status, t_reason, t_rc, t_max_rc, t_updated = _chosen
                        print(f"[Guardian] Found task #{tid}: resume_count={t_rc}/{t_max_rc}")
                        conn.close()
                        _guardian_resume_task(tid)
                        _time.sleep(max(interval, 10))
                        continue
                    conn.close()

                # ── Goal Patrol Phase (inside try, ~900s throttle) ──
                now_t = _time.time()
                if now_t - _patrol_last >= 900:
                    _patrol_last = now_t
                    try:
                        from tools.task_plan import load_goals, save_goals
                        _goals = load_goals()
                        _active_goals = [g for g in _goals.get("items", [])
                                         if g.get("status") in ("doing", "pending")]
                        for _goal in _active_goals:
                            _gid = _goal["id"]
                            _task_ids = _goal.get("task_ids", [])
                            if not _task_ids:
                                continue
                            _conn_g = sqlite3.connect(DB_PATH)
                            _incomplete = _conn_g.execute(
                                f"SELECT COUNT(*) FROM tasks WHERE id IN ({','.join('?' for _ in _task_ids)}) "
                                f"AND status NOT IN ('completed', 'failed')", _task_ids
                            ).fetchone()[0]
                            if _incomplete == 0:
                                _conn_g.close()
                                if _check_goal_completeness(_task_ids[0]) == 1:
                                    print(f"[Guardian] Goal #{_gid} auto-completed via patrol")
                                continue
                            _resumable = _conn_g.execute(
                                f"SELECT id FROM tasks WHERE id IN ({','.join('?' for _ in _task_ids)}) "
                                f"AND status='interrupted' AND (interruption_reason IS NULL OR interruption_reason != 'user') "
                                f"ORDER BY id DESC LIMIT 1", _task_ids
                            ).fetchone()
                            _conn_g.close()
                            if _resumable:
                                print(f"[Guardian] Goal patrol: resuming task #{_resumable[0]} for goal #{_gid}")
                                _guardian_resume_task(_resumable[0])
                                break
                            if _incomplete > 0:
                                _desc = _goal.get("desc", "")
                                _new_tid = create_task(f"继续目标: {_desc[:80]}",
                                    f"【系统自动创建】继续大目标 #{_gid}: {_desc}", session_id=1)
                                _goal["task_ids"].append(_new_tid)
                                _goal["updated"] = _time.strftime("%Y-%m-%d %H:%M")
                                save_goals(_goals)
                                import threading as _gthr
                                _gthr.Thread(target=_run_background_task,
                                    args=(_new_tid, f"继续完成大目标: {_desc}", None, False), daemon=True).start()
                                print(f"[Guardian] Created continuation task #{_new_tid} for goal #{_gid}")
                                break
                    except Exception as _g_err:
                        print(f"[Guardian] Goal patrol error: {_g_err}")

                _time.sleep(max(interval, 10))
            except Exception as e:
                print(f"[Guardian] Error: {e}")
                _time.sleep(30)

    threading.Thread(target=_guardian_loop, daemon=True).start()
    print("[Guardian] Started (code-based polling + goal patrol)")


# Background listeners are started from api/server.py (_bg.start_*)
# Do NOT call them here — they would run twice!