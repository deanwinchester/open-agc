# -*- coding: utf-8 -*-
"""Verify claim_task_for_resume: concurrent claims on the same task — exactly one wins.

Uses a temp SQLite DB and monkeypatches api.task_core.db_connect so the real
chat_history.db is untouched.
"""
import os
import sqlite3
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api.task_core as tc

tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp.close()
db_path = tmp.name

conn = sqlite3.connect(db_path)
conn.execute(
    "CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT, "
    "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
conn.execute("INSERT INTO tasks (id, status) VALUES (1, 'interrupted')")
conn.execute("INSERT INTO tasks (id, status) VALUES (2, 'backgrounded')")
conn.commit()
conn.close()


def test_connect():
    c = sqlite3.connect(db_path, timeout=10)
    c.execute("PRAGMA busy_timeout=5000")
    c.row_factory = sqlite3.Row
    return c


tc.db_connect = test_connect  # monkeypatch

failures = []

# 1) Single claim on 'interrupted' succeeds, second fails (already running)
assert tc.claim_task_for_resume(1, ('interrupted',)) is True, "first claim should win"
assert tc.claim_task_for_resume(1, ('interrupted',)) is False, "second claim must lose"
print("[1] sequential double-claim: PASS")

# 2) Wrong status not claimable
assert tc.claim_task_for_resume(2, ('interrupted',)) is False, "backgrounded not in allowed"
assert tc.claim_task_for_resume(2, ('backgrounded',)) is True, "backgrounded claim should win"
print("[2] allowed_statuses filter: PASS")

# 3) Concurrent: reset task 1 to interrupted, N threads race — exactly one True
c = test_connect()
c.execute("UPDATE tasks SET status='interrupted' WHERE id=1")
c.commit()
c.close()

N = 8
results = []
barrier = threading.Barrier(N)


def racer():
    barrier.wait()  # maximize contention
    results.append(tc.claim_task_for_resume(1, ('interrupted',)))


threads = [threading.Thread(target=racer) for _ in range(N)]
for t in threads:
    t.start()
for t in threads:
    t.join()

wins = sum(1 for r in results if r)
assert wins == 1, f"expected exactly 1 winner, got {wins}"
print(f"[3] concurrent {N}-thread claim: PASS (winners={wins})")

# 4) Final state sane: task 1 is 'running'
c = test_connect()
st = c.execute("SELECT status FROM tasks WHERE id=1").fetchone()[0]
c.close()
assert st == 'running', f"final status should be running, got {st}"
print("[4] final status='running': PASS")

# 5) Nonexistent task: False, no exception
assert tc.claim_task_for_resume(999, ('interrupted',)) is False
print("[5] missing task: PASS")

os.unlink(db_path)
print("ALL CLAIM CAS TESTS PASSED")
