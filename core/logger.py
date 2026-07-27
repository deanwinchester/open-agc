"""
SessionLogger — Structured JSONL logging for agent sessions.

Logs each event (user query, agent response, tool call, tool result,
thinking, error) as a JSON line to data/logs/{date}_{session_id}.jsonl.
"""
import os
import json
from datetime import datetime
from typing import Optional


class SessionLogger:
    def __init__(self, log_dir: str, session_id: int, model: str = None):
        self.session_id = session_id
        self.log_dir = log_dir
        self.model = model
        os.makedirs(log_dir, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        self.log_path = os.path.join(log_dir, f"{today}_{session_id}.jsonl")

    def _write(self, entry: dict):
        entry.setdefault("ts", datetime.now().isoformat())
        entry.setdefault("session_id", self.session_id)
        if self.model:
            entry.setdefault("model", self.model)
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def log_user_query(self, text: str):
        self._write({"type": "user_query", "text": text[:5000]})

    def log_agent_response(self, text: str):
        self._write({"type": "agent_response", "text": text[:10000]})

    def log_tool_call(self, tool_name: str, args: dict = None):
        self._write({
            "type": "tool_call",
            "tool": tool_name,
            "args": json.dumps(args, ensure_ascii=False)[:2000] if args else ""
        })

    def log_tool_result(self, tool_name: str, result: str, success: bool = True):
        # Mask before truncation: callers (agent.py) pass the raw tool result,
        # and non-shell/python tools do no tool-layer masking — without this a
        # vault password echoed by read_file/fetch_url would land here in
        # plaintext. Masking the full string first also keeps a password
        # straddling the 8000-char cut from leaking half-masked.
        from core.secrets import mask_secrets
        self._write({
            "type": "tool_result",
            "tool": tool_name,
            "result": mask_secrets(result or "")[:8000],
            "success": success
        })

    def log_thinking(self, content: str, iteration: int = 0):
        self._write({
            "type": "thinking",
            "content": content[:5000],
            "iteration": iteration
        })

    def log_error(self, message: str, traceback: str = ""):
        self._write({
            "type": "error",
            "message": message[:2000],
            "traceback": traceback[:5000]
        })

    def log_system(self, message: str):
        self._write({"type": "system", "message": message[:2000]})


def read_logs(log_dir: str, session_id: int, limit: int = 50,
              log_type: str = None) -> list:
    """Read recent log entries for a session.

    Args:
        log_dir: Path to the logs directory.
        session_id: Filter by session_id.
        limit: Max entries to return.
        log_type: Optional filter by type (user_query, tool_call, etc.).
    """
    entries = []
    if not os.path.isdir(log_dir):
        return entries

    # Find matching log files (any date, matching session)
    for fname in sorted(os.listdir(log_dir), reverse=True):
        if not fname.endswith(".jsonl"):
            continue
        if f"_{session_id}.jsonl" not in fname:
            continue
        fpath = os.path.join(log_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if log_type and entry.get("type") != log_type:
                            continue
                        entries.append(entry)
                        if len(entries) >= limit:
                            return entries
                    except json.JSONDecodeError:
                        continue
        except Exception:
            continue

    return entries
