"""Check task 233 state and wake timer."""
import sys, os, sqlite3
sys.path.insert(0, os.path.dirname(__file__))
from api.db import DB_PATH

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

# 1. Task info
task = conn.execute("SELECT * FROM tasks WHERE id=233").fetchone()
if task:
    print("=== TASK 233 ===")
    d = dict(task)
    for k, v in d.items():
        print(f"  {k}: {v}")
else:
    print("Task 233 not found!")
    # Find the most recent tasks
    tasks = conn.execute("SELECT id, status, task_type, updated_at FROM tasks ORDER BY id DESC LIMIT 10").fetchall()
    print("\n=== Recent tasks ===")
    for t in tasks:
        print(f"  #{t['id']}: {t['status']} ({t['task_type']}) updated={t['updated_at']}")
    conn.close()
    sys.exit(0)

# 2. Check wake_at
print(f"\n  wake_at set: {task['wake_at']} (type: {type(task['wake_at']).__name__})")

# 3. Check model call logs for this task
logs = conn.execute(
    "SELECT id, provider, model, prompt_tokens, completion_tokens, timestamp, "
    "cache_hit, latency_ms FROM model_call_logs WHERE task_id=233 ORDER BY id"
).fetchall()
print(f"\n=== Model call logs ({len(logs)} entries) ===")
for log in logs:
    print(f"  #{log['id']}: {log['provider']}/{log['model']} | {log['prompt_tokens']}+{log['completion_tokens']}t | {log['timestamp']} | cache={log['cache_hit']} | {log['latency_ms']}ms")

# 4. Check task_steps
steps = conn.execute(
    "SELECT id, step_number, tool_name, created_at FROM task_steps WHERE task_id=233 ORDER BY id"
).fetchall()
print(f"\n=== Task steps ({len(steps)}) ===")
for s in steps[-10:]:
    print(f"  #{s['id']} step={s['step_number']}: {s['tool_name']} at {s['created_at']}")

# 5. Check if BgMonitor would fire
if task['wake_at']:
    from datetime import datetime, timezone
    try:
        wake_dt = datetime.strptime(task['wake_at'], '%Y-%m-%d %H:%M:%S')
        wake_dt = wake_dt.replace(tzinfo=timezone.utc)
        now_utc = datetime.now(timezone.utc)
        diff = (now_utc - wake_dt).total_seconds()
        print(f"\n  wake_at: {task['wake_at']}")
        print(f"  now_utc: {now_utc.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  diff: {diff:.0f}s ({diff/60:.1f}min)")
        if diff > 0:
            print(f"  >>> WAKE TIME HAS PASSED! BgMonitor should have fired <<<")
        else:
            print(f"  Wake time not yet reached (in {-diff:.0f}s)")
    except Exception as e:
        print(f"\n  wake_at parse error: {e}")

conn.close()
