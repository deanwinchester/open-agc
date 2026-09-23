"""
Context compression and compaction utilities for OpenAGCAgent.

Extracted from agent/agent.py to reduce the 2649-line monolith.
All methods were originally @staticmethod or used only self.llm.
"""
import re
import json
from typing import List, Dict


def compress_search_results(result: str) -> str:
    """Compress search results: keep all entries, truncate snippets per-entry.

    Search results are structured as:
      [From Engine]
      1. Title
         URL: xxx
         Snippet: yyy
      2. ...
    """
    MAX_ENTRY_CHARS = 400
    MAX_TOTAL = 4000

    lines = result.split("\n")
    compressed = []
    current_entry = []

    for line in lines:
        if re.match(r'^\d+\.\s', line):
            if current_entry:
                entry_text = "\n".join(current_entry)
                if len(entry_text) > MAX_ENTRY_CHARS:
                    entry_text = entry_text[:MAX_ENTRY_CHARS] + "..."
                compressed.append(entry_text)
            current_entry = [line]
        else:
            current_entry.append(line)

    if current_entry:
        entry_text = "\n".join(current_entry)
        if len(entry_text) > MAX_ENTRY_CHARS:
            entry_text = entry_text[:MAX_ENTRY_CHARS] + "..."
        compressed.append(entry_text)

    result_text = "\n".join(compressed)
    if len(result_text) > MAX_TOTAL:
        result_text = result_text[:MAX_TOTAL] + "\n...(truncated)"

    return result_text


def compress_file_content(result: str) -> str:
    """Compress file content: keep head + tail with omitted-line annotation."""
    HEAD_LINES = 30
    TAIL_LINES = 20
    lines = result.split("\n")
    if len(lines) <= HEAD_LINES + TAIL_LINES + 5:
        return result
    head = lines[:HEAD_LINES]
    tail = lines[-TAIL_LINES:]
    omitted = len(lines) - HEAD_LINES - TAIL_LINES
    return "\n".join(head + [f"─── {omitted} lines omitted ───"] + tail)


def compress_shell_output(result: str, tool_name: str) -> str:
    """Compress shell/Python output: keep head + scored middle lines + tail."""
    COMPRESS_THRESHOLD = 3000
    EXTRACTIVE_TARGET = 8000

    if len(result) <= COMPRESS_THRESHOLD:
        return result

    lines = result.split("\n")

    def _line_score(line: str) -> int:
        low = line.lower()
        score = 0
        if any(kw in low for kw in ("error", "exception", "traceback", "fail",
               "trace ", "fatal", "warning", "cannot", "not found", "denied",
               "unexpected", "syntaxerror", "command not found")):
            score += 5
        if any(kw in low for kw in ("exit code", "returncode", "status", "result")):
            score += 3
        if any(kw in low for kw in ("file", "path", "dir", "found", "missing")):
            score += 2
        if any(c.isdigit() for c in line):
            score += 1
        if len(line) > 300:
            score -= 2
        return score

    head = lines[:15]
    tail = lines[-5:]
    middle = lines[15:-5] if len(lines) > 20 else []

    if not middle:
        compressed = "\n".join(head + tail)
        return (f"[Compressed: {len(result)} chars → {len(compressed)} chars | "
                f"original tool: {tool_name}]\n{compressed}")

    scored_lines = [(i, _line_score(l), l) for i, l in enumerate(middle, start=15)]
    scored_lines.sort(key=lambda x: -x[1])

    important = [(i, l) for i, s, l in scored_lines if s >= 2]
    min_keep = max(1, len(middle) // 5)
    if len(important) < min_keep:
        extra = scored_lines[:min_keep - len(important)]
        existing_ids = {idx for idx, _ in important}
        for idx, s, l in extra:
            if idx not in existing_ids:
                important.append((idx, l))
                existing_ids.add(idx)

    section_lines = []
    existing_ids = {idx for idx, _ in important}
    for i, l in enumerate(middle):
        idx = i + 15
        if idx not in existing_ids and re.search(r'^[-|=+|]{5,}|^#{1,3}\s', l):
            section_lines.append((idx, l))
            existing_ids.add(idx)

    compressed_lines = list(head)
    if important or section_lines:
        compressed_lines.append(f"─── key output ({len(important)} important lines) ───")
        seen = set()
        for idx, l in sorted(important + section_lines):
            if l not in seen:
                compressed_lines.append(l)
                seen.add(l)
        omitted = len(lines) - len(head) - len(tail) - len(seen)
        if omitted > 0:
            compressed_lines.append(f"─── {omitted} lines omitted ───")

    compressed_lines.extend(tail)
    compressed = "\n".join(compressed_lines)

    if len(compressed) > EXTRACTIVE_TARGET:
        compressed = "\n".join(head + [f"─── {len(lines) - len(head) - len(tail)} lines omitted ───"] + tail)

    return (f"[Compressed: {len(result)} chars → {len(compressed)} chars | "
            f"original tool: {tool_name}]\n{compressed}")


def compress_json_result(result: str, max_total: int = 4000) -> str:
    """JSON 感知的结构化压缩：保留全部标量元信息，数组只留前 N 条且逐条裁字段。

    MCP 工具（如 es_search）返回的是 JSON，不是 web 搜索的「1. Title/URL/Snippet」
    文本格式——走错压缩器会把整个 JSON 当成单条条目裁到 400 字符，模型看到
    残片会误判「工具输出被截断」而绕过 MCP 直接用 Python 请求接口（生产实证）。
    非 JSON 输入返回 None，由调用方回退到其他压缩器。
    """
    text = result.strip()
    if not text or text[0] not in "{[":
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None

    MAX_ITEM_CHARS = 600   # 单条命中保留上限
    MAX_STR_FIELD = 300    # 字符串字段保留上限
    total_chars = len(result)

    def _trim(obj, depth=0):
        if isinstance(obj, str):
            return obj if len(obj) <= MAX_STR_FIELD else obj[:MAX_STR_FIELD] + "…"
        if isinstance(obj, dict):
            return {k: _trim(v, depth + 1) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_trim(v, depth + 1) for v in obj]
        return obj

    def _shrink_arrays(obj, keep, counter):
        """把遇到的数组裁到前 keep 条，被裁掉的条数累加进 counter。"""
        if isinstance(obj, dict):
            return {k: _shrink_arrays(v, keep, counter) for k, v in obj.items()}
        if isinstance(obj, list):
            if len(obj) > keep:
                counter[0] += len(obj) - keep
                obj = obj[:keep]
            return [_shrink_arrays(v, keep, counter) for v in obj]
        return obj

    note = (f"[JSON 结果已压缩：原始 {total_chars} 字符，数组仅保留前 "
            f"{{keep}} 条（省略 {{omitted}} 条），长字段逐条裁至 "
            f"{MAX_STR_FIELD} 字符。数据在服务端是完整的，这不是工具故障]")
    budget = max_total - 220  # 给 note 与换行预留，保证总长 ≤ max_total

    trimmed = _trim(data)
    omitted_total = 0
    keep = 0
    out = ""
    # 逐级收紧数组保留条数，直到塞进预算
    for keep in (10, 5, 3, 1):
        counter = [0]
        candidate = _shrink_arrays(trimmed, keep, counter)
        out = json.dumps(candidate, ensure_ascii=False, indent=1)
        omitted_total = counter[0]
        if len(out) <= budget:
            break

    return note.format(keep=keep, omitted=omitted_total) + "\n" + out


def compress_tool_result(result: str, tool_name: str, max_total: int = 4000) -> str:
    """Route tool result compression based on tool name."""
    # JSON 优先：MCP 工具名五花八门（es_search/xx_query），按名字路由会
    # 误判格式；能解析成 JSON 的一律走结构化压缩。
    json_compressed = compress_json_result(result, max_total)
    if json_compressed is not None:
        return json_compressed
    if "search" in tool_name:
        return compress_search_results(result)
    if "read_file" in tool_name:
        return compress_file_content(result)
    return compress_shell_output(result, tool_name)


# ── Tool call folding ──

FOLD_AFTER_N = 30
KEEP_LAST_N = 20


def fold_tool_calls(messages: List[Dict], force: bool = False) -> List[Dict]:
    """Fold older tool-call rounds into a summary to save context space.

    When the number of consecutive tool rounds exceeds FOLD_AFTER_N (30),
    the earliest rounds are replaced with a compact execution summary.
    Keeps the last KEEP_LAST_N (20) rounds intact.

    Returns the folded messages list.
    """
    if not messages:
        return messages

    # A round = assistant(tool_calls) followed by zero or more tool results
    # Group consecutive tool messages into rounds
    rounds = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role", "")
        if role == "assistant" and msg.get("tool_calls"):
            round_start = i
            i += 1
            # Consume following tool messages for this round
            while i < len(messages) and messages[i].get("role") == "tool":
                i += 1
            rounds.append((round_start, i))
        else:
            i += 1

    if len(rounds) <= FOLD_AFTER_N and not force:
        return messages

    # Keep last N rounds intact
    keep = rounds[-KEEP_LAST_N:] if len(rounds) > KEEP_LAST_N else rounds
    fold = rounds[:-KEEP_LAST_N] if len(rounds) > KEEP_LAST_N else []

    if not fold:
        return messages

    # Build summary for folded rounds
    summary_lines = [f"📋 已完成步骤 (共 {len(fold)} 步):"]
    for idx, (start, end) in enumerate(fold):
        asst = messages[start]
        tcs = asst.get("tool_calls", [])
        for tc in tcs:
            fn = tc.get("function", {})
            name = fn.get("name", "?")
            args_str = json.dumps(fn.get("arguments", ""))[:80]
            # Check tool results for errors
            has_error = ""
            for t_idx in range(start + 1, end):
                t_msg = messages[t_idx]
                t_content = str(t_msg.get("content", ""))[:100].lower()
                if any(kw in t_content for kw in ("error", "traceback", "exception", "fail")):
                    has_error = " ⚠️"
                    break
            summary_lines.append(f"  {idx+1}. {name}({args_str}){has_error}")

    summary_text = "\n".join(summary_lines)

    # Build new messages: non-round messages + kept rounds + summary
    result = []
    # Include all messages before the first folded round
    if fold:
        result = messages[:fold[0][0]]
        # Add summary
        result.append({"role": "assistant", "content": summary_text})
        # Add kept rounds
        for start, end in keep:
            result.extend(messages[start:end])
        # Add messages after the last kept round
        last_end = keep[-1][1] if keep else fold[-1][1]
        result.extend(messages[last_end:])
    else:
        result = messages

    return result
