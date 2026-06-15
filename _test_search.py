# -*- coding: utf-8 -*-
import sqlite3, json, sys, os
sys.path.insert(0, r'D:\GitHub\open-agc')
from core.paths import get_data_path

db_path = get_data_path('chat_history.db')
db = sqlite3.connect(db_path)
db.row_factory = sqlite3.Row

# Check message 2499
msg = db.execute("SELECT * FROM messages WHERE id=2499").fetchone()
if msg:
    print('=== Message 2499 ===')
    print('Role:', msg['role'])
    print('Created:', msg['created_at'])
    print('Content preview:', (msg['content'] or '')[:500])
    print('Content length:', len(msg['content'] or ''))

    # Check if "谁是卷王" appears in it
    content = msg['content'] or ''
    if '谁是卷王' in content:
        print('\n✅ "谁是卷王" FOUND in message 2499')
        # Find position
        pos = content.index('谁是卷王')
        ctx_start = max(0, pos - 150)
        ctx_end = min(len(content), pos + 350)
        preview = ''
        if ctx_start > 0: preview += '...'
        preview += content[ctx_start:ctx_end]
        if ctx_end < len(content): preview += '...'
        print('Context around match:')
        print(preview)
    else:
        print('\n❌ "谁是卷王" NOT in message 2499')
else:
    print('Message 2499 not found')
    # Check range
    count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    print(f'Total messages: {count}')
    if count > 2499:
        near = db.execute("SELECT id, role, content[:80] FROM messages ORDER BY id LIMIT 1 OFFSET 2498").fetchone()
        print(f'Message at index 2499: id={near["id"]} role={near["role"]}')
    else:
        print(f'Only {count} messages, no id=2499')

# Also simulate what search_history would do: search messages for "谁是卷王"
print('\n=== Simulating search_history(query="谁是卷王") ===')
q = "谁是卷王"
q_lower = q.lower()
q_words = set(q_lower.split())

# Search messages table for "谁是卷王"
matches = db.execute(
    "SELECT id, role, content[:200] as preview FROM messages WHERE content LIKE ? ORDER BY id DESC LIMIT 20",
    ('%' + q + '%',)
).fetchall()
print(f'Messages containing "谁是卷王": {len(matches)}')
for m in matches:
    print(f'  #{m["id"]} ({m["role"]}): {m["preview"][:100]}')

# Check session_id for message 2499
if msg:
    # Find what session this message belongs to
    row = db.execute(
        "SELECT session_id FROM messages WHERE id=2499"
    ).fetchone()
    if row:
        print(f'\nMessage 2499 session_id: {row["session_id"]}')

db.close()
