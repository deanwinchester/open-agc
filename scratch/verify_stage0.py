# -*- coding: utf-8 -*-
"""Stage-0 data-layer verification. Runs against a temp OPEN_AGC_DATA_DIR (never touches real data)."""
import json, os, shutil, sqlite3, sys, threading

data_dir = os.environ["OPEN_AGC_DATA_DIR"]
os.makedirs(os.path.join(data_dir, "data"), exist_ok=True)

# ── 1. init_db on a COPY of the real DB, run twice (create_indexes idempotent) ──
real_db = os.path.join("data", "chat_history.db")
test_db = os.path.join(data_dir, "data", "chat_history.db")
copied = False
if os.path.exists(real_db):
    shutil.copy2(real_db, test_db)
    copied = True

from api.db import init_db, db_connect, DB_PATH
assert os.path.abspath(DB_PATH) == os.path.abspath(test_db), f"DB_PATH mismatch: {DB_PATH}"
init_db()
init_db()  # second run: migrations + create_indexes must be idempotent

conn = db_connect()
bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
assert int(bt) == 5000, f"busy_timeout={bt}"
row = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
assert row[0] >= 1  # index access on Row
idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
for name in ("idx_task_steps_task_id", "idx_tasks_status", "idx_messages_session_id",
             "idx_downloads_task_id", "idx_sessions_updated"):
    assert name in idx, f"missing index {name}"
conn.close()
print(f"[OK] init_db x2 on {'copied real' if copied else 'fresh'} DB; busy_timeout=5000; 12 indexes idempotent")

# ── 2. concurrent writers should not hit 'database is locked' ──
errors = []
def writer(tag):
    try:
        for i in range(30):
            c = db_connect()
            c.execute("INSERT INTO messages (role, content, session_id) VALUES ('user', ?, 1)", (f"{tag}-{i}",))
            c.commit()
            c.close()
    except Exception as e:
        errors.append(f"{tag}: {e}")
threads = [threading.Thread(target=writer, args=(f"t{n}",)) for n in range(4)]
[t.start() for t in threads]
[t.join() for t in threads]
assert not errors, errors
print("[OK] 4 threads x 30 concurrent inserts, no 'database is locked'")

# ── 3. save_config: 100 saves across threads, atomic, UTF-8, no tmp residue ──
from api import config as cfg
assert os.path.abspath(cfg.CONFIG_PATH).startswith(os.path.abspath(data_dir)), cfg.CONFIG_PATH

def saver(n):
    for i in range(25):
        c = cfg.load_config()
        c[f"k{n}"] = i
        c["中文键"] = "中文值"
        cfg.save_config(c)

ts = [threading.Thread(target=saver, args=(n,)) for n in range(4)]
[t.start() for t in ts]
[t.join() for t in ts]
final = cfg.load_config()
assert final["中文键"] == "中文值", "UTF-8 content corrupted"
assert not os.path.exists(cfg.CONFIG_PATH + ".tmp"), "tmp file left behind"
with open(cfg.CONFIG_PATH, "r", encoding="utf-8") as f:
    json.load(f)  # must be valid JSON after concurrent saves
print("[OK] save_config x100 (4 threads): valid JSON, UTF-8 intact, no .tmp residue")

# ── 4. corrupt config -> backup + warning, returns {} ──
with open(cfg.CONFIG_PATH, "w", encoding="utf-8") as f:
    f.write("{not json!")
res = cfg.load_config()
assert res == {}
backups = [p for p in os.listdir(os.path.dirname(cfg.CONFIG_PATH)) if "config.json.corrupt-" in p]
assert backups, "no corrupt backup created"
print(f"[OK] corrupt config.json backed up -> {backups[0]}, load_config returned {{}}")

# ── 5. task_plan: atomic writes, UTF-8 round-trip, corrupt goals.json raises + preserved ──
from tools import task_plan as tp
plan = {"plan_id": "verify0", "goal": "验证目标", "steps": [], "status": "doing"}
assert tp.save_plan(plan)
assert tp.load_plan(plan_id="verify0")["goal"] == "验证目标"
goals = {"items": [{"id": 1, "desc": "中文目标", "status": "pending", "task_ids": []}]}
assert tp.save_goals(goals)
assert tp.load_goals()["items"][0]["desc"] == "中文目标"
assert not os.path.exists(tp._get_goals_path() + ".tmp")

gpath = tp._get_goals_path()
with open(gpath, "w", encoding="utf-8") as f:
    f.write("{broken")
raised = False
try:
    tp.load_goals()
except RuntimeError:
    raised = True
assert raised, "load_goals did not raise on corrupt goals.json"
assert os.path.exists(gpath), "corrupt goals.json was removed!"
with open(gpath, "r", encoding="utf-8") as f:
    assert f.read() == "{broken", "corrupt goals.json content was modified"
print("[OK] task_plan atomic save/load UTF-8; corrupt goals.json raises RuntimeError and file preserved")

print("ALL STAGE-0 CHECKS PASSED")
