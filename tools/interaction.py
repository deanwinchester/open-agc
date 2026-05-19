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
    def __init__(self, reason: str = "", pid: int = None, output_file: str = ""):
        self.reason = reason
        self.pid = pid
        self.output_file = output_file
        super().__init__(f"Task paused: {reason}")


class PauseAndWaitTool(BaseTool):
    name: str = "pause_and_wait"
    description: str = (
        "暂停当前任务并等待后台进程完成。用于长时间运行的命令（下载模型、安装依赖、训练模型等）。"
        "系统会保存当前上下文，在后台任务完成后自动恢复执行。"
    )

    def execute(self, reason: str = "", pid: int = None,
                output_file: str = "", description: str = "", **kwargs) -> str:
        raise TaskPaused(
            reason=reason or description or "任务进入后台",
            pid=pid,
            output_file=output_file
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
                    "and automatically resume when the background task completes."
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

    def execute(self, query: str = "", search_type: str = "all",
                max_results: int = 8, **kwargs) -> str:
        agent_ctx = kwargs.get("_agent_context")
        if not agent_ctx:
            return "Error: Cannot search history without agent context."

        messages = getattr(agent_ctx, 'messages', [])

        results = []
        q_lower = query.lower() if query else ""
        q_words = set(q_lower.split()) if q_lower else set()
        scored = []  # (score, index, text)

        for i, msg in enumerate(messages):
            role = msg.get("role", "")
            content = str(msg.get("content", ""))
            score = 0

            if search_type in ("all", "user_query") and role == "user":
                if q_lower:
                    score = sum(1 for w in q_words if w in content.lower())
                else:
                    score = 1
                if score > 0:
                    scored.append((score + 2, i,
                        f"[用户查询] {content[:300]}"))

            if search_type in ("all", "tool_calls") and role == "assistant":
                tcs = msg.get("tool_calls", [])
                for tc in (tcs or []):
                    if isinstance(tc, dict):
                        fn = tc.get("function", {})
                        name = fn.get("name", "")
                        raw_args = fn.get("arguments", "{}")
                        # Normalize: args might be a JSON string OR already-parsed dict
                        if isinstance(raw_args, str):
                            args_str = raw_args
                            try:
                                import json
                                args = json.loads(raw_args)
                            except Exception:
                                args = {}
                        elif isinstance(raw_args, dict):
                            import json
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
                            s = f"[工具调用: {name}]"
                            if url: s += f" url={url}"
                            if fname: s += f" filename={fname}"
                            if fpath: s += f" path={fpath}"
                            if cmd: s += f" cmd={cmd}"
                            if qtext: s += f" query={qtext[:80]}"
                            scored.append((8 + data_score, i, s))
                        elif not q_lower and has_data:
                            s = f"[工具调用: {name}]"
                            if url: s += f" url={url}"
                            if fname: s += f" filename={fname}"
                            if fpath: s += f" path={fpath}"
                            scored.append((1 + data_score, i, s))

            if search_type in ("all", "results") and role == "tool":
                name = msg.get("name", "")
                c = content[:500].replace('\n', ' ').replace('\r', '')
                if q_lower:
                    q_ws = q_lower.split()
                    match = all(w in c.lower() or w in name.lower() for w in q_ws)
                else:
                    match = False
                if match or not q_lower:
                    import re
                    urls = re.findall(r'(?:https?|ftp)://[^\s\'"<>]{5,}', c)
                    preview = c[:400]
                    s = f"[{name} 结果]"
                    if urls: s += f" 链接={urls[0]}" + (f" (+{len(urls)-1})" if len(urls)>1 else "")
                    s += f" | {preview}"
                    scored.append((4 if match else 0, i, s))

            if search_type in ("all", "agent_response") and role == "assistant" and not msg.get("tool_calls"):
                if q_lower and q_lower in content.lower():
                    scored.append((3, i, f"[Agent 回复] {content[:400]}"))

        # Also search persisted task_steps in database (survives across run_turns)
        try:
            from core.paths import get_data_path
            db_path = get_data_path("chat_history.db")
            sess_id = getattr(agent_ctx, 'session_id', None)
            if sess_id and os.path.exists(db_path):
                db = sqlite3.connect(db_path)
                db.row_factory = sqlite3.Row
                like_pattern = f"%{q_lower}%" if q_lower else "%"
                db_steps = db.execute(
                    "SELECT task_id, step_number, tool_name, tool_label, "
                    "args_preview, result_preview, full_result, success "
                    "FROM task_steps WHERE session_id=? AND "
                    "(full_result LIKE ? OR result_preview LIKE ?) "
                    "ORDER BY task_id DESC, step_number DESC LIMIT 20",
                    (sess_id, like_pattern, like_pattern)
                ).fetchall()
                db.close()
                for step in db_steps:
                    fr = step["full_result"] or ""
                    rp = step["result_preview"] or ""
                    combined = (fr + " " + rp)[:3000]
                    urls = re.findall(r'(?:https?|ftp)://[^\s\'"<>]{5,}', combined)
                    preview = (rp or fr)[:300]
                    label = step["tool_label"] or step["tool_name"]
                    s = f"[数据库步骤 #{step['task_id']}:{step['step_number']} {label}]"
                    if urls:
                        s += f" 链接={urls[0]}" + (f" (+{len(urls)-1})" if len(urls) > 1 else "")
                    s += f" | {preview[:200]}"
                    scored.append((3 if urls else 2, step["task_id"] * 1000 + step["step_number"], s))
        except Exception as e:
            print(f"[SearchHistory] DB search error: {e}")

        # Sort by score descending, take top N
        scored.sort(key=lambda x: -x[0])
        top = scored[:max_results]

        if not top:
            return (
                f"会话记忆中未找到与 '{query}' 相关的内容。\n"
                f"建议：尝试更短的关键词，或先用 search_available_tools 查看可用工具。"
            )

        lines = [f"会话记忆检索结果 ({len(top)} 条，关键词: '{query or '全部'}'):"]
        for _, idx, text in sorted(top, key=lambda x: x[1]):  # Sort by message order
            lines.append(f"  {text}")

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
                    "This is your memory recall tool. If you don't remember, search."
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
                        }
                    },
                    "required": ["query"]
                }
            }
        }
