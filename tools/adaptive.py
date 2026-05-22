"""Adaptive resident tool scoring — promote frequently-used non-core tools.

Tracks built-in, auto-generated, and MCP tool usage across sessions.
At session startup, non-core tools with an adaptation score above
ADAPTIVE_THRESHOLD are automatically added to the active tool set,
eliminating the need for explicit search_available_tools discovery.
"""
import json
import math
import os
from datetime import datetime
from typing import Dict, Set

FREQ_FILE = "tool_frequency.json"
ADAPTIVE_THRESHOLD = 5.0
MAX_ADAPTIVE_TOOLS = 5


def _load_tool_usage(data_dir: str) -> dict:
    path = os.path.join(data_dir, FREQ_FILE)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_tool_usage(data_dir: str, usage: dict):
    path = os.path.join(data_dir, FREQ_FILE)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(usage, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Adaptive] Failed to save tool usage: {e}")


def _compute_adaptive_score(entry: dict) -> float:
    """Score a tool's adaptation weight.

    Formula:  calls * log2(sessions + 1) / sqrt(days_since_last_use + 1)

    - calls: total lifetime calls
    - sessions: distinct sessions the tool was used in
    - days_since_last_use: time decay — unused tools gradually fall below threshold
    """
    calls = entry.get("calls", 0)
    sessions = entry.get("sessions", 0)
    last_used = entry.get("last_used", "")

    if calls == 0:
        return 0.0

    days = 0.0
    if last_used:
        try:
            last = datetime.fromisoformat(last_used)
            days = (datetime.now() - last).total_seconds() / 86400.0
        except (ValueError, TypeError):
            days = 0.0

    return calls * math.log2(sessions + 1) / math.sqrt(max(1.0, days + 1))


def get_adaptive_tools(data_dir: str,
                       all_tool_names: Set[str],
                       core_tool_names: Set[str]) -> Set[str]:
    """Return the subset of non-core tools that qualify for adaptive residency.

    Results are capped at MAX_ADAPTIVE_TOOLS — only the highest-scoring
    tools are returned.
    """
    usage = _load_tool_usage(data_dir)
    scored = []
    for name in all_tool_names:
        if name in core_tool_names:
            continue
        entry = usage.get(name)
        if not entry:
            continue
        score = _compute_adaptive_score(entry)
        if score >= ADAPTIVE_THRESHOLD:
            scored.append((score, name))

    scored.sort(key=lambda x: x[0], reverse=True)
    selected = {name for _, name in scored[:MAX_ADAPTIVE_TOOLS]}
    if selected:
        print(f"[Adaptive] Auto-resident tools: {', '.join(sorted(selected))}")
    return selected


def record_tool_call(data_dir: str, tool_name: str, session_id: int,
                     success: bool, tool_type: str = "builtin"):
    """Record a tool call for adaptive scoring + graduation tracking.

    tool_type is one of: "builtin", "auto_tool", "mcp".
    For auto_tool, the caller is responsible for graduation logic;
    this function only updates the shared usage counters.
    """
    usage = _load_tool_usage(data_dir)
    entry = usage.get(tool_name, {
        "type": tool_type,
        "calls": 0,
        "sessions": 0,
        "last_session": None,
        "last_used": None,
        "consecutive": 0,
        "graduated": False,
    })

    entry["calls"] += 1
    if success:
        entry["consecutive"] = entry.get("consecutive", 0) + 1
    else:
        entry["consecutive"] = 0

    prev_session = entry.get("last_session")
    if prev_session != session_id:
        entry["sessions"] = entry.get("sessions", 0) + 1
        entry["last_session"] = session_id

    entry["last_used"] = datetime.now().isoformat(timespec="minutes")
    usage[tool_name] = entry
    _save_tool_usage(data_dir, usage)
