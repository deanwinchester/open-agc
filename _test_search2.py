# -*- coding: utf-8 -*-
import sqlite3, json, sys, os
sys.path.insert(0, r'D:\GitHub\open-agc')
from core.paths import get_data_path

db = sqlite3.connect(get_data_path('chat_history.db'))
db.row_factory = sqlite3.Row

# Check schema
cols = db.execute("PRAGMA table_info(messages)").fetchall()
print('=== messages table columns ===')
for c in cols:
    print(f'  {c["name"]}: {c["type"]}')

# Check message 2499
msg = db.execute("SELECT * FROM messages WHERE id=2499").fetchone()
if msg:
    d = dict(msg)
    print(f'\n=== Message 2499 ===')
    for k, v in d.items():
        try:
            print(f'  {k}: {str(v)[:100]}')
        except UnicodeEncodeError:
            print(f'  {k}: (contains emoji, {len(str(v))} chars)')
    content = d.get('content') or d.get('message_json') or ''
    content = str(content)
    print(f'\nContent length: {len(content)}')

    # Check for "谁是卷王"
    if '谁是卷王' in content:
        print('\n✅ "谁是卷王" FOUND')
        pos = content.index('谁是卷王')
        ctx_start = max(0, pos - 150)
        ctx_end = min(len(content), pos + 350)
        preview = ''
        if ctx_start > 0: preview += '...'
        preview += content[ctx_start:ctx_end]
        if ctx_end < len(content): preview += '...'
        print(preview)
    else:
        print('\n❌ "谁是卷王" NOT in content')
else:
    print('\nMessage 2499 not found')

# Search messages for "谁是卷王"
q = "谁是卷王"
matches = db.execute(
    "SELECT id, role FROM messages WHERE content LIKE ? ORDER BY id DESC LIMIT 10",
    ('%' + q + '%',)
).fetchall()
print(f'\nMessages containing "谁是卷王": {len(matches)}')
for m in matches:
    print(f'  #{m["id"]} ({m["role"]})')

db.close()
