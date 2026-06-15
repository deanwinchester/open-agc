# -*- coding: utf-8 -*-
"""Full simulation of search_history(query='谁是卷王') as the tool would run."""
import sqlite3, sys, os, re, json, time as _time
sys.path.insert(0, r'D:\GitHub\open-agc')
from core.paths import get_data_path

QUERY = "谁是卷王"
SESSION_ID = 1
MAX_RESULTS = 8
PAGE = 1
q_lower = QUERY.lower()
q_words = set(q_lower.split())
scored = []
_search_now = _time.time()

def fmt_ts(ts_val):
    if ts_val and ts_val > 1000000000:
        age = _search_now - ts_val
        if age < 60: return "(%ds)" % int(age)
        if age < 3600: return "(%dm)" % int(age//60)
        if age < 86400: return "(%dh)" % int(age//3600)
        return "(%dd)" % int(age//86400)
    return ""

def msg_ts(msg, idx):
    ts = msg.get("_timestamp")
    if ts: return ts
    return idx * 0.001

# ── Path 3: Messages table ──
sys.stdout.buffer.write(b"=== Path 3: Messages table ===\n")
db = sqlite3.connect(get_data_path("chat_history.db"))
db.row_factory = sqlite3.Row
rows = db.execute(
    "SELECT id, role, content, timestamp as created_at FROM messages WHERE session_id=? ORDER BY id ASC",
    (SESSION_ID,)
).fetchall()

msg_added = 0
for mr in rows:
    role = mr["role"]
    if role not in ("user", "agent"):
        continue
    content = str(mr["content"] or "")
    if not content: continue
    cl = content.lower()
    mc = sum(1 for w in q_words if w in cl)
    if mc == 0: continue
    msg_added += 1
    mid = mr["id"]
    ts_str = str(mr["created_at"] or "")
    try: ts_val = _time.mktime(_time.strptime(ts_str, '%Y-%m-%d %H:%M:%S'))
    except: ts_val = mid
    tag = fmt_ts(ts_val)
    # Context around first match
    first_pos = len(content)
    for w in q_words:
        p = content.lower().find(w)
        if p >= 0 and p < first_pos: first_pos = p
    ctx_start = max(0, first_pos - 150)
    ctx_end = min(len(content), first_pos + 350)
    preview = ""
    if ctx_start > 0: preview += "..."
    preview += content[ctx_start:ctx_end]
    if ctx_end < len(content): preview += "..."
    label = "用户" if role == "user" else "Agent"
    score = 5 + mc
    s = "[%s消息 msg:%d%s] %s" % (label, mid, tag, preview)
    scored.append((score, ts_val, s))

sys.stdout.buffer.write(("Messages added: %d\n" % msg_added).encode('utf-8'))

# ── Sort, page, display ──
def _noop(x): pass
scored.sort(key=lambda x: -x[0])
top = scored[:MAX_RESULTS * 3]
top.sort(key=lambda x: -x[1])

total = len(top)
per_page = MAX_RESULTS
start = (PAGE - 1) * per_page
end = start + per_page
page_items = top[start:end]

lines = ["会话记忆检索结果 (第%d页，共%d条，关键词: '%s'):" % (PAGE, total, QUERY)]
lines.append("提示：用 expand_id 查看详情，page=N 翻页。")
for idx, (score, ts, text) in enumerate(page_items, start + 1):
    lines.append("  #%d %s" % (idx, text))
if end < total:
    lines.append("  ... 还有 %d 条" % (total - end))

output = "\n".join(lines)
sys.stdout.buffer.write(b"\n")
sys.stdout.buffer.write(output.encode('utf-8'))
sys.stdout.buffer.write(b"\n\n")

# ── Check if msg 2499 is in the paged results ──
msg2499_in_results = any("msg:2499" in text for _, _, text in page_items)
sys.stdout.buffer.write(("msg 2499 in page %d results: %s\n" % (PAGE, msg2499_in_results)).encode('utf-8'))

# Find which page msg 2499 would be on
if msg_added > 0:
    for idx, (score, ts, text) in enumerate(top):
        if "msg:2499" in text:
            page_num = idx // per_page + 1
            position_on_page = idx % per_page + 1
            sys.stdout.buffer.write(("msg 2499 ranked at position %d (page %d, #%d on page)\n" % (idx+1, page_num, position_on_page)).encode('utf-8'))
            break
    else:
        sys.stdout.buffer.write(b"msg 2499 not found in scored results!\n")

db.close()
