"""WebSocket endpoint - register with app.websocket()."""
import os, sys, json, re, sqlite3, asyncio, threading, queue, concurrent.futures, traceback
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import WebSocket, WebSocketDisconnect
from api.db import DB_PATH
from api.config import load_config, log_agent_error
from api.state import (
    connected_websockets, _main_event_loop, _active_agents, _background_agents,
    _sandbox_waits, _pending_sandbox_approvals, _session_enabled_tools,
    _llamacpp_download_state, _apply_pending_sandbox_approvals,
    _broadcast_to_websockets, _ws_send_safe, _pending_final_responses,
)
from api.task_core import (
    create_task, update_task_status, update_task_type, get_task_context, save_task_context,
    save_message, handle_task_completion,
    add_task_step, _extract_task_title, _record_task_deliverables, _load_session_context,
    _resolve_task_for_query, _resolve_goal_for_query, _check_goal_completeness, _get_task_step_count,
)
from core.paths import get_data_path
from core.llamacpp_manager import get_llamacpp_manager
from core.logger import SessionLogger
from core.stats_manager import get_stats_manager
from agent.agent import OpenAGCAgent
# Import background helper for task history broadcast
from api.state import _broadcast_task_history


async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_websockets.append(websocket)

    # Read session_id from query parameter, default to 1
    ws_session_id = int(websocket.query_params.get("session_id", "1"))

    # Push current download state to the newly connected client
    if _llamacpp_download_state.get("active"):
        await websocket.send_json({
            "type": "llamacpp_download",
            "task": _llamacpp_download_state.get("type", ""),
            "label": _llamacpp_download_state.get("label", ""),
            "progress": _llamacpp_download_state.get("progress", 0.0),
            "stage": _llamacpp_download_state.get("stage", ""),
            "error": _llamacpp_download_state.get("error", "")
        })

    # Broadcast history_steps if this session has a recent or in-progress task
    try:
        _hb_conn = sqlite3.connect(DB_PATH)
        _hb_row = _hb_conn.execute(
            "SELECT id, status FROM tasks WHERE session_id=? AND status IN ('interrupted','completed','running','backgrounded') ORDER BY updated_at DESC LIMIT 1",
            (ws_session_id,)
        ).fetchone()
        _hb_conn.close()
        if _hb_row:
            _broadcast_task_history(_hb_row[0], ws_session_id, _hb_row[1])
    except Exception as _hb_e:
        print(f"[WS] Broadcast task history error: {_hb_e}")

    # Deliver any pending final response that wasn't sent due to WS disconnect
    _pending = _pending_final_responses.get(ws_session_id)
    if _pending:
        try:
            await websocket.send_json({
                "type": "message",
                "role": "agent",
                "content": _pending["content"],
                "session_id": ws_session_id,
                "task_id": _pending.get("task_id"),
            })
            # Clear after successful delivery
            _pending_final_responses.pop(ws_session_id, None)
            print(f"[WS] Delivered pending final response to reconnected client (session {ws_session_id})")
        except Exception as _pend_e:
            print(f"[WS] Failed to deliver pending response: {_pend_e}")

    # Flag to track whether this connection is still alive
    ws_alive = True

    async def _safe_send(data: dict):
        """Send JSON via WebSocket, silently ignore if connection is dead."""
        nonlocal ws_alive
        if not ws_alive:
            return
        try:
            await websocket.send_json(data)
        except Exception:
            ws_alive = False

    # We will maintain conversation history for this session here
    # Load recent chat history from DB instead of starting empty
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        # Load the last 20 user/agent messages for context (exclude tool_step from count)
        cursor.execute("SELECT role, content FROM (SELECT * FROM messages WHERE session_id=? AND role != 'tool_step' ORDER BY id DESC LIMIT 20) ORDER BY id ASC", (ws_session_id,))
        rows = cursor.fetchall()
        conn.close()

        # LLMs strict require 'assistant' not 'agent'
        session_history = []
        for row in rows:
            role = row["role"]
            if role in ("tool_step",):  # skip internal display messages
                continue
            if role == "agent":
                role = "assistant"
            session_history.append({"role": role, "content": row["content"]})
    except Exception as e:
        print(f"Failed to load chat history: {e}")
        session_history = []
    last_query = ""  # Track last query for retry
    agent_is_running = False
    receive_task = None # Persistent receive_task to avoid concurrency issues

    # Replay the most recent task's steps for this session
    # Only replay if the user hasn't sent new messages after the task completed
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT t.id, t.status, t.created_at, t.updated_at FROM tasks t "
            "WHERE t.id IN (SELECT DISTINCT task_id FROM task_steps WHERE session_id=?) "
            "ORDER BY t.created_at DESC LIMIT 1",
            (ws_session_id,))
        last_task = cursor.fetchone()
        if last_task:
            # Only replay if no newer user messages exist after the task completed
            check_time = last_task["updated_at"] or last_task["created_at"]
            newer_msgs = cursor.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id=? AND role='user' AND timestamp > ?",
                (ws_session_id, check_time)).fetchone()[0]
            if newer_msgs == 0:
                steps = cursor.execute(
                    "SELECT step_number, tool_name, tool_label, args_preview, "
                    "result_preview, full_result, full_args, success, thinking_content FROM task_steps "
                    "WHERE task_id=? ORDER BY created_at",
                    (last_task["id"],)).fetchall()
                if steps:
                    await _safe_send({
                        "type": "history_steps",
                        "task_id": last_task["id"],
                        "task_status": last_task["status"],
                        "steps": [dict(s) for s in steps],
                        "session_id": ws_session_id
                    })
        conn.close()
    except Exception as e:
        print(f"[WS] Task replay error: {e}")
    
    async def run_agent_with_progress(query: str, model: str = None, agent_profile_name: str = None, images: list = None, resume_task_id: int = None):
        """Run agent in a thread and push progress to WebSocket via a Queue.

        If resume_task_id is set, steps are appended to the existing task instead of creating a new one.
        """
        nonlocal session_history, last_query, agent_is_running, receive_task, ws_alive, ws_session_id

        if agent_is_running:
            return "BUSY"

        agent_is_running = True
        # Pre-resolve task_id BEFORE agent execution so tools always get a valid _task_id.
        # resume_task_id is used when explicitly resuming; otherwise detect new vs continuation.
        if resume_task_id:
            ws_task_id = resume_task_id
        else:
            ws_task_id = _resolve_task_for_query(ws_session_id, query)
        step_offset = 0

        # Always compute step offset from existing steps for ANY existing task,
        # not just explicit resume. Prevents step numbering reset after WS reconnect.
        if ws_task_id:
            try:
                _offset_conn = sqlite3.connect(DB_PATH)
                _max_step = _offset_conn.execute(
                    "SELECT COALESCE(MAX(step_number), -1) FROM task_steps WHERE task_id=?",
                    (ws_task_id,)).fetchone()[0]
                _offset_conn.close()
                step_offset = _max_step + 1
            except Exception as e:
                print(f"[Task] Step offset error: {e}")

        # Additional resume-specific handling (status update)
        if resume_task_id:
            try:
                update_task_status(resume_task_id, "running")
            except Exception as e:
                print(f"[Task] Resume status error: {e}")

        try:
            import queue as thread_queue
            progress_queue = thread_queue.Queue()
            has_taken_action = False
            agent = None
            _bg_pid = None
            _bg_wake = None

            # Accumulate shell output per step for tool_step message persistence
            _step_outputs: dict = {}
            def progress_callback(event: dict):
                nonlocal has_taken_action, ws_task_id, _bg_pid
                """Thread-safe: push progress events from thread pool into queue."""
                # Handle sandbox_approved: persist to pending approvals so it
                # survives agent recreation on task resume
                if event.get("event") == "sandbox_approved":
                    _path = event.get("path", "")
                    _sid = event.get("session_id") or ws_session_id
                    if _path:
                        _pending_sandbox_approvals.setdefault(_sid, []).append({
                            "action": "approve_dir", "path": _path
                        })
                        print(f"[Sandbox] Persisted approval: {_path} for session {_sid}")
                    return  # Don't queue this event to frontend

                # Record task steps (offset on resume to continue numbering)
                adjusted_step = event.get("step", 0) + step_offset
                event["step"] = adjusted_step

                # Accumulate shell output per step for tool_step persistence
                # Capture PID from pause_and_wait for BgMonitor tracking
                if event.get("event") == "task_backgrounded":
                    _bg_pid = event.get("pid")
                    _bg_wake = event.get("wake_in_minutes")

                if event.get("event") == "shell_output":
                    text = event.get("text", "")
                    if text:
                        prev = _step_outputs.get(adjusted_step, "")
                        _step_outputs[adjusted_step] = (prev + text)[-8000:]  # cap at 8K chars

                if ws_task_id and event.get("event") == "tool_start":
                    try:
                        add_task_step(
                            task_id=ws_task_id,
                            step_number=adjusted_step,
                            tool_name=event.get("tool", ""),
                            tool_label=event.get("tool_label", ""),
                            args_preview=event.get("args_preview", ""),
                            session_id=ws_session_id,
                            tool_call_id=event.get("tool_call_id"),
                            full_args=event.get("tool_args")
                        )
                    except Exception as e:
                        print(f"[Task] Failed to add step: {e}")

                if ws_task_id and event.get("event") == "tool_done":
                    # Link any pending downloads to this task (downloads run AFTER tool execution)
                    try:
                        import tools.download as _dl
                        pending = getattr(_dl, '_pending_task_links', {})
                        dl_ids = pending.pop(ws_session_id, [])
                        if dl_ids:
                            print(f"[Task] tool_done: linking {len(dl_ids)} download(s) to task {ws_task_id}")
                            dl_conn = sqlite3.connect(DB_PATH)
                            for dl_id in dl_ids:
                                dl_conn.execute(
                                    "UPDATE downloads SET task_id=? WHERE id=? AND task_id IS NULL",
                                    (ws_task_id, dl_id))
                                # Check if this download already failed before linking
                                already_failed = dl_conn.execute(
                                    "SELECT status, label, filename, error_message FROM downloads WHERE id=? AND status='failed'",
                                    (dl_id,)).fetchone()
                                if already_failed:
                                    err = already_failed[3] or "未知错误"
                                    label = already_failed[1] or already_failed[2] or f"download #{dl_id}"
                                    save_message("system",
                                        f"❌ 下载失败: {label}\n错误信息: {err}",
                                        ws_session_id)
                                    _broadcast_to_websockets({
                                        "type": "download_failed",
                                        "download_id": dl_id,
                                        "task_id": ws_task_id,
                                        "session_id": ws_session_id,
                                        "label": label,
                                        "error": err
                                    })
                                    # Inject failure info into the running agent so it can retry
                                    try:
                                        _aa_dict = _active_agents.get(ws_session_id, {})
                                        agent_ref = next(iter(_aa_dict.values())) if _aa_dict else None
                                        if agent_ref:
                                            agent_ref.pending_messages.append(
                                                f"【系统通知】下载失败了。\n文件: {label}\n错误: {err}\n"
                                                f"download_id: {dl_id}\n"
                                                f"请尝试其他方式重新下载（如换源），如果确实无法下载则结束任务。"
                                            )
                                            print(f"[Task] Injected download failure into agent for session {ws_session_id}")
                                    except Exception as inject_err:
                                        print(f"[Task] Failed to inject failure into agent: {inject_err}")
                                    print(f"[Task] tool_done: download #{dl_id} already failed — notified session {ws_session_id}")
                                # Also check if download already completed before linking
                                already_done = dl_conn.execute(
                                    "SELECT status, label, filename FROM downloads WHERE id=? AND status='completed'",
                                    (dl_id,)).fetchone()
                                if already_done:
                                    label = already_done[1] or already_done[2] or f"download #{dl_id}"
                                    try:
                                        _aa_dict = _active_agents.get(ws_session_id, {})
                                        agent_ref = next(iter(_aa_dict.values())) if _aa_dict else None
                                        if agent_ref:
                                            agent_ref.pending_messages.append(
                                                f"【系统通知】后台下载已完成。\n文件: {label}\n"
                                                f"请继续执行之前的任务。"
                                            )
                                            print(f"[Task] Injected download completion into agent for session {ws_session_id}")
                                    except Exception as inject_err:
                                        print(f"[Task] Failed to inject download completion: {inject_err}")
                            dl_conn.commit()
                            dl_conn.close()
                    except Exception as link_err:
                        print(f"[Task] tool_done link error: {link_err}")
                    try:
                        # Update the step with result and tool_call_id
                        conn = sqlite3.connect(DB_PATH)
                        cursor = conn.cursor()
                        cursor.execute(
                            "UPDATE task_steps SET result_preview=?, full_result=?, success=?, tool_call_id=COALESCE(?, tool_call_id) WHERE task_id=? AND step_number=?",
                            (event.get("result_preview", ""),
                             event.get("full_result", event.get("result_preview", "")),
                             1 if event.get("success") else 0,
                             event.get("tool_call_id"),
                             ws_task_id, adjusted_step)
                        )
                        conn.commit()
                        conn.close()
                    except Exception as e:
                        print(f"[Task] Failed to update step: {e}")

                # Update task_steps with tool result (result_preview only set on tool_done)
                if ws_task_id and event.get("event") == "tool_done":
                    try:
                        _rpreview = event.get("result_preview", "")
                        _success = 1 if event.get("success") else 0
                        _conn_step = sqlite3.connect(DB_PATH)
                        _conn_step.execute(
                            "UPDATE task_steps SET result_preview=?, success=? WHERE task_id=? AND step_number=?",
                            (_rpreview, _success, ws_task_id, adjusted_step)
                        )
                        _conn_step.commit()
                        _conn_step.close()
                    except Exception as _step_e:
                        print(f"[WS] Task step update error: {_step_e}")

                # tool_step is persisted in task_steps table -- no need to duplicate in messages

                # Attach task_id to the event so frontend can track it
                if ws_task_id:
                    event["task_id"] = ws_task_id
                # Adjust step number for resumed tasks
                if step_offset:
                    event["step"] = event.get("step", 0) + step_offset

                progress_queue.put(event)
            
            _cfg_model = load_config().get("default_model", "moonshot/kimi-latest")
            current_model = model or os.getenv("DEFAULT_MODEL") or _cfg_model

            # Auto-start llama-server if using a llamacpp model
            if "llamacpp/" in current_model:
                lm = get_llamacpp_manager()
                if not lm.is_running():
                    model_filename = current_model.replace("llamacpp/", "")
                    await _safe_send({
                        "type": "status",
                        "message": f"正在启动 llama-server 并加载 {model_filename}..."
                    })
                    lm.start(model_filename)
                    for i in range(120):
                        await asyncio.sleep(0.5)
                        if lm.is_running():
                            await _safe_send({
                                "type": "status",
                                "message": "llama-server 就绪，开始处理..."
                            })
                            break
                    else:
                        _broadcast_to_websockets({
                            "type": "llamacpp_download",
                            "task": "binary",
                            "label": "llama-server 启动失败",
                            "progress": 0.0,
                            "stage": "error",
                            "error": "模型文件可能不兼容或损坏，请尝试下载其他 GGUF 模型"
                        })
                        await _safe_send({
                            "type": "system_message",
                            "message": "❌ **llama-server 启动失败**\n\n模型文件可能不兼容或损坏，请尝试下载其他 GGUF 模型。\n可在「设置 → 模型管理」中更换模型。"
                        })
                        save_message("system",
                            "❌ llama-server 启动失败，模型文件可能不兼容或损坏，请在设置中更换模型。",
                            ws_session_id)
                        agent_is_running = False
                        return

            from core.logger import SessionLogger
            session_logger = SessionLogger(
                log_dir=get_data_path("logs"),
                session_id=ws_session_id
            )
            agent = OpenAGCAgent(model=current_model, session_id=ws_session_id,
                                 logger=session_logger,
                                 pre_enabled_tools=_session_enabled_tools.get(ws_session_id))
            _apply_pending_sandbox_approvals(agent, ws_session_id)
            _active_agents.setdefault(ws_session_id, {})[ws_task_id or 0] = agent
            
            # Inject custom agent profile prompt if specified
            if agent_profile_name and agent_profile_name != "default":
                config = load_config()
                profiles_raw = config.get("agent_profiles", [])
                try:
                    profiles = json.loads(profiles_raw) if isinstance(profiles_raw, str) else profiles_raw
                    for p in profiles:
                        if isinstance(p, dict) and p.get("name") == agent_profile_name and p.get("prompt"):
                            agent.system_prompt_base = f"【角色设定: {p['name']}】\n{p['prompt']}\n\n---\n" + agent.system_prompt_base
                            if p.get("model"):
                                agent.llm.default_model = p["model"]
                            break
                except Exception as e:
                    print(f"Failed to load agent profile {agent_profile_name}: {e}")
            
            # Inject previous session history
            if session_history:
                session_history = [{k:v for k,v in m.items() if k != '_timestamp'} for m in session_history]
                agent.messages.extend(session_history)

            # Save the first user query so it survives crashes in the messages table
            try:
                save_message("user", query, ws_session_id)
            except Exception as _msg_e:
                print(f"[WS] Save user message failed: {_msg_e}")

            loop = asyncio.get_event_loop()
            
            import concurrent.futures
            agent_future = loop.run_in_executor(
                None, 
                lambda: agent.run_turn(query, False, progress_callback, images=images, task_id=ws_task_id, skip_rag=bool(resume_task_id))
            )
            
            # Handle agent progress and check for interruption
            while not agent_future.done() and ws_alive:
                if receive_task is None:
                    receive_task = asyncio.create_task(websocket.receive_text())

                done, pending = await asyncio.wait(
                    [receive_task],
                    timeout=0.15,
                    return_when=asyncio.FIRST_COMPLETED
                )

                if receive_task in done:
                    try:
                        data = receive_task.result()
                        user_msg = json.loads(data)
                        if user_msg.get("type") == "interrupt":
                            agent.is_interrupted = True
                            interrupt_shell()
                            if ws_task_id:
                                update_task_status(ws_task_id, "interrupted", interruption_reason="user")
                            # Also interrupt any background agents for this session
                            for tid, bg_agent in list(_background_agents.items()):
                                bg_agent.is_interrupted = True
                            interrupt_shell()
                            # Cancel any active download
                            if _llamacpp_download_state.get("active"):
                                _llamacpp_download_state["cancelled"] = True
                                _llamacpp_download_state["active"] = False
                                _broadcast_to_websockets({
                                    "type": "llamacpp_download",
                                    "task": _llamacpp_download_state.get("type", ""),
                                    "label": "下载已取消",
                                    "progress": 0.0,
                                    "stage": "error",
                                    "error": "用户中断"
                                })
                        elif user_msg.get("type") == "tool_reply":
                            agent.user_input_queue.put(user_msg.get("answer"))
                            # Also unblock any background agents waiting for user input
                            _answer = user_msg.get("answer", "")
                            for _tid, _bg_a in list(_background_agents.items()):
                                try:
                                    _bg_a.user_input_queue.put_nowait(_answer)
                                except Exception as _queue_e:
                                    print(f"[WS] Background agent queue error (task {_tid}): {_queue_e}")
                        elif user_msg.get("type") == "sandbox_response":
                            sid = user_msg.get("session_id", ws_session_id)
                            action = user_msg.get("action", "deny_once")
                            wait = _sandbox_waits.get(sid)
                            if wait:
                                wait["result"]["action"] = action
                                wait["result"]["path"] = user_msg.get("path", "")
                                wait["event"].set()
                                print(f"[WS] Sandbox response: {action} for {sid}")
                            elif action in ("approve_once", "approve_dir", "approve_always", "approve_session"):
                                # Late approval: apply to running agent's whitelist directly
                                _path = user_msg.get("path", "")
                                if _path and hasattr(agent, '_session_sandbox_whitelist'):
                                    import os as _ws_os
                                    _ws_dir = _ws_os.path.dirname(_ws_os.path.abspath(_path))
                                    agent._session_sandbox_whitelist.add(_ws_dir)
                                    agent._session_sandbox_whitelist.add(_path)
                                    print(f"[WS] Late sandbox approval applied to running agent: {_path}")
                        else:
                            # Non-blocking input: queue message to agent
                            q = user_msg.get("query", user_msg.get("text", ""))
                            if q.strip():
                                # Get the most recent agent for this session
                                _aa_sess = _active_agents.get(ws_session_id, {})
                                a = next(iter(_aa_sess.values())) if _aa_sess else None
                                if a:
                                    a.queue_message(q)
                                    save_message("user", q, ws_session_id)
                                    # Save as tool_step in the task flow
                                    if ws_task_id:
                                        import json as _jj
                                        _interject_data = {
                                            "step": -1,
                                            "tool": "user_interjection",
                                            "tool_label": "用户插入",
                                            "args_preview": q[:200],
                                            "success": True,
                                            "output": ""
                                        }
                                        save_message("tool_step", _jj.dumps(_interject_data, ensure_ascii=False), ws_session_id)
                                        # Also broadcast as a progress event so frontend shows it live
                                        await _safe_send({
                                            "type": "progress",
                                            "event": "tool_start",
                                            "step": -1,
                                            "tool": "user_interjection",
                                            "tool_label": "用户插入",
                                            "args_preview": q[:200],
                                            "task_id": ws_task_id,
                                            "session_id": ws_session_id,
                                            "background": False
                                        })
                                        await _safe_send({
                                            "type": "progress",
                                            "event": "tool_done",
                                            "step": -1,
                                            "tool": "user_interjection",
                                            "tool_label": "用户插入",
                                            "result_preview": q[:200],
                                            "success": True,
                                            "task_id": ws_task_id,
                                            "session_id": ws_session_id,
                                            "background": False
                                        })
                                    print(f"[WS] Queued message to agent session {ws_session_id}")
                        receive_task = None
                    except (WebSocketDisconnect, RuntimeError):
                        ws_alive = False
                        if websocket in connected_websockets:
                            connected_websockets.remove(websocket)
                        _active_agents.pop(ws_session_id, None)
                        receive_task = None
                        # Don't raise — ws_alive=False will let outer loop break
                    except Exception as _recv_e:
                        print(f"[WS] Receive task cleanup error: {_recv_e}")
                        receive_task = None

                # Drain the thread-safe queue (no cross-thread race)
                while True:
                    try:
                        event = progress_queue.get_nowait()
                        event["session_id"] = ws_session_id
                        await _safe_send({
                                "type": "progress",
                                **event
                            })
                    except thread_queue.Empty:
                        break

            while not progress_queue.empty():
                try:
                    event = progress_queue.get_nowait()
                    event["session_id"] = ws_session_id
                    await _safe_send({
                        "type": "progress",
                        **event
                    })
                except Exception as _prog_e:
                    print(f"[WS] Progress send error: {_prog_e}")
                    # Continue draining remaining events — don't break
            
            response = await agent_future
            session_history = agent.messages[1:]
            # Persist enabled tools for next turn (avoid re-discovering)
            _session_enabled_tools[ws_session_id] = getattr(agent, 'active_tool_names', set())

            # ── Store response for reconnecting clients (before heavy DB) ──
            # Only skip [TASK_BACKGROUNDED] (internal protocol, not user-facing)
            if response and not response.startswith("[TASK_BACKGROUNDED]"):
                _pending_final_responses[ws_session_id] = {
                    "content": response,
                    "task_id": ws_task_id,
                }

            # Handle [INTERJECTION_REJECTED] prefix (strips it from response)
            if response and response.startswith("[INTERJECTION_REJECTED]"):
                try:
                    _reject_line = response[len("[INTERJECTION_REJECTED] "):]
                    _newline_pos = _reject_line.find("\n")
                    _reject_json = _reject_line[:_newline_pos] if _newline_pos >= 0 else _reject_line
                    _remaining = _reject_line[_newline_pos + 1:] if _newline_pos >= 0 else ""
                    import json as _rj
                    reject_data = _rj.loads(_reject_json)
                    rejected_msg = reject_data.get("message", "")
                    if rejected_msg and ws_session_id:
                        new_task_id = create_task(
                            reject_data.get("response", rejected_msg)[:120] or rejected_msg[:120],
                            rejected_msg, session_id=ws_session_id)
                        print(f"[WS] Created new task #{new_task_id} for rejected interjection")
                        await _safe_send({"type": "system_message",
                            "message": f"📋 **已为您创建新任务 #{new_task_id}**（当前任务不相关）\n{reject_data.get('reason', '')}",
                            "session_id": ws_session_id})
                except Exception as _rj_err:
                    print(f"[WS] Interjection reject error: {_rj_err}")
                response = _remaining if '_remaining' in locals() else response

            if ws_task_id and response:
                # Run task completion in a background thread so it doesn't block the
                # event loop with heavy json.dumps(agent.messages[1:]) serialization.
                _bg_messages = agent.messages[1:]  # capture before thread
                _tb_ws_task_id = ws_task_id
                _tb_response = response
                _tb_session_id = ws_session_id
                _tb_wake = _bg_wake

                def _do_completion():
                    try:
                        _r = handle_task_completion(
                            _tb_ws_task_id, _tb_response, _bg_messages, _tb_session_id,
                            wake_minutes=_tb_wake,
                        )
                        # Save agent response (fast DB write)
                        if _r != 'interrupted_user':
                            try:
                                save_message("agent", _tb_response, _tb_session_id)
                            except Exception:
                                pass

                        if _r == 'backgrounded':
                            if _bg_pid:
                                try:
                                    from tools.shell import get_background_processes as _gbp
                                    _bg_procs = _gbp()
                                    if str(_tb_ws_task_id) not in _bg_procs:
                                        from tools.shell import _background_process_info as _bgi
                                        from tools.shell import _background_process_lock as _bgl
                                        with _bgl:
                                            _bgi[str(_tb_ws_task_id)] = {"pid": _bg_pid, "command": "", "started_at": _time.time()}
                                except Exception:
                                    pass
                            # Send task_backgrounded notification from background thread
                            _broadcast_to_websockets({
                                "type": "task_backgrounded",
                                "task_id": _tb_ws_task_id,
                                "message": "后台命令执行中，完成后自动恢复",
                                "session_id": _tb_session_id,
                            })
                            return

                        # Update stats (fast DB writes)
                        try:
                            stats = get_stats_manager().get_task_usage(_tb_ws_task_id)
                            if stats:
                                _conn_tmp = sqlite3.connect(DB_PATH)
                                _conn_tmp.execute(
                                    "UPDATE tasks SET total_tokens=?, total_cost=?, prompt_tokens=?, completion_tokens=?, cached_tokens=? WHERE id=?",
                                    (stats["total"], stats.get("cost", 0.0), stats.get("prompt", 0), stats.get("completion", 0), stats.get("cached", 0), _tb_ws_task_id))
                                _conn_tmp.commit()
                                _conn_tmp.close()
                        except Exception:
                            pass
                    except Exception as _bg_comp_e:
                        print(f"[WS] Background completion error: {_bg_comp_e}")

                import threading as _comp_thr
                _comp_thr.Thread(target=_do_completion, daemon=True).start()

            return (response, ws_task_id)
        except Exception as e:
            error_msg = str(e)
            # One automatic retry: give the agent a chance to recover
            # by injecting the error and letting it try a different approach.
            if agent and ws_task_id and not getattr(agent, '_auto_retried', False):
                agent._auto_retried = True
                print(f"[WS] Auto-retry: agent failed with '{error_msg[:100]}', giving one more chance...")
                # Save current context before retry
                try:
                    save_task_context(ws_task_id, agent.messages[1:])
                except Exception:
                    pass
                # Inject the error into the agent's context for self-correction
                retry_prompt = (
                    f"[系统通知] 你之前的操作遇到了意外错误，已被自动恢复。\n"
                    f"错误信息：{error_msg[:300]}\n\n"
                    f"请分析原因，不要重复同一操作，尝试完全不同的策略来完成原始任务。"
                )
                agent.messages.append({"role": "user", "content": retry_prompt})
                try:
                    # Re-run with skip_rag=True (context already loaded)
                    response = agent.run_turn(
                        user_input=None,  # None = resume, don't re-add user message
                        verbose=False,
                        progress_callback=progress_callback,
                        task_id=ws_task_id,
                        skip_rag=True,
                    )
                    # If retry succeeded, log and return
                    try:
                        save_message("agent", response, ws_session_id)
                    except Exception as _retry_save_e:
                        print(f"[WS] Save retry message failed: {_retry_save_e}")
                    print(f"[WS] Auto-retry succeeded for task {ws_task_id}")
                    return (response, ws_task_id)
                except Exception as retry_e:
                    print(f"[WS] Auto-retry also failed for task {ws_task_id}: {retry_e}")

            # Final failure — save context and mark failed
            if ws_task_id:
                if agent:
                    try:
                        save_task_context(ws_task_id, agent.messages[1:])
                    except Exception:
                        pass
                update_task_status(ws_task_id, "failed", error_msg[:200], interruption_reason="error")
            raise
        finally:
            agent_is_running = False
            # If ws_alive is False (WS disconnected mid-execution), broadcast the
            # response to any reconnected client via the pending response mechanism.
            # The main loop handles sending for the normal (alive) case.
            try:
                _resp = locals().get('response')
                if not ws_alive and _resp and not _resp.startswith("[TASK_BACKGROUNDED]"):
                    _broadcast_to_websockets({
                        "type": "message", "role": "agent",
                        "content": _resp, "session_id": ws_session_id,
                        "task_id": locals().get('ws_task_id'),
                    })
                    print(f"[WS] Broadcast final response after disconnect (session {ws_session_id})")
            except Exception as _resp_e:
                print(f"[WS] Broadcast final response error: {_resp_e}")
            # Clean up finished agent from _active_agents so new messages
            # can start a fresh agent loop (instead of being queued to a dead agent)
            try:
                _aa_sess = _active_agents.get(ws_session_id, {})
                _keys_to_remove = [k for k, v in _aa_sess.items() if v is agent]
                for k in _keys_to_remove:
                    del _aa_sess[k]
                    print(f"[WS] Removed finished agent (task_id={k}) from _active_agents")
            except Exception as _clean_e:
                print(f"[WS] Agent cleanup error: {_clean_e}")

    try:
        while True:
            if not ws_alive:
                print("[WS] Not alive, exiting main loop")
                break
            config = load_config()
            heartbeat_enabled = config.get("heartbeat_enabled", False)
            heartbeat_interval = config.get("heartbeat_interval", 180)

            try:
                # Wait for user message with timeout for heartbeat
                if receive_task is None:
                    receive_task = asyncio.create_task(websocket.receive_text())
                
                timeout = heartbeat_interval if heartbeat_enabled else None
                
                # Check if we already have a finished receive_task result from a previous agent run_turn interrupt
                if receive_task.done():
                    data = receive_task.result()
                    receive_task = None 
                else:
                    done, pending = await asyncio.wait([receive_task], timeout=timeout)
                    if receive_task in done:
                        data = receive_task.result()
                        receive_task = None
                    else:
                        # ── Timeout: no user message received ──
                        # Recovery is handled by the background guardian.
                        continue
                        if not is_heartbeat:
                            continue

                user_msg = json.loads(data)
                msg_type = user_msg.get("type", "query")
                resume_id_for_run = None

                if msg_type == "switch_session":
                    # Switch to a different session without reconnecting WebSocket
                    new_sid = int(user_msg.get("session_id", 1))
                    if new_sid != ws_session_id:
                        ws_session_id = new_sid
                        # Reload session_history for the new session's LLM context
                        try:
                            _ss_conn = sqlite3.connect(DB_PATH)
                            _ss_conn.row_factory = sqlite3.Row
                            _ss_cursor = _ss_conn.cursor()
                            _ss_cursor.execute(
                                "SELECT role, content FROM (SELECT * FROM messages WHERE session_id=? AND role != 'tool_step' ORDER BY id DESC LIMIT 20) ORDER BY id ASC",
                                (ws_session_id,))
                            _ss_rows = _ss_cursor.fetchall()
                            _ss_conn.close()
                            session_history = []
                            for _ss_row in _ss_rows:
                                _ss_role = _ss_row["role"]
                                if _ss_role in ("tool_step",): continue
                                if _ss_role == "agent": _ss_role = "assistant"
                                session_history.append({"role": _ss_role, "content": _ss_row["content"]})
                        except Exception as _ss_e:
                            print(f"[WS] Session switch: failed to reload history: {_ss_e}")
                            session_history = []
                        # Broadcast history_steps for the new session's most recent task
                        try:
                            _ss_conn2 = sqlite3.connect(DB_PATH)
                            _ss_conn2.row_factory = sqlite3.Row
                            _ss_last = _ss_conn2.execute(
                                "SELECT id, status FROM tasks WHERE session_id=? AND status IN ('interrupted','completed','running','backgrounded') ORDER BY updated_at DESC LIMIT 1",
                                (ws_session_id,)
                            ).fetchone()
                            _ss_conn2.close()
                            if _ss_last:
                                _broadcast_task_history(_ss_last[0], ws_session_id, _ss_last[1])
                        except Exception as _ss_e2:
                            print(f"[WS] Session switch: failed to broadcast history: {_ss_e2}")
                    continue

                if msg_type == "sandbox_response":
                    # Resolve a pending sandbox auth wait
                    sid = user_msg.get("session_id", ws_session_id)
                    action = user_msg.get("action", "deny_once")
                    wait = _sandbox_waits.get(sid)
                    if wait:
                        wait["result"]["action"] = action
                        wait["result"]["path"] = user_msg.get("path", "")
                        wait["event"].set()
                        print(f"[WS] Sandbox response: {action} for session {sid}")
                    elif action in ("approve_once", "approve_dir", "approve_always", "approve_session"):
                        # Late approval after wait timed out — save and resume task
                        _path = user_msg.get("path", "")
                        if _path:
                            print(f"[WS] Late sandbox approval: {action} for {_path}")
                            _pending_sandbox_approvals.setdefault(sid, []).append({
                                "action": action, "path": _path
                            })
                            # Find backgrounded/interrupted task for this session and resume
                            try:
                                _late = sqlite3.connect(DB_PATH)
                                _late_t = _late.execute(
                                    "SELECT id, user_query FROM tasks WHERE session_id=? AND status IN ('backgrounded','interrupted') ORDER BY id DESC LIMIT 1",
                                    (sid,)).fetchone()
                                _late.close()
                                if _late_t:
                                    _tid2 = _late_t[0]
                                    _uq2 = _late_t[1] or ""
                                    _ctx2 = get_task_context(_tid2)
                                    if _ctx2 is None:
                                        _ctx2 = []
                                    _ctx2.append({"role": "user", "content":
                                        f"【系统通知】你之前因沙箱权限等待超时而中断。路径 {_path} 已获得用户授权，"
                                        f"请重新尝试之前被阻止的操作。"})
                                    save_task_context(_tid2, _ctx2)
                                    update_task_status(_tid2, "interrupted",
                                        "延迟授权触发恢复", interruption_reason="background_complete")
                                    print(f"[WS] Resuming task #{_tid2} after late sandbox approval")
                                    import threading as _thr
                                    _thr.Thread(
                                        target=_run_background_task,
                                        args=(_tid2, _uq2, _ctx2, True),
                                        daemon=True
                                    ).start()
                            except Exception as _late_err:
                                print(f"[WS] Late sandbox resume error: {_late_err}")
                    continue

                if msg_type == "resume":
                    # Resume an interrupted task
                    task_id = user_msg.get("task_id")
                    if task_id and not agent_is_running:
                        # Don't resume if this task is already running in background
                        if task_id in _background_agents:
                            _bg_agent = _background_agents.get(task_id)
                            if _bg_agent and not getattr(_bg_agent, 'is_interrupted', False):
                                # Queue to the existing background agent instead
                                _extra = user_msg.get("extra_instruction", "").strip()
                                _msg = f"[用户继续指令] {_extra}" if _extra else "继续执行未完成的任务"
                                _bg_agent.queue_message(_msg)
                                print(f"[WS] Task #{task_id} is already running in background — queued resume message")
                                continue
                        resume_id_for_run = task_id
                        try:
                            ctx = get_task_context(task_id)
                            # Always load steps for replay and context
                            conn2 = sqlite3.connect(DB_PATH)
                            conn2.row_factory = sqlite3.Row
                            steps = conn2.execute(
                                "SELECT step_number, tool_name, tool_label, args_preview, "
                                "result_preview, full_result, full_args, success, thinking_content FROM task_steps "
                                "WHERE task_id=? ORDER BY created_at", (task_id,)).fetchall()
                            # Also fetch the original task goal
                            task_row = conn2.execute(
                                "SELECT user_query FROM tasks WHERE id=?", (task_id,)).fetchone()
                            conn2.close()
                            await _safe_send({
                                "type": "history_steps",
                                "task_id": task_id,
                                "task_status": "resuming",
                                "steps": [dict(s) for s in steps]
                            })
                            if ctx:
                                session_history = ctx
                            original_goal = (task_row["user_query"] if task_row else "")
                            query = "【系统提示】任务已恢复，请根据历史上下文，从上次中断的地方继续执行任务。"
                            # Append extra instruction if provided by user
                            extra = user_msg.get("extra_instruction", "").strip()
                            if extra:
                                query += f"\n\n用户附加指令：{extra}"
                        except Exception as e:
                            print(f"[WS] Resume error: {e}")
                            query = "继续执行未完成的任务。"
                            extra = user_msg.get("extra_instruction", "").strip()
                            if extra:
                                query += f"\n\n用户附加指令：{extra}"
                        retry_model = None
                        agent_profile_name = None
                        ws_images = None
                    else:
                        continue
                elif msg_type == "retry":
                    query = user_msg.get("query", last_query)
                    retry_model = user_msg.get("model", None)
                    agent_profile_name = user_msg.get("agent_name", None)
                    ws_images = user_msg.get("images", None)
                    if not query.strip():
                        continue
                else:
                    query = user_msg.get("query", "")
                    retry_model = None
                    agent_profile_name = user_msg.get("agent_name", None)
                    ws_images = user_msg.get("images", None)
                    if not query.strip():
                        continue

                    # If an agent is already running for this session (from another WS
                    # connection or previous turn), queue the message instead of starting
                    # a new agent loop.
                    _existing_session_agents = _active_agents.get(ws_session_id, {})
                    if _existing_session_agents:
                        _existing_session_agent = next(iter(_existing_session_agents.values()))
                        if not getattr(_existing_session_agent, 'is_interrupted', False):
                            _existing_session_agent.queue_message(query)
                            save_message("user", query, ws_session_id)
                            print(f"[WS] Existing agent active for session {ws_session_id} — queued message (not starting new loop)")
                            continue

                    # Auto-reconstruct context for continuation queries
                    # Pass recent conversation context for accurate classification
                    _recent_ctx = "\n".join(
                        f"{m.get('role','?')}: {str(m.get('content',''))[:100]}"
                        for m in (session_history[-4:] if session_history else [])
                    ) if session_history else ""
                    _resolved_goal = _resolve_goal_for_query(query, recent_context=_recent_ctx)
                    if _resolved_goal > 0:
                        try:
                            from tools.task_plan import load_goals as _ct_load
                            _ct_goals = _ct_load()
                            _matched_goal = None
                            for _ct_item in _ct_goals.get("items", []):
                                if _ct_item["id"] == _resolved_goal:
                                    _matched_goal = _ct_item
                                    query += f"\n\n[关联大目标 #{_resolved_goal}] {_ct_item['desc']}"
                                    break
                            # Load the last task associated with this goal
                            if _matched_goal and _matched_goal.get("task_ids") and not resume_id_for_run:
                                _goal_tids = sorted(_matched_goal["task_ids"], reverse=True)
                                for _goal_tid in _goal_tids:
                                    try:
                                        _ctx = get_task_context(_goal_tid)
                                        if _ctx:
                                            resume_id_for_run = _goal_tid
                                            session_history = _ctx
                                            print(f"[WS] Loaded goal #{_resolved_goal} task #{_goal_tid} context ({len(_ctx)} msgs)")
                                            break
                                    except Exception:
                                        continue
                        except Exception:
                            pass


                # Send thinking status
                await _safe_send({
                    "type": "status",
                    "message": "Agent is thinking...",
                    "session_id": ws_session_id
                })

                # Run the agent
                response, ws_task_id = await run_agent_with_progress(query, retry_model, agent_profile_name, images=ws_images, resume_task_id=resume_id_for_run)

                # Send the final response via broadcast (reaches ALL connected
                # sockets, bypasses ws_alive poison). This is the single send point.
                _broadcast_to_websockets({
                    "type": "message", "role": "agent",
                    "content": response, "session_id": ws_session_id,
                    "task_id": ws_task_id,
                })
                # Clear pending response — successfully dispatched to all sockets
                _pending_final_responses.pop(ws_session_id, None)
                
            except (WebSocketDisconnect, RuntimeError) as _ws_err:
                # WebSocketDisconnect doesn't contain "disconnect" in str() output.
                # Check by type for WebSocketDisconnect or message for RuntimeError.
                if isinstance(_ws_err, WebSocketDisconnect) or "disconnect" in str(_ws_err).lower():
                    print("[WS] Client disconnected")
                    break
                # Not a disconnect — re-raise
                raise
            except Exception as e:
                import traceback
                traceback.print_exc()
                err_str = str(e).lower()
                # Log error to stderr only — don't pollute the chat session
                log_agent_error(str(e))
                print(f"[Agent Error] {e}")
                # Only show API key hint in chat (actionable by user); hide internal errors
                if "api_key" in err_str or "authentication" in err_str or "not found" in err_str or "key" in err_str:
                    hint = (
                        "---\n**💡 提示：您似乎尚未配置此模型的 API Key！**\n\n"
                        "以 DeepSeek 为例，请前往 [DeepSeek 开放平台](https://platform.deepseek.com/api_keys) "
                        "免费申请一个 API Key，然后在左侧边栏的「设置 - 模型配置」中填入并保存即可开始对话！"
                    )
                    save_message("system", hint, ws_session_id)
                    await _safe_send({
                        "type": "error",
                        "content": hint,
                        "session_id": ws_session_id
                    })
                else:
                    # Non-actionable errors: notify frontend to stop thinking animation
                    await _safe_send({
                        "type": "error",
                        "content": "Agent 执行出错，任务已标记为失败。",
                        "session_id": ws_session_id
                    })
                    print(f"[Agent Error] Full traceback above. Hiding from chat to avoid clutter.")
                
    except WebSocketDisconnect:
        print("Client disconnected")
        if websocket in connected_websockets:
            connected_websockets.remove(websocket)
        _active_agents.pop(ws_session_id, None)  # nested dict cleaned up
        _session_enabled_tools.pop(ws_session_id, None)
        # Keep _pending_final_responses for reconnecting clients (cleared after delivery)
    except Exception as e:
        print(f"WebSocket error: {e}")
        if websocket in connected_websockets:
            connected_websockets.remove(websocket)
        _active_agents.pop(ws_session_id, None)  # nested dict cleaned up
        _session_enabled_tools.pop(ws_session_id, None)

