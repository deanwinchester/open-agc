import json
import os
import re
import sqlite3
from typing import Any, Dict, List, Optional
from pydantic import Field
from tools.base import BaseTool

class AskUserQuestionTool(BaseTool):
    name: str = "ask_user_question"
    description: str = (
        "Ask the user a question to clarify requirements, request permission, or get missing information. "
        "Use this tool whenever you are unsure how to proceed or need explicit user confirmation. "
        "The agent will pause execution until the user answers."
    )
    
    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question_text": {
                            "type": "string",
                            "description": "The exact question to display to the user."
                        },
                        "options": {
                            "type": "array",
                            "items": {
                                "type": "string"
                            },
                            "description": "(Optional) A list of predefined options for the user to choose from. Leave empty if you want a free-text answer."
                        }
                    },
                    "required": ["question_text"]
                }
            }
        }

    def execute(self, **kwargs) -> str:
        agent_ctx = kwargs.get("_agent_context")
        if not agent_ctx:
            return "Error: Cannot ask user because agent context is missing."
            
        question_text = kwargs.get("question_text")
        options = kwargs.get("options")
        
        # This will block the thread until the user replies via WebSocket
        answer = agent_ctx.wait_for_user_input(question_text, options)

        return f"User replied: {answer}"


class TaskPaused(Exception):
    """Raised when the agent voluntarily pauses itself for background tasks."""
    def __init__(self, reason: str = "", pid: int = None, output_file: str = "",
                 wake_in_minutes: int = None):
        self.reason = reason
        self.pid = pid
        self.output_file = output_file
        self.wake_in_minutes = wake_in_minutes
        extra = f" (定时唤醒: {wake_in_minutes}min)" if wake_in_minutes else ""
        super().__init__(f"Task paused: {reason}{extra}")


class PauseAndWaitTool(BaseTool):
    name: str = "pause_and_wait"
    description: str = (
        "暂停当前任务并等待后台进程完成。用于长时间运行的命令（下载模型、安装依赖、训练模型等）。"
        "系统会保存当前上下文，在后台任务完成后自动恢复执行。"
        "可设置定时唤醒作为兜底（即使后台进程检测失败，到时间也会自动恢复）。"
    )

    def execute(self, reason: str = "", pid: int = None,
                output_file: str = "", description: str = "",
                wake_in_minutes: int = None, **kwargs) -> str:
        raise TaskPaused(
            reason=reason or description or "任务进入后台",
            pid=pid,
            output_file=output_file,
            wake_in_minutes=wake_in_minutes
        )

    def get_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "pause_and_wait",
                "description": (
                    "Pause the current task and yield to background execution. "
                    "Use this when a command returns [Still Running] (long downloads, "
                    "model training, package installation). The system will save context "
                    "and automatically resume when the background task completes. "
                    "You can also set a wake-up timer as a safety net — even if the "
                    "background process detection fails, the task will be resumed on time."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": "Brief description of the background task for the user."
                        },
                        "pid": {
                            "type": "integer",
                            "description": "PID of the background process to monitor."
                        },
                        "output_file": {
                            "type": "string",
                            "description": "Path to the output file for reading results on resume."
                        },
                        "wake_in_minutes": {
                            "type": "integer",
                            "description": "Optional: wake up after this many minutes as fallback. "
                                           "Use when the process might not be tracked correctly "
                                           "(e.g., orphan processes, restart scenarios)."
                        }
                    },
                    "required": ["reason"]
                }
            }
        }


class SearchHistoryTool(BaseTool):
    """Search the agent's own conversation memory — recall past queries, results, decisions."""
    name: str = "search_history"
    description: str = (
        "检索当前会话的完整记忆。当上下文模糊、需要回忆之前讨论过什么、"
        "查找之前获取的数据（URL/文件/命令结果）、或需要确认任务进度时使用。"
        "这是你的\"记忆回溯\"入口——不记得时就搜索。"
    )

    def _expand_item(self, expand_id: str, agent_ctx) -> str:
        """Look up full content of an item by its result ID (e.g. 'step:204:39', 'mem:42')."""
        parts = expand_id.split(":", 2)
        source = parts[0]
        try:
            if source == "step" and len(parts) >= 3:
                tid, step = int(parts[1]), int(parts[2])
                from core.paths import get_data_path
                _db = sqlite3.connect(get_data_path("chat_history.db"))
                _db.row_factory = sqlite3.Row
                _row = _db.execute(
                    "SELECT tool_name, full_args, full_result, args_preview, result_preview, created_at "
                    "FROM task_steps WHERE task_id=? AND step_number=?", (tid, step)
                ).fetchone()
                _db.close()
                if _row:
                    _args = _row["full_args"] or _row["args_preview"] or ""
                    _result = _row["full_result"] or _row["result_preview"] or ""
                    _time = _row["created_at"] or ""
                    _tool = _row["tool_name"]
                    # For manage_memory, the actual content is in the args (content param), not the result
                    if _tool == "manage_memory" and not _result.strip():
                        try:
                            _args_parsed = json.loads(_args)
                            _content_arg = _args_parsed.get("content", "")
                            if _content_arg:
                                _result = _content_arg
                        except Exception:
                            pass
                    return (
                        f"=== 步骤详情 #{tid}:{step} ===\n"
                        f"工具: {_tool}\n时间: {_time}\n\n"
                        f"--- 参数 ---\n{_args[:3000]}\n\n"
                        f"--- 结果 ---\n{_result[:5000]}"
                    )
                return f"未找到步骤 {expand_id}"
            elif source == "mem" and len(parts) >= 2:
                mem_id = int(parts[1])
                _mem_store = getattr(agent_ctx, 'memory_store', None)
                if _mem_store and hasattr(_mem_store, 'get_memory'):
                    _mem = _mem_store.get_memory(mem_id)
                    if _mem:
                        # Record recall with weight=3 (explicit expansion = high value)
                        if hasattr(_mem_store, 'record_recall'):
                            try:
                                _mem_store.record_recall(mem_id, weight=3)
                            except Exception:
                                pass
                        return (
                            f"=== 记忆详情 #{mem_id} ===\n"
                            f"话题: {_mem.get('topic', '')}\n"
                            f"类型: {_mem.get('memory_type', '?')} / {_mem.get('category', '?')}\n"
                            f"状态: {_mem.get('status', 'active')}\n"
                            f"召回: {_mem.get('recall_count', 0)} 次\n\n"
                            f"{_mem.get('content', '(空)')}"
                        )
                return f"未找到记忆 {expand_id}"
            elif source == "msg" and len(parts) >= 2:
                msg_id = int(parts[1])
                from core.paths import get_data_path
                _db = sqlite3.connect(get_data_path("chat_history.db"))
                _row = _db.execute(
                    "SELECT role, content, timestamp as created_at FROM messages WHERE id=?", (msg_id,)
                ).fetchone()
                _db.close()
                if _row:
                    return (
                        f"=== 消息详情 #{msg_id} ===\n"
                        f"角色: {_row['role']}\n时间: {_row['created_at'] or ''}\n\n"
                        f"{_row['content'] or '(空)'}"
                    )
                return f"未找到消息 {expand_id}"
        except Exception as e:
            return f"展开 {expand_id} 时出错: {e}"
        return f"未知的展开目标: {expand_id}"

    def execute(self, query: str = "", search_type: str = "all",
                max_results: int = 8, expand_id: str = "",
                topic: str = "", include_archived: bool = False,
                page: int = 1, memory_type: str = "", **kwargs) -> str:
        agent_ctx = kwargs.get("_agent_context")
        if not agent_ctx:
            return "Error: Cannot search history without agent context."

        # Expand mode: load full content for a specific result
        if expand_id:
            return self._expand_item(expand_id.strip(), agent_ctx)

        messages = getattr(agent_ctx, 'messages', [])

        results = []
        q_lower = query.lower() if query else ""
        q_words = set(q_lower.split()) if q_lower else set()
        scored = []  # (score, time_key, ref_id, text)
        import time as _search_time
        _search_now = _search_time.time()

        def _fmt_ts(ts_val, msg_idx):
            if ts_val and ts_val > 1000000000:
                age = _search_now - ts_val
                if age < 60: return f" ({int(age)}s)"
                if age < 3600: return f" ({int(age//60)}m)"
                if age < 86400: return f" ({int(age//3600)}h)"
                return f" ({int(age//86400)}d)"
            return ""

        def _msg_ts(msg, idx):
            ts = msg.get("_timestamp")
            if ts: return ts
            return idx * 0.001

        for i, msg in enumerate(messages):
            role = msg.get("role", "")
            content = str(msg.get("content", ""))
            score = 0
            ts = _msg_ts(msg, i)
            tag = _fmt_ts(ts, i)

            if search_type in ("all", "user_query") and role == "user":
                if q_lower:
                    score = sum(1 for w in q_words if w in content.lower())
                else:
                    score = 1
                if score > 0:
                    scored.append((score + 2, ts,
                        f"[用户查询{tag}] {content[:300]}"))

            if search_type in ("all", "tool_calls") and role == "assistant":
                tcs = msg.get("tool_calls", [])
                for tc in (tcs or []):
                    if isinstance(tc, dict):
                        fn = tc.get("function", {})
                        name = fn.get("name", "")
                        # Skip noise tools in search results
                        if name in ("search_history",):
                            continue
                        raw_args = fn.get("arguments", "{}")
                        # Normalize: args might be a JSON string OR already-parsed dict
                        if isinstance(raw_args, str):
                            args_str = raw_args
                            try:
                                args = json.loads(raw_args)
                            except Exception:
                                args = {}
                        elif isinstance(raw_args, dict):
                            args = raw_args
                            args_str = json.dumps(raw_args, ensure_ascii=False)
                        else:
                            args = {}
                            args_str = "{}"
                        url = args.get("url", "")
                        fname = args.get("filename", "")
                        fpath = args.get("path", "")
                        cmd = args.get("command", "")[:100]
                        qtext = args.get("query", "") or args.get("question_text", "") or args.get("reason", "")
                        # Build searchable text from all arg values
                        search_text = f"{name} {url} {fname} {fpath} {cmd} {qtext}".lower()
                        # Word-level matching: ALL query words must appear somewhere
                        if q_lower:
                            q_words = q_lower.split()
                            match = all(w in search_text for w in q_words)
                        else:
                            match = False
                        has_data = bool(url or fname or fpath)
                        data_score = 3 if has_data else 0
                        if match:
                            s = f"[工具调用:{name}{tag}]"
                            if url: s += f" url={url}"
                            if fname: s += f" filename={fname}"
                            if fpath: s += f" path={fpath}"
                            if cmd: s += f" cmd={cmd}"
                            if qtext: s += f" query={qtext[:80]}"
                            scored.append((2 + data_score, ts, s))
                        elif not q_lower and has_data:
                            s = f"[工具调用:{name}{tag}]"
                            if url: s += f" url={url}"
                            if fname: s += f" filename={fname}"
                            if fpath: s += f" path={fpath}"
                            scored.append((1 + data_score, ts, s))

            if search_type in ("all", "results") and role == "tool":
                name = msg.get("name", "")
                c = content[:500].replace('\n', ' ').replace('\r', '')
                if q_lower:
                    q_ws = q_lower.split()
                    match = all(w in c.lower() or w in name.lower() for w in q_ws)
                else:
                    match = False
                if match or not q_lower:
                    urls = re.findall(r'(?:https?|ftp)://[^\s\'"<>]{5,}', c)
                    preview = c[:400]
                    s = f"[{name} 结果{tag}]"
                    if urls: s += f" 链接={urls[0]}" + (f" (+{len(urls)-1})" if len(urls)>1 else "")
                    s += f" | {preview}"
                    scored.append((4 if match else 0, ts, s))

            if search_type in ("all", "agent_response") and role == "assistant" and not msg.get("tool_calls"):
                if (q_lower and q_lower in content.lower()
                        # Skip echoes of previous search results
                        and "会话记忆检索结果" not in content[:50]):
                    scored.append((3, ts, f"[Agent 回复{tag}] {content[:600]}"))

        # Also search persisted task_steps in database (survives across run_turns)
        try:
            from core.paths import get_data_path
            db_path = get_data_path("chat_history.db")
            sess_id = getattr(agent_ctx, 'session_id', None)
            if sess_id and os.path.exists(db_path):
                db = sqlite3.connect(db_path)
                db.row_factory = sqlite3.Row

                # Split query into words for word-level matching (same as in-memory search)
                q_words = q_lower.split() if q_lower else []

                # For multi-word queries, a single-word LIKE is too restrictive
                # (Chinese keywords may not appear in args/result text).
                # Fetch recent steps and filter in Python with word-level matching.
                db_steps = db.execute(
                    "SELECT task_id, step_number, tool_name, tool_label, "
                    "args_preview, result_preview, full_result, success, created_at "
                    "FROM task_steps WHERE session_id=? "
                    "ORDER BY task_id DESC, step_number DESC LIMIT 200",
                    (sess_id,)
                ).fetchall()
                db.close()
                for step in db_steps:
                    ap = step["args_preview"] or ""
                    fr = step["full_result"] or ""
                    rp = step["result_preview"] or ""
                    combined = (ap + " " + fr + " " + rp)[:3000]
                    combined_lower = combined.lower()

                    # Word-level matching: rank by how many query words match
                    # (DB records lack conversational context, so partial match is needed)
                    if q_words:
                        match_count = sum(1 for w in q_words if w in combined_lower)
                        if match_count == 0:
                            continue
                        word_score = match_count
                    else:
                        word_score = 1

                    # Skip noise: search_history echoes, manage_memory args are already in FTS5
                    if step["tool_name"] in ("search_history", "manage_memory"):
                        continue

                    urls = re.findall(r'(?:https?|ftp)://[^\s\'"<>]{5,}', combined)
                    preview = (rp or ap or fr)[:300]
                    label = step["tool_label"] or step["tool_name"]
                    created = step["created_at"] or ""
                    db_tag = f" ({created})" if created else ""
                    s = f"[数据库步骤 #{step['task_id']}:{step['step_number']} {label}{db_tag}]"
                    if urls:
                        s += f" 链接={urls[0]}" + (f" (+{len(urls)-1})" if len(urls) > 1 else "")
                    s += f" | {preview[:200]}"
                    # Use created_at timestamp for sorting (convert to seconds for mix with msg _timestamp)
                    try:
                        from datetime import datetime as _dbdt
                        _db_ts = _dbdt.strptime(created, '%Y-%m-%d %H:%M:%S').timestamp() if created else 0
                    except Exception:
                        _db_ts = 0
                    scored.append((3 + word_score if urls else 1 + word_score, _db_ts, s))
        except Exception as e:
            print(f"[SearchHistory] DB search error: {e}")

        # 3. Also search the messages table (user queries + agent responses)
        #    This catches things like "第三个吧" that are in messages but not in task_steps.
        try:
            db_msg = sqlite3.connect(get_data_path("chat_history.db"))
            db_msg.row_factory = sqlite3.Row
            _sess_id = getattr(agent_ctx, 'session_id', None)
            if _sess_id:
                _msg_rows = db_msg.execute(
                    "SELECT id, role, content, timestamp as created_at FROM messages WHERE session_id=? "
                    "ORDER BY id ASC", (_sess_id,)
                ).fetchall()
                db_msg.close()
                for _mr in _msg_rows:
                    _role = _mr["role"]
                    _content = str(_mr["content"] or "")
                    _created = str(_mr["created_at"] or "")
                    _msg_id = _mr.get("id", "")
                    if not _content:
                        continue
                    _id_tag = f" msg:{_msg_id}" if _msg_id else ""
                    _content_lower = _content.lower()
                    if q_lower:
                        _match_count = sum(1 for _w in q_words if _w in _content_lower)
                        if _match_count == 0:
                            continue
                    else:
                        _match_count = 1
                    # Show context around the first match rather than raw start
                    _preview = _content
                    if q_lower and _match_count > 0:
                        _first_match_pos = len(_content)
                        for _w in q_words:
                            _p = _content_lower.find(_w)
                            if _p >= 0 and _p < _first_match_pos:
                                _first_match_pos = _p
                        _ctx_start = max(0, _first_match_pos - 150)
                        _ctx_end = min(len(_content), _first_match_pos + 350)
                        _preview = ""
                        if _ctx_start > 0:
                            _preview = "..."
                        _preview += _content[_ctx_start:_ctx_end]
                        if _ctx_end < len(_content):
                            _preview += "..."
                    else:
                        _preview = _content[:500]
                    _tag = f" ({_created})" if _created else ""
                    _role_label = "用户" if _role == "user" else "Agent"
                    _s = f"[{_role_label}消息{_tag}{_id_tag}] {_preview}"
                    try:
                        from datetime import datetime as _dt2
                        _ts2 = _dt2.strptime(_created, '%Y-%m-%d %H:%M:%S').timestamp() if _created else 0
                    except Exception:
                        _ts2 = 0
                    scored.append((5 + _match_count, _ts2, _s))
        except Exception as e:
            print(f"[SearchHistory] Messages table search error: {e}")

        # 4. Also search the memory store (FTS5) — catches memories saved via
        #    manage_memory that contain the actual content the agent needs.
        try:
            _mem_store = getattr(agent_ctx, 'memory_store', None)
            if _mem_store and hasattr(_mem_store, 'search_memories') and q_lower:
                _mem_results = _mem_store.search_memories(
                    q_lower, top_k=5, topic=topic, include_archived=include_archived,
                    memory_type=memory_type or None,
                )
                if _mem_results:
                    for _mem in _mem_results:
                        _content = str(_mem.get('content', ''))
                        _cat = str(_mem.get('category', 'general'))
                        _type = str(_mem.get('memory_type', 'episode'))
                        if _content:
                            _ts = _mem.get('_timestamp', 0) or _mem.get('created_at', 0)
                            if isinstance(_ts, str):
                                try:
                                    from datetime import datetime as _dt3
                                    _ts3_val = _dt3.strptime(_ts, '%Y-%m-%d %H:%M:%S').timestamp()
                                except Exception:
                                    _ts3_val = 0
                            else:
                                _ts3_val = float(_ts) if _ts else 0
                            _preview = _content[:500]
                            _mem_id = _mem.get('id', '')
                            _id_tag = f" mem:{_mem_id}" if _mem_id else ""
                            _s = f"[记忆存储 ({_cat}/{_type}){_id_tag}] {_preview}"
                            scored.append((4 + _match_count, _ts3_val, _s))
        except Exception as e:
            print(f"[SearchHistory] Memory store search error: {e}")

        # Take top N by relevance score, then sort by time (most recent first)
        scored.sort(key=lambda x: -x[0])
        top = scored[:max_results]

        if not top:
            return (
                f"会话记忆中未找到与 '{query}' 相关的内容。\n"
                f"建议：尝试更短的关键词，或先用 search_available_tools 查看可用工具。"
            )

        # Sort by time_key descending (most recent first)
        top.sort(key=lambda x: -x[1])
        _total = len(top)
        _page = max(1, page)
        _per_page = max(1, max_results)
        _start = (_page - 1) * _per_page
        _end = _start + _per_page
        _page_items = top[_start:_end]

        _total_pages = max(1, (_total + _per_page - 1) // _per_page)
        _type_tag = f" 类型:{memory_type}" if memory_type else ""
        lines = [
            f"会话记忆检索结果 (第{_page}/{_total_pages}页，共{_total}条，关键词: '{query or '全部'}'{_type_tag}):",
            "提示：用 expand_id 查看详情，page=N 翻页，memory_type=core/working/episode 筛选类型。"
        ]
        for _idx, (_score, _ts, _text) in enumerate(_page_items, _start + 1):
            lines.append(f"  #{_idx} {_text}")
        if _end < _total:
            lines.append(f"  ... 还有 {_total - _end} 条，用 page={_page + 1} 查看下一页")

        return "\n".join(lines)

    def get_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "search_history",
                "description": (
                    "Search your own conversation memory. Use this whenever you feel context is fuzzy — "
                    "to recall what the user asked, what files you read, what commands you ran, "
                    "what errors occurred, what URLs you found, or what decisions were made earlier. "
                    "This is your memory recall tool. If you don't remember, search.\n\n"
                    "Progressive drill-down: after a search, you can get full content of a specific "
                    "result by calling search_history with expand_id set to the result's ID "
                    "(e.g. 'step:204:39', 'mem:42', 'msg:100'). The ID is shown in brackets in each result."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "What to search for: a keyword, filename, URL fragment, task description, error message, or leave empty to see recent activity."
                        },
                        "search_type": {
                            "type": "string",
                            "enum": ["all", "user_query", "tool_calls", "results", "agent_response"],
                            "description": "Scope: 'all' (default), 'user_query' (user messages), 'tool_calls' (tool invocations), 'results' (tool outputs), 'agent_response' (your own replies)."
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Max results to return (default 8)."
                        },
                        "expand_id": {
                            "type": "string",
                            "description": "Progressive drill-down: set to a result ID from a previous search "
                                           "(e.g. 'step:204:39', 'mem:42', 'msg:100') to get the FULL content "
                                           "of that specific item. Leave empty for normal search."
                        },
                        "topic": {
                            "type": "string",
                            "description": "Optional: limit search to a specific topic tag (e.g. '车票'). "
                                           "Only affects memory store search."
                        },
                        "include_archived": {
                            "type": "boolean",
                            "description": "Set to true to also search archived (old, 1yr+) memories. "
                                           "Default false. Use when normal search returns nothing useful."
                        },
                        "page": {
                            "type": "integer",
                            "description": "Page number for paginated results (default 1, most recent first). "
                                           "Use when you see '还有 N 条，用 page=N 查看下一页'."
                        },
                        "memory_type": {
                            "type": "string",
                            "enum": ["", "core", "working", "episode"],
                            "description": "Filter memory store by type: 'core' (long-term facts), "
                                           "'working' (current task context), "
                                           "'episode' (task experience). Leave empty for all."
                        }
                    },
                    "required": []
                }
            }
        }
