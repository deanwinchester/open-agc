"""Background task execution, monitoring, guardian and email listener."""
import os, json, re, sqlite3, threading, shutil
from contextlib import closing
from datetime import datetime, timezone, timedelta
from typing import Optional
from api.db import DB_PATH, db_connect
from api.config import load_config
from core.process import pid_alive
from api.state import (
    connected_websockets, _main_event_loop, _active_agents, _background_agents,
    _pending_sandbox_approvals, _guardian_resume_lock, _SERVER_START_TIME,
    _broadcast_to_websockets, _ws_send_safe, _apply_pending_sandbox_approvals,
    _broadcast_task_history,
)
from api.task_core import (
    create_task, update_task_status, get_task_context, save_task_context,
    save_message, handle_task_completion, claim_task_for_resume,
    add_task_step, _extract_task_title, _record_task_deliverables,
    _load_session_context, _get_task_step_count, _check_goal_completeness,
    _is_task_stale, _get_step_offset, _STALE_RUNNING_MINUTES,
    remediate_goal, _link_task_to_goal, format_checkpoint_notice,
)
from tools.shell import (
    get_background_processes, cleanup_background_process,
    get_orphan_processes, cleanup_orphan_process,
    adopt_orphan_processes, interrupt_shell, _decode_mixed,
)

_time = __import__('time')

# 统一停滞阈值：BgMonitor 每轮约 10s，90 轮 ≈ 15 分钟。进程活着就绝不
# 判完成——输出冻结满阈值才解除追踪（如实告知，不删输出文件）。
_STALL_FREEZE_ROUNDS = 90  # 90 * 10s = 15min


def _email_reply_lines(task_id: int, status: str, summary: str) -> tuple:
    """回信文案按真实终态区分（completed/failed/interrupted/backgrounded
    各自措辞，不再一律 "Task completed"）。返回 (主题状态词, 正文)。"""
    _headlines = {
        "completed": f"Task #{task_id} completed.",
        "failed": f"Task #{task_id} FAILED.",
        "interrupted": f"Task #{task_id} was interrupted before completion.",
        "backgrounded": f"Task #{task_id} is still running in the background.",
    }
    headline = _headlines.get(
        status,
        f"Task #{task_id} has not finished within 10 minutes and is still running.")
    body = f"{headline}\n\nDetails:\n{(summary or '')[:3000]}"
    return status or "running", body


def start_email_listener():
    def email_listener_loop():
        from core.email_service import fetch_emails, send_email
        while True:
            try:
                config = load_config()
                try:
                    conn = db_connect()
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
                        # mark_seen=False：先取信不标记，等任务落库成功后再标记，
                        # 否则落库失败/进程崩溃会永久丢失这封邮件指令。
                        emails = fetch_emails(
                            row["email_imap_server"], email_account, email_password,
                            criteria=criteria, limit=5, mark_seen=False)
                        for e in emails:
                            print(f"[Email Listener] Session {sess_id}: email from {owner}: {e['subject']}")

                            # Create a task for this email
                            email_body = e.get("body", "")
                            full_query = f"📧 邮件指令: {e['subject']}\n\n{email_body}"
                            task_id = create_task(f"邮件指令: {e['subject']}", full_query, session_id=sess_id)

                            # 任务落库成功后才标记已读（顺序保证：落库失败时邮件
                            # 下轮仍 UNSEEN，会被重新拾取）
                            try:
                                from core.email_service import mark_email_seen
                                mark_email_seen(row["email_imap_server"],
                                                email_account, email_password, e.get("id"))
                            except Exception as _mse:
                                print(f"[Email Listener] mark_seen error: {_mse}")

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
                            _final_status = ""
                            _final_response = ""
                            for _ in range(_max_wait // 2):
                                _time.sleep(2)
                                try:
                                    with closing(db_connect()) as _tc:
                                        _trow = _tc.execute(
                                            "SELECT status, result_summary FROM tasks WHERE id=?",
                                            (task_id,)).fetchone()
                                    if _trow and _trow[0] in ("completed", "failed", "interrupted", "backgrounded"):
                                        _final_status = _trow[0]
                                        _final_response = _trow[1] or ""
                                        break
                                except Exception:
                                    pass

                            try:
                                _status_word, _reply_body = _email_reply_lines(
                                    task_id, _final_status, _final_response)
                                ok = send_email(smtp_server, email_account, email_password, owner,
                                    f"Re: {e['subject']} - Task #{task_id} [{_status_word}]",
                                    _reply_body)
                                if ok:
                                    save_message("system", f"📧 邮件指令任务 #{task_id}（{_status_word}）已回复 {owner}", sess_id)
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
        bg_conn = db_connect()
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
        # Agent steps are 1-based per run; offset = MAX(existing step) so the
        # first new step after resume is MAX+1 (no collision with old rows,
        # no gap). Shared helper keeps ws.py / background / guardian aligned.
        step_offset = _get_step_offset(task_id)

    # Detect heartbeat tasks — suppress all progress broadcasts and chat messages
    _is_heartbeat = False
    try:
        _hb_conn = db_connect()
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
                    full_args=event.get("tool_args"),
                    sub_task=event.get("sub_task")
                )
            except Exception:
                pass
        elif event.get("event") == "tool_done":
            done_step = event.get("step", step_counter) + step_offset
            try:
                conn = db_connect()
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
        # 有上下文快照时检查点提示已由 get_task_context 注入；无快照时这里
        # 兜底拼进恢复查询，保证断点信息不丢（ws 手动恢复路径同口径）。
        if not context_messages:
            _ck_notice = format_checkpoint_notice(task_id)
            if _ck_notice:
                query += f"\n\n{_ck_notice}"

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
        response = agent.run_turn(query, False, progress_cb, task_id=task_id, skip_rag=bool(context_messages))

        # If user already interrupted this task, don't overwrite the status
        try:
            chk_conn = db_connect()
            chk_row = chk_conn.execute("SELECT status, interruption_reason FROM tasks WHERE id=?", (task_id,)).fetchone()
            chk_conn.close()
            if chk_row and chk_row[0] == "interrupted" and chk_row[1] == "user":
                print(f"[BgTask] Task #{task_id} was user-interrupted, skipping status update")
                return response or ""
        except Exception:
            pass

        # Delegate state transitions to shared handler
        # 快照与 ws 一致取全量 messages[1:]（跳过 system prompt）——
        # 只存本轮新增会在 resume 时丢失历史轮次；
        # save_task_context 现有防缩守卫保持不变。
        _bg_result = handle_task_completion(
            task_id, response, agent.messages[1:] if agent else [],
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
        if not _is_heartbeat and response:
            from api.task_core import save_message as _bg_save_msg
            try:
                _bg_save_msg("agent", response, bg_session_id, task_id=task_id)
            except Exception as _bg_save_e:
                print(f"[BgTask] Save message error: {_bg_save_e}")
            _broadcast_to_websockets({
                "type": "message",
                "role": "agent",
                "background": True,
                "session_id": bg_session_id,
                "content": f"**{'🔄 自动恢复' if is_resume else '⏰ 定时'}任务完成**: {user_query[:40]}...\n\n{response}"
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


def resume_task_with_late_answer(task_id: int, answer: str) -> dict:
    """Inject a late user answer into an ask-paused task and resume it.

    Shared by REST ``POST /api/tasks/{id}/reply`` and the WS ``tool_reply``
    fallback: used when no live agent holds the task's ``user_input_queue``
    (e.g. ask_user wait timed out and the task was background-paused).
    The answer is appended to the saved context as a user message so the
    resumed agent sees "question already asked + already answered" and
    continues without re-asking. Returns a dict describing the outcome:
    ``{"ok": True}`` on resume, otherwise ``{"ok": False, "error": ...}``.
    """
    try:
        conn = db_connect()
        row = conn.execute(
            "SELECT status, user_query, result_summary, interruption_reason "
            "FROM tasks WHERE id=?", (task_id,)).fetchone()
        conn.close()
    except Exception as e:
        return {"ok": False, "error": "db_error", "message": str(e)}
    if not row:
        return {"ok": False, "error": "not_found",
                "message": f"任务 #{task_id} 不存在"}
    status, user_query, summary, int_reason = row[0], row[1] or "", row[2] or "", row[3]
    if status in ("completed", "failed"):
        label = "已完成" if status == "completed" else "已失败"
        return {"ok": False, "error": "terminal", "status": status,
                "message": f"任务 #{task_id} {label}，无需再回答"}
    if status == "running":
        return {"ok": False, "error": "running", "status": status,
                "message": f"任务 #{task_id} 正在运行，回答暂无法投递"}
    if status == "interrupted" and int_reason == "user":
        return {"ok": False, "error": "user_interrupted", "status": status,
                "message": f"任务 #{task_id} 已被用户中断"}
    # CAS: atomically claim the task so guardian/BgMonitor/another reply
    # can't resume it concurrently.
    if not claim_task_for_resume(task_id, ("backgrounded", "interrupted")):
        return {"ok": False, "error": "claim_failed",
                "message": f"任务 #{task_id} 已被其他路径恢复，请稍候"}
    # Recover the original question from the pause summary (best effort) so
    # the resumed agent can match the answer to what it asked.
    question = ""
    _qm = re.search(r"问题[:：]\s*(.+)$", summary or "")
    if _qm:
        question = _qm.group(1).strip()
    q_part = f"（你此前的问题：{question}）" if question else ""
    ctx = get_task_context(task_id) or []
    ctx.append({"role": "user", "content": (
        f"【系统通知】用户已回答你此前的问题{q_part}。\n"
        f"用户回答: {answer}\n"
        "请直接利用该回答继续执行未完成的任务，不要重复提问。"
    )})
    save_task_context(task_id, ctx)
    # 认领即 running（上方 CAS 已置位），不再降级 interrupted——
    # _run_background_task 内部会自行置 running。
    threading.Thread(
        target=_run_background_task,
        args=(task_id, user_query, ctx, True),
        daemon=True
    ).start()
    print(f"[BgTask] Task #{task_id}: late answer injected, resuming")
    return {"ok": True, "status": "resumed",
            "message": f"回答已注入，任务 #{task_id} 已恢复执行"}


def resume_task_manual(task_id: int, extra_instruction: str = "") -> dict:
    """任务管理界面「继续」按钮的手动恢复入口（REST POST /api/tasks/{id}/resume）。

    与 WS {type:'resume'}（api/ws.py）同一条恢复链路的纯 REST 版本：
    活后台 agent 持有任务 → 指令排队投递；否则 状态校验 →
    claim_task_for_resume CAS 认领（认领即 running，同时清历史中断原因）
    → 附加指令注入恢复上下文 → _run_background_task 后台恢复。
    可恢复状态与 WS 路径对齐：interrupted/backgrounded/background_failed/
    failed/completed。返回 {"ok": ...} 结果字典（同 resume_task_with_late_answer）。
    """
    try:
        conn = db_connect()
        row = conn.execute(
            "SELECT status, user_query FROM tasks WHERE id=?", (task_id,)).fetchone()
        conn.close()
    except Exception as e:
        return {"ok": False, "error": "db_error", "message": str(e)}
    if not row:
        return {"ok": False, "error": "not_found",
                "message": f"任务 #{task_id} 不存在"}
    status, user_query = row[0], row[1] or ""
    extra = (extra_instruction or "").strip()
    # 活后台 agent 持有该任务：不另开恢复，把继续指令排队给它（同 WS 语义）
    _bg_agent = _background_agents.get(task_id)
    if _bg_agent is not None and not getattr(_bg_agent, 'is_interrupted', False):
        try:
            _bg_agent.queue_message(
                f"[用户继续指令] {extra}" if extra else "继续执行未完成的任务")
            return {"ok": True, "status": "queued",
                    "message": f"任务 #{task_id} 正在运行，指令已排队投递"}
        except Exception as e:
            return {"ok": False, "error": "queue_failed",
                    "message": f"指令投递失败: {e}"}
    # CAS 允许的状态集合与 WS resume 一致（WS 原语义：任何非 running 均可恢复）
    _RESUMABLE = ('interrupted', 'backgrounded', 'background_failed',
                  'failed', 'completed')
    if status not in _RESUMABLE:
        return {"ok": False, "error": "not_resumable", "status": status,
                "message": f"任务 #{task_id} 正在运行，无需恢复"
                if status == "running" else
                f"任务 #{task_id} 当前状态（{status}）不可恢复"}
    # CAS: 原子认领，Guardian/BgMonitor/WS 等其他恢复路径不会并发双跑
    if not claim_task_for_resume(task_id, _RESUMABLE):
        return {"ok": False, "error": "claim_failed", "status": status,
                "message": f"任务 #{task_id} 已被其他路径恢复，请稍候"}
    ctx = get_task_context(task_id) or []
    if extra:
        # 附加指令作为最后一条 user 消息注入恢复上下文（agent 恢复时最先看到）
        ctx.append({"role": "user", "content": f"【用户附加指令】{extra}"})
        save_task_context(task_id, ctx)
    threading.Thread(
        target=_run_background_task,
        args=(task_id, user_query, ctx or None, True),
        daemon=True
    ).start()
    print(f"[BgTask] Task #{task_id}: manual resume via REST"
          + (" (with extra instruction)" if extra else ""))
    return {"ok": True, "status": "resumed",
            "message": f"任务 #{task_id} 已恢复执行"}

def _normalize_next_run_at_utc():
    """One-time normalization: recompute next_run_at (UTC) for all enabled schedules.

    Rows written before the UTC fix store next_run_at computed from LOCAL time
    while the scheduler compares against UTC. Rather than guessing each row's
    original timezone, simply recompute from the cron expression in UTC."""
    try:
        from croniter import croniter
        conn = db_connect()
        cursor = conn.cursor()
        rows = cursor.execute(
            "SELECT id, schedule_cron FROM tasks WHERE task_type='scheduled' "
            "AND schedule_enabled=1 AND schedule_cron IS NOT NULL AND schedule_cron != ''"
        ).fetchall()
        now_utc = datetime.now(timezone.utc)
        fixed = 0
        for row in rows:
            try:
                next_run = croniter(row[1], now_utc).get_next(datetime).strftime('%Y-%m-%d %H:%M:%S')
                cursor.execute("UPDATE tasks SET next_run_at=? WHERE id=?", (next_run, row[0]))
                fixed += 1
            except Exception:
                pass
        conn.commit()
        conn.close()
        if fixed:
            print(f"[TaskScheduler] Normalized next_run_at to UTC for {fixed} scheduled task(s)")
    except Exception as e:
        print(f"[TaskScheduler] next_run_at normalization error: {e}")


def start_task_scheduler():
    """Background thread that handles scheduled tasks only.
    Auto-resume is handled by the guardian loop (Phase 1 + Phase 2)."""
    def scheduler_loop():
        print("[TaskScheduler] Started")
        _normalize_next_run_at_utc()
        while True:
            try:
                conn = db_connect()
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
                
                # 1. Check scheduled tasks due for execution.
                # 只点火终态任务：interrupted 让位 Guardian、backgrounded 让位
                # BgMonitor，避免与恢复链路重复拉起同一任务。
                cursor.execute(
                    "SELECT * FROM tasks WHERE task_type='scheduled' AND schedule_enabled=1 AND next_run_at <= ? AND status IN ('completed','failed')",
                    (now_utc,)
                )
                due_tasks = cursor.fetchall()
                
                for task in due_tasks:
                    task_id = task["id"]
                    print(f"[TaskScheduler] Executing scheduled task #{task_id}: {task['title']}")
                    
                    # Update next_run_at and run_count. Always advance the
                    # schedule — even when the fire below is conceded to a
                    # concurrent resume path — so the row never stays due and
                    # fires late after that other run completes.
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
                    
                    # CAS: 认领即 running。并发恢复路径（WS 手动恢复等）抢先认领时
                    # 不重复点火；认领失败直接跳过（schedule 已推进，不会滞留 due）。
                    if not claim_task_for_resume(task_id, ('completed', 'failed')):
                        print(f"[TaskScheduler] Task #{task_id}: fire claim failed (claimed by another path), skipping")
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
        conn = db_connect()
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
    """Check if backoff period has elapsed since task was last updated.

    resume_count=0（首次恢复）无退避，立即可恢复；此后按 _BACKOFF_SCHEDULE
    递增等待。超出日程表档位数后按最后一档（300s）封顶——真正停止由
    Guardian 的 max_resume_count 超限逻辑（置 background_failed）负责，
    避免任务永远滞留 interrupted。
    """
    if resume_count <= 0:
        return True
    delay = _get_backoff_delay(resume_count)
    if delay < 0:
        delay = _BACKOFF_SCHEDULE[-1]
    try:
        if not updated_at_str:
            return True
        # 与 _is_task_stale 同款解析：strptime 兼容 Python 3.10
        # （fromisoformat 在 3.10 不认 'Z' 后缀，会静默落入 except 恒真）
        s = str(updated_at_str).strip().replace('T', ' ')[:19]
        updated = datetime.strptime(s, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - updated).total_seconds() >= delay
    except Exception:
        return True


# _SERVER_START_TIME imported from api.state


def _read_masked_output_tail(path: str, max_chars: int) -> str:
    """Read the tail of a raw shell output file with secret values masked.

    Mask BEFORE tail-cutting so a cut can never split a credential and let it
    escape whole-string matching. Returns "" on any read error.
    """
    try:
        # 原始字节可能逐行混杂 UTF-8/GBK，整块 utf-8+replace 会把 GBK 行变 �
        with open(path, "rb") as rf:
            text = _decode_mixed(rf.read())
    except Exception:
        return ""
    try:
        from core.secrets import mask_secrets
        text = mask_secrets(text)
    except Exception:
        pass
    return text[-max_chars:]


def _flip_bg_to_interrupted(conn, task_id: int, summary: str, reason: str) -> bool:
    """Guarded backgrounded→interrupted flip for startup reconcile.

    WHERE status='backgrounded' 守卫：BgMonitor 的 wake 点火与启动 reconcile
    几乎同时启动，若任务已被抢先 CAS 认领（running），不覆写、不重复恢复。
    语义与 update_task_status(..., 'interrupted') 一致（清 wake_at、刷新
    updated_at）。返回是否成功翻转。
    """
    cur = conn.execute(
        "UPDATE tasks SET status='interrupted', result_summary=?, "
        "interruption_reason=?, wake_at=NULL, updated_at=CURRENT_TIMESTAMP "
        "WHERE id=? AND status='backgrounded'",
        (summary, reason, task_id))
    conn.commit()
    return cur.rowcount == 1


def reconcile_backgrounded_after_restart():
    """服务器重启后统一恢复 backgrounded 任务。

    旧逻辑只处理有下载锚点的任务：无锚点（无 wake_at/无进程/无下载）的任务
    重启后永远滞留 backgrounded。现统一：所有仍 backgrounded 的任务都置
    interrupted 并注入「服务器重启，请继续执行」系统通知，随后走
    claim_task_for_resume CAS + _run_background_task 恢复链路（resume_count
    自然计数）。下载锚点语义保留（完成/失败文案与 background_resumed 原子
    标志），只是不再只有它们被恢复。

    防恢复风暴：沿用 _is_backoff_elapsed 退避（按重启前的 updated_at 判定——
    标记 interrupted 会刷新 updated_at，必须先快照）与 max_resume_count 上限
    （超限置 background_failed）；退避未到期的任务留在 interrupted，等下次
    启动 reconcile 或 Guardian（heartbeat 开启时）接管。heartbeat 默认关闭、
    Guardian 不做 interrupted 恢复，因此必须在启动时主动 reconcile。
    """
    try:
        conn = db_connect()
        # 0. 快照所有 backgrounded 任务（updated_at 用于退避判定——后面的
        #    update_task_status 会刷新它，必须先取出来）
        bg_rows = conn.execute(
            "SELECT id, user_query, resume_count, max_resume_count, updated_at "
            "FROM tasks WHERE status='backgrounded'"
        ).fetchall()
        bg_by_id = {r["id"]: dict(r) for r in bg_rows}

        # 1. 下载完成锚点（原语义：完成文案 + background_resumed 原子标志）。
        #    状态翻转带守卫：BgMonitor 的 wake 点火与本函数几乎同时启动，
        #    可能已抢先 CAS 认领（running）——不覆写、不重复恢复。
        pairs = conn.execute(
            "SELECT d.id as dl_id, d.task_id FROM downloads d "
            "JOIN tasks t ON t.id = d.task_id "
            "WHERE d.status = 'completed' AND d.background_resumed = 0 "
            "AND t.status = 'backgrounded'"
        ).fetchall()
        anchored = set()
        flipped = set()  # 本函数成功翻转 backgrounded→interrupted 的任务
        for p in pairs:
            tid = p["task_id"]
            if not _flip_bg_to_interrupted(
                    conn, tid, "服务器重启，后台任务已完成", "background_complete"):
                print(f"[Startup] Task {tid}: already claimed by another path, skipping")
                continue
            anchored.add(tid)
            flipped.add(tid)
            ctx = get_task_context(tid)
            if ctx:
                ctx.append({"role": "user", "content": (
                    "【系统通知】服务器重启，后台下载任务已完成，文件已就绪。"
                    "请继续执行之前未完成的任务。"
                )})
                save_task_context(tid, ctx)
            conn.execute("UPDATE downloads SET background_resumed=1 WHERE id=?",
                         (p["dl_id"],))
            conn.commit()
            print(f"[Startup] Recovered backgrounded task {tid} from completed download {p['dl_id']}")

        # 2. 下载失败锚点（原语义：只通知会话，不触发恢复）
        failed_pairs = conn.execute(
            "SELECT DISTINCT d.id as dl_id, d.task_id, d.label, d.filename, d.error_message, "
            "(SELECT session_id FROM task_steps WHERE task_id = d.task_id AND session_id IS NOT NULL LIMIT 1) as session_id "
            "FROM downloads d "
            "JOIN tasks t ON t.id = d.task_id "
            "WHERE d.status = 'failed' AND d.background_resumed = 0 "
            "AND t.status IN ('completed', 'interrupted', 'background_failed')"
        ).fetchall()
        for p in failed_pairs:
            label = p["label"] or p["filename"] or f"download #{p['dl_id']}"
            err = p["error_message"] or "未知错误"
            session_id = p["session_id"] or 1
            save_message("system",
                f"❌ 下载失败: {label}\n错误信息: {err}",
                session_id)
            conn.execute("UPDATE downloads SET background_resumed=1 WHERE id=?",
                         (p["dl_id"],))
            conn.commit()
            print(f"[Startup] Recovered failed download #{p['dl_id']} (task {p['task_id']}) — message saved to session {session_id}")

        # 3. 其余 backgrounded 任务（无完成下载锚点，含原"进程信息丢失"分支）：
        #    统一置 interrupted + 注入「服务器重启，请继续执行」通知
        #    （带与步骤 1 相同的 status='backgrounded' 守卫，防与 BgMonitor 抢认领）
        for tid in bg_by_id:
            if tid in anchored:
                continue
            # 重启前登记且仍存活的进程：注册表已持久化并在启动时复活，
            # 留给 BgMonitor 自然接管（继续监控/完成后恢复），这里不置
            # interrupted、不重复恢复。
            try:
                from tools.shell import get_background_processes_for_task
                from core.process import pid_alive as _pid_alive
                _tp = get_background_processes_for_task(tid)
                if any(_pid_alive(i.get("pid")) for i in _tp.values() if i.get("pid")):
                    print(f"[Startup] Task {tid}: tracked process still alive — leaving to BgMonitor")
                    continue
            except Exception:
                pass
            try:
                if not _flip_bg_to_interrupted(
                        conn, tid, "服务器重启，请继续执行", "server_restart"):
                    print(f"[Startup] Task {tid}: already claimed by another path, skipping")
                    continue
                flipped.add(tid)
                ctx = get_task_context(tid)
                if ctx:
                    ctx.append({"role": "user", "content": (
                        "【系统通知】服务器重启，后台进程信息已丢失。"
                        "请检查之前的工作状态，继续执行之前未完成的任务。"
                    )})
                    save_task_context(tid, ctx)
                print(f"[Startup] Task {tid}: marked interrupted (server restart)")
            except Exception as e:
                print(f"[Startup] Task {tid}: recovery error: {e}")

        # 4. 统一恢复（只处理本函数成功翻转的任务）：max_resume_count 上限 +
        #    _is_backoff_elapsed 退避 + claim_task_for_resume CAS（认领即
        #    running）+ _run_background_task
        _cfg_mrc = load_config().get("max_resume_count", 10)
        for tid in flipped:
            row = bg_by_id[tid]
            try:
                _rc = row.get("resume_count") or 0
                _mrc = row.get("max_resume_count") or _cfg_mrc
                if _rc >= _mrc:
                    conn.execute(
                        "UPDATE tasks SET status='background_failed', result_summary=?, "
                        "interruption_reason='max_resume_exceeded', updated_at=CURRENT_TIMESTAMP "
                        "WHERE id=? AND status='interrupted'",
                        (f"自动恢复次数超限（{_rc}/{_mrc}），已停止自动恢复", tid))
                    conn.commit()
                    print(f"[Startup] Task {tid}: resume_count {_rc}/{_mrc} exceeded — marked background_failed")
                    continue
                if not _is_backoff_elapsed(row.get("updated_at"), _rc):
                    print(f"[Startup] Task {tid}: backoff not elapsed (resume_count={_rc}), resume deferred")
                    continue
                # CAS: 认领即 running；认领失败说明另一路径已接管，放弃本次恢复
                if not claim_task_for_resume(tid, ('interrupted',)):
                    print(f"[Startup] Task {tid}: resume claim failed (claimed by another path), skipping")
                    continue
                ctx = get_task_context(tid)
                threading.Thread(
                    target=_run_background_task,
                    args=(tid, row.get("user_query") or "", ctx, True),
                    daemon=True
                ).start()
                print(f"[Startup] Task {tid}: resumed after server restart")
            except Exception as e:
                print(f"[Startup] Task {tid}: resume error: {e}")

        conn.close()
    except Exception as e:
        print(f"[Startup] Background recovery error: {e}")


def start_background_monitor():
    """Monitor backgrounded tasks — check download/process completion and auto-resume."""
    def monitor_loop():
        import time as _t
        import os as _os
        _output_staleness = {}  # {task_id: {"size": int, "count": int}} for output file growth tracking
        while True:
            conn = None
            try:
                conn = db_connect()
                bg_tasks = conn.execute(
                    "SELECT id, user_query, resume_count, max_resume_count, created_at, updated_at, wake_at FROM tasks "
                    "WHERE status='backgrounded'"
                ).fetchall()
                if bg_tasks:
                    print(f"[BgMonitor] Found {len(bg_tasks)} backgrounded task(s) to check")
                # 惰性回收僵尸进程条目：任何状态任务的死 pid 条目都清（下方逐
                # 任务检查只覆盖 backgrounded——running/interrupted 等任务名下
                # 的死条目此前永远显示"运行中"）。本轮要处理的 backgrounded
                # 任务排除在外：其死条目留给下方分支判定（关系"全死才恢复"）。
                try:
                    from tools.shell import reap_dead_background_processes
                    reap_dead_background_processes(
                        alive_fn=pid_alive,
                        exclude_task_ids={str(t["id"]) for t in bg_tasks})
                except Exception as _reap_e:
                    print(f"[BgMonitor] Reap error: {_reap_e}")
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
                                # CAS: claim before resuming so no other path runs this task concurrently
                                if not claim_task_for_resume(tid, ('backgrounded',)):
                                    print(f"[BgMonitor] Task {tid}: resume claim failed (claimed by another path), skipping")
                                    continue
                                conn.execute("UPDATE tasks SET wake_at=NULL WHERE id=?", (tid,))
                                conn.commit()
                                ctx = get_task_context(tid)
                                if ctx:
                                    ctx.append({"role": "user", "content": (
                                        "【系统通知】定时唤醒时间已到，请继续执行之前未完成的任务。"
                                    )})
                                    save_task_context(tid, ctx)
                                # Directly resume the task. 认领即 running（上方 CAS
                                # 已置位），不再降级 interrupted；_run_background_task
                                # 内部会自行置 running。
                                _uq = conn.execute(
                                    "SELECT user_query FROM tasks WHERE id=?", (tid,)
                                ).fetchone()
                                user_query = _uq[0] if _uq else ""
                                threading.Thread(
                                    target=_run_background_task,
                                    args=(tid, user_query, ctx, True),
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
                        # Atomic flag: exactly one path (this monitor or the
                        # direct resume in update_download_progress) consumes
                        # the download row — loser skips, no double resume.
                        _dl_cur = conn.execute(
                            "UPDATE downloads SET background_resumed=1 WHERE id=? AND background_resumed=0",
                            (dl["id"],))
                        conn.commit()
                        if _dl_cur.rowcount != 1:
                            continue
                        print(f"[BgMonitor] Task {tid}: download {dl['id']} done — resuming")
                        ctx = get_task_context(tid)
                        if ctx:
                            ctx.append({"role": "user", "content": (
                                "【系统通知】后台下载任务已完成，文件已就绪。"
                                "请继续执行之前未完成的任务，不要重复下载已有文件。"
                            )})
                            # 守卫：不覆写已被他路径认领（running）的任务——
                            # 全链最后一处「认领后可被覆写」的残口
                            conn.execute(
                                "UPDATE tasks SET status='interrupted', result_summary=?, "
                                "interruption_reason=?, updated_at=CURRENT_TIMESTAMP "
                                "WHERE id=? AND status != 'running'",
                                ("后台任务已完成", "background_complete", tid))
                            save_task_context(tid, ctx)
                        continue
                    dl_fail = conn.execute(
                        "SELECT id, error_message FROM downloads WHERE task_id=? "
                        "AND status='failed' AND background_resumed=0 "
                        "ORDER BY id DESC LIMIT 1",
                        (tid,)).fetchone()
                    if dl_fail:
                        # Atomic flag (same rowcount-wins rule as the completed branch)
                        _dlf_cur = conn.execute(
                            "UPDATE downloads SET background_resumed=1 WHERE id=? AND background_resumed=0",
                            (dl_fail["id"],))
                        conn.commit()
                        if _dlf_cur.rowcount != 1:
                            continue
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
                                                  adopt_orphan_processes, detach_background_process)
                        bg_procs = get_background_processes()
                        task_procs = bg_procs.get(str(tid))  # {pid: info}，一任务多进程

                        # Fallback 1: try orphan pool if main pool misses
                        if not task_procs:
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
                                    task_procs = bg_procs.get(str(tid))
                                    if task_procs:
                                        print(f"[BgMonitor] Task {tid}: adopted orphan process")

                        # Fallback 2: handle backgrounded tasks with no process info (e.g. after restart)
                        if not task_procs:
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

                                    # 无寄托兜底：超 6h 无任何寄托（无进程/无唤醒定时/
                                    # 无进行中下载）的 backgrounded 任务不可能再自己回来，
                                    # 置 background_failed 终结，不再永远滞留。
                                    # （重启前 bg 的任务已被上方 2h process_lost 规则先接住）
                                    if age > timedelta(hours=6):
                                        _has_wake = bool(task["wake_at"])
                                        _has_dl = conn.execute(
                                            "SELECT 1 FROM downloads WHERE task_id=? "
                                            "AND status IN ('downloading','paused') LIMIT 1",
                                            (tid,)).fetchone()
                                        if not _has_wake and not _has_dl:
                                            print(f"[BgMonitor] Task {tid}: no process/wake/download anchor for {age.total_seconds()/3600:.1f}h — marking failed")
                                            update_task_status(tid, "background_failed",
                                                "后台任务超过 6 小时无任何寄托（进程/唤醒/下载），已自动终结",
                                                interruption_reason="no_anchor_timeout")
                                            ctx = get_task_context(tid)
                                            if ctx:
                                                ctx.append({"role": "user", "content": (
                                                    "【系统通知】后台任务超过 6 小时没有任何可追踪的执行寄托"
                                                    "（无进程、无唤醒定时、无进行中下载），系统已将其标记为失败。"
                                                    "如需继续，请重新发起任务。"
                                                )})
                                                save_task_context(tid, ctx)
                                            continue
                            except Exception as ts_err:
                                print(f"[BgMonitor] Task {tid}: time-check error: {ts_err}")

                        if task_procs:
                            # 一任务多进程：按 pid 逐个判活——任务的所有 pid 都死了
                            # 才触发恢复；部分存活继续等（绝不提前判完成）。
                            _alive_entries = []
                            _dead_keys = []
                            for _pk, _pi in task_procs.items():
                                _p = _pi.get("pid")
                                # NOTE: os.kill(pid, 0) would TERMINATE the process on
                                # Windows (TerminateProcess), so use psutil-based check.
                                if _p and pid_alive(_p):
                                    _alive_entries.append(_pi)
                                else:
                                    _dead_keys.append(_pk)
                            should_resume = not _alive_entries
                            if should_resume:
                                # 全部死亡：取最近登记进程的输出/命令用于恢复通知
                                _latest = max(task_procs.values(),
                                              key=lambda i: i.get("started_at") or 0)
                                out_file = _latest.get("output_file", "")
                                command = _latest.get("command", "")
                                cleanup_background_process(str(tid))
                            else:
                                # 清掉已死 pid 的条目（活条目继续监控）
                                for _dk in _dead_keys:
                                    cleanup_background_process(str(tid), _dk)
                                # 统一保守语义（不分长/短任务）：进程活着就绝不
                                # 判完成。输出冻结满 ~15min（_STALL_FREEZE_ROUNDS
                                # 轮 × 10s）的进程移入 orphan 池（detached 标记）
                                # 而不是丢弃——进程继续跑、不删输出文件，在进程
                                # 管理中仍可见可杀；任务的进程全部脱离后写兜底
                                # wake_at（+30min）让任务被自动收回询问用户。
                                _detached = []
                                for _pi in _alive_entries:
                                    _p = _pi.get("pid")
                                    _of = _pi.get("output_file", "")
                                    if not (_of and _os.path.exists(_of)):
                                        continue
                                    _sk = f"{tid}:{_p}"
                                    cur_size = _os.path.getsize(_of)
                                    prev = _output_staleness.get(_sk, {})
                                    prev_size = prev.get("size", -1)
                                    if cur_size == prev_size and cur_size >= 0:
                                        # File not growing — increment staleness counter
                                        new_count = prev.get("count", 0) + 1
                                        since = prev.get("since") or _time.time()
                                        _output_staleness[_sk] = {"size": cur_size, "count": new_count, "since": since}
                                        if new_count >= _STALL_FREEZE_ROUNDS:
                                            stall_min = max(1, int((_time.time() - since) / 60))
                                            print(f"[BgMonitor] Task {tid}: pid {_p} alive but output frozen ~{stall_min}min — detaching to orphan pool (process left running)")
                                            detach_background_process(str(tid), _p)
                                            _output_staleness.pop(_sk, None)
                                            _detached.append((_p, stall_min))
                                    else:
                                        # File still growing — reset staleness
                                        _output_staleness[_sk] = {"size": cur_size, "count": 0}
                                if _detached and not get_background_processes().get(str(tid)):
                                    # 任务名下已无任何被追踪进程（全部冻结脱离）：
                                    # 兜底 wake_at + 如实告知（进程未丢，在 orphan 区）
                                    _p, stall_min = _detached[-1]
                                    try:
                                        _wake_dt = (datetime.now(timezone.utc) + timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M:%S')
                                        conn.execute(
                                            "UPDATE tasks SET wake_at=? WHERE id=? AND wake_at IS NULL",
                                            (_wake_dt, tid))
                                        conn.commit()
                                    except Exception as _wk_e:
                                        print(f"[BgMonitor] Task {tid}: fallback wake_at error: {_wk_e}")
                                    ctx = get_task_context(tid)
                                    if ctx:
                                        ctx.append({"role": "user", "content": (
                                            f"【系统通知】后台进程（PID {_p}）仍在运行，但已约 {stall_min} 分钟无输出，"
                                            f"已转入进程管理列表（标记为已脱离监控，可手动终止）。"
                                            f"系统将在约 30 分钟后自动唤醒本任务确认进展；也可手动恢复任务。"
                                        )})
                                        save_task_context(tid, ctx)

                            if not should_resume:
                                continue  # 进程仍存活（或刚脱离追踪），不恢复

                            # ── Common resume path (all tracked processes dead) ──
                            # CAS: 认领即 running，不再降级 interrupted；认领失败说明
                            # 另一路径（wake/下载直启/Guardian/WS）已接管，放弃本次恢复。
                            if not claim_task_for_resume(tid, ('backgrounded',)):
                                print(f"[BgMonitor] Task {tid}: resume claim failed (claimed by another path), skipping")
                                continue
                            _output_staleness.pop(str(tid), None)
                            for _sk in [k for k in _output_staleness if k.startswith(f"{tid}:")]:
                                _output_staleness.pop(_sk, None)
                            full_out = ""
                            if out_file and _os.path.exists(out_file):
                                # Masked read: raw output may contain credentials
                                full_out = _read_masked_output_tail(out_file, 5000)
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
                            # Broadcast steps before resuming so frontend shows live card
                            try:
                                _bg_sess = conn.execute("SELECT session_id FROM tasks WHERE id=?", (tid,)).fetchone()
                                _broadcast_task_history(tid, _bg_sess[0] if _bg_sess else 1, "running")
                            except Exception:
                                pass
                            # 认领即 running（上方 CAS 已置位），不再降级 interrupted
                            threading.Thread(
                                target=_run_background_task,
                                args=(tid, user_query, ctx, True),
                                daemon=True
                            ).start()
                    except Exception as e:
                        print(f"[BgMonitor] Shell process check error: {e}")

                conn.close()
            except Exception as e:
                print(f"[BgMonitor] Error: {e}")
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
            _t.sleep(10)
    threading.Thread(target=monitor_loop, daemon=True).start()
# _check_goal_completeness imported from api.task_core
def _guardian_resume_task(task_id: int) -> None:
    """Resume an interrupted task: load context, mark running, execute one turn."""
    if not _guardian_resume_lock.acquire(blocking=False):
        print(f"[Guardian] Resume #{task_id}: lock held, skipping")
        return
    try:
        # CAS: atomically flip interrupted→running so exactly one resume path wins
        if not claim_task_for_resume(task_id, ('interrupted',)):
            print(f"[Guardian] Resume #{task_id}: not found, not interrupted, or already claimed")
            return

        cfg = load_config()
        model = cfg.get("default_model", "moonshot/kimi-latest")
        print(f"[Guardian] Resume #{task_id}: model={model}")
        from agent.agent import OpenAGCAgent

        # Look up session_id BEFORE creating agent, so sandbox auth (_sandbox_waits)
        # uses the correct key that matches the frontend's session_id
        _hb_session = 1
        try:
            _hb_c = db_connect()
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

        # Continue step numbering after existing steps (agent is 1-based per
        # run) so resumed steps don't collide with old task_steps rows.
        _hb_step_offset = _get_step_offset(task_id)

        _hb_thinking = {"content": None}

        def _hb_cb(e):
            if "step" in e:
                e["step"] = e.get("step", 0) + _hb_step_offset
            if e.get("event") == "thinking" and e.get("content"):
                _hb_thinking["content"] = e["content"]
            if e.get("event") == "tool_start":
                add_task_step(task_id, e.get("step", 0), e.get("tool", ""), e.get("tool_label", ""), args_preview=e.get("args_preview", ""), session_id=_hb_session, sub_task=e.get("sub_task"), thinking_content=_hb_thinking["content"])
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
            # resume_count 已在上方 claim_task_for_resume CAS 认领时 +1
            # （收敛到认领点，不再于完成后重复计数）
            handle_task_completion(
                task_id, resp or "", agent.messages[1:] if agent else [],
                update_title=False,
            )

        # Broadcast completion to the session's WebSocket clients
        if resp:
            from api.task_core import save_message as _gd_save
            try:
                _gd_save("agent", resp, _hb_session, task_id=task_id)
            except Exception as _gd_save_e:
                print(f"[Guardian] Save message error: {_gd_save_e}")
            try:
                _broadcast_to_websockets({
                    "type": "message",
                    "role": "agent",
                    "session_id": _hb_session,
                    "content": f"**🔄 自动恢复任务完成**\n\n{resp}"
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


def goal_patrol_once(spawn: bool = True) -> list:
    """单轮目标巡检（从 _guardian_loop 抽出，便于测试）。返回动作描述列表。

    每个 doing/pending 目标：
    - 空 task_ids → 接管：创建首个任务并回链（回写走 goals 锁）
    - 任务全部已完结 → _check_goal_completeness 判定（判 NO 内部走补救）
    - 最新任务 interruption_reason == 'user' → 跳过（尊重用户中断，不复活）
    - 有 interrupted 且非 user 且 resume_count < max_resume_count → 恢复
    - 有 backgrounded → 跳过（等唤醒）
    - 其余仍有未完成 → remediate_goal 补救（内部有 goal.resume_count 上限）

    spawn=False 时只建库/改 goals，不启动后台执行线程（测试用）。
    """
    from tools.task_plan import load_goals
    actions = []
    goals = load_goals()
    active_goals = [g for g in goals.get("items", [])
                    if g.get("status") in ("doing", "pending")]
    for goal in active_goals:
        gid = goal["id"]
        task_ids = list(goal.get("task_ids", []) or [])
        desc = goal.get("desc", "")
        conn = db_connect()
        try:
            if not task_ids:
                # 空 task_ids：接管 —— 创建首个任务并回链
                query = f"【系统自动创建】开始大目标 #{gid}: {desc}"
                new_tid = create_task(f"开始目标: {desc[:80]}", query, session_id=1)
                _link_task_to_goal(gid, new_tid)
                if spawn:
                    threading.Thread(
                        target=_run_background_task,
                        args=(new_tid, f"开始并完成大目标: {desc}", None, False),
                        daemon=True).start()
                actions.append(f"goal #{gid}: no tasks — created first task #{new_tid} and linked")
                break
            ph = ",".join("?" for _ in task_ids)
            # failed 不算"已完结"：有失败任务的目标不得判完成
            incomplete = conn.execute(
                f"SELECT COUNT(*) FROM tasks WHERE id IN ({ph}) "
                f"AND status != 'completed'", task_ids
            ).fetchone()[0]
            if incomplete == 0:
                if _check_goal_completeness(task_ids[0]) == 1:
                    actions.append(f"goal #{gid}: auto-completed via patrol")
                continue
            latest = conn.execute(
                f"SELECT id, interruption_reason FROM tasks WHERE id IN ({ph}) "
                f"ORDER BY id DESC LIMIT 1", task_ids
            ).fetchone()
            # 尊重用户中断：该目标最新任务被用户打断则不再自动复活
            if latest and latest["interruption_reason"] == "user":
                actions.append(
                    f"goal #{gid}: latest task #{latest['id']} interrupted by user — skipped")
                continue
            resumable = conn.execute(
                f"SELECT id FROM tasks WHERE id IN ({ph}) "
                f"AND status='interrupted' "
                f"AND (interruption_reason IS NULL OR interruption_reason != 'user') "
                f"AND resume_count < max_resume_count "
                f"ORDER BY id DESC LIMIT 1", task_ids
            ).fetchone()
            bg_active = conn.execute(
                f"SELECT id FROM tasks WHERE id IN ({ph}) "
                f"AND status='backgrounded' "
                f"ORDER BY id DESC LIMIT 1", task_ids
            ).fetchone()
        finally:
            conn.close()
        if resumable:
            actions.append(f"goal #{gid}: resuming task #{resumable['id']}")
            _guardian_resume_task(resumable["id"])
            break
        if bg_active:
            actions.append(
                f"goal #{gid}: has backgrounded task #{bg_active['id']}, skipping new task creation")
            continue
        result = remediate_goal(gid, reason="目标仍有未完成任务，巡检自动补救", spawn=spawn)
        actions.append(f"goal #{gid}: remediation → {result}")
        if result == "remediated":
            break
    return actions


def _has_live_agent_handle(task_id: int) -> bool:
    """True when a live agent object holds this task — thread alive ⇒ not a corpse.

    前台 agent 注册在 _active_agents[session_id][task_id]，后台/恢复 agent
    注册在 _background_agents[task_id]（与 routes_tasks.interrupt_task 同款
    查法）。陈腐 running 复位前必须查活：句柄在就不是孤尸，跳过复位。
    """
    if task_id in _background_agents:
        return True
    for _agents in _active_agents.values():
        if task_id in _agents:
            return True
    return False


def stale_running_rescue_once() -> list:
    """单轮陈腐 running 复位（独立 60s 小循环调用，便于测试）。返回动作描述列表。

    updated_at 由 add_task_step 心跳刷新：'running' 任务超过
    _STALE_RUNNING_MINUTES 无步骤更新说明 worker 线程已死（崩溃前没来得及
    复位状态），复位为 interrupted 使其可被恢复。复位前查活句柄——线程
    活着就不是孤尸。纯 SQL + 内存句柄检查，不受 heartbeat_enabled 门控。
    """
    actions = []
    conn = db_connect()
    try:
        _stale_running = conn.execute(
            "SELECT id, updated_at FROM tasks WHERE status='running' "
            "AND task_type NOT IN ('heartbeat', 'goal_resume')"
        ).fetchall()
        for _sr in _stale_running:
            if not _is_task_stale(_sr["updated_at"]):
                continue
            if _has_live_agent_handle(_sr["id"]):
                actions.append(
                    f"task #{_sr['id']} stale but has live agent handle — skipped")
                continue
            print(f"[StaleRescue] Task #{_sr['id']} running but stale "
                  f"(>{_STALE_RUNNING_MINUTES}min no step update) — resetting to interrupted")
            update_task_status(
                _sr["id"], "interrupted",
                "执行线程失联（无步骤更新超时），已自动标记为可恢复",
                interruption_reason="stale_running")
            actions.append(f"task #{_sr['id']} reset to interrupted (stale_running)")
    finally:
        conn.close()
    return actions


def start_stale_rescue_loop():
    """独立陈腐 running 复位小循环（60s 一轮，纯 SQL，默认常开）。

    从 Guardian 抽出（B4）：heartbeat_enabled 只控制 interrupted 自动恢复
    与 goal patrol；孤尸复位是保底安全网，heartbeat 关闭时也必须可用。
    """
    def _stale_loop():
        while True:
            try:
                stale_running_rescue_once()
            except Exception as e:
                print(f"[StaleRescue] Error: {e}")
            _time.sleep(60)
    threading.Thread(target=_stale_loop, daemon=True).start()


def start_guardian_loop():
    """Background guardian — code-based polling with goal patrol.

    陈腐 running 复位已移交 start_stale_rescue_loop（独立 60s 小循环），
    本循环的 heartbeat_enabled 门控只管 interrupted 自动恢复与 goal patrol。
    """
    def _guardian_loop():
        _patrol_last = 0
        while True:
            try:
                cfg = load_config()
                if not cfg.get("heartbeat_enabled", False):
                    _time.sleep(30)
                    continue
                interval = cfg.get("heartbeat_interval", 180)

                conn = db_connect()
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
                        if _rc >= _mrc:
                            # 超限：置 background_failed，不再每轮重复扫描
                            conn.execute(
                                "UPDATE tasks SET status='background_failed', result_summary=?, "
                                "interruption_reason='max_resume_exceeded', updated_at=CURRENT_TIMESTAMP "
                                "WHERE id=? AND status='interrupted'",
                                (f"自动恢复次数超限（{_rc}/{_mrc}），已停止自动恢复", _tid))
                            conn.commit()
                            print(f"[Guardian] Task #{_tid}: resume_count {_rc}/{_mrc} exceeded — marked background_failed")
                            continue
                        # 退避未到期跳过（resume_count=0 立即可恢复）
                        if not _is_backoff_elapsed(_updated, _rc):
                            print(f"[Guardian] Task #{_tid}: backoff not elapsed (resume_count={_rc}), skipping")
                            continue
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
                        for _act in goal_patrol_once():
                            print(f"[Guardian] Goal patrol: {_act}")
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