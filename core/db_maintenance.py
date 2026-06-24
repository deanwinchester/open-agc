"""
Database maintenance utilities — cleanup, vacuum, and retention policies.

Run on server startup and periodically to keep the database size in check.
"""
import os
import time
import sqlite3
import shutil
from datetime import datetime, timedelta

from core.paths import get_data_path


def cleanup_model_logs(days: int = 30, min_cost: float = 0.0, dry_run: bool = False) -> dict:
    """Delete model_call_logs entries older than `days` days.

    Also removes the corresponding on-disk request/response JSON files.

    Args:
        days: Retention period in days (default 30).
        min_cost: Only delete entries with cost_estimate < this value.
        dry_run: If True, only report what would be deleted.

    Returns:
        Dict with keys: deleted_rows, freed_bytes, deleted_files.
    """
    db_path = get_data_path("chat_history.db")
    if not os.path.exists(db_path):
        return {"deleted_rows": 0, "freed_bytes": 0, "deleted_files": 0}

    conn = sqlite3.connect(db_path, timeout=5)
    cursor = conn.cursor()

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    # Find entries to delete
    cursor.execute(
        """SELECT id, response_data FROM model_call_logs
           WHERE timestamp < ? AND cost_estimate < ?""",
        (cutoff, min_cost)
    )
    entries = cursor.fetchall()
    if not entries:
        conn.close()
        return {"deleted_rows": 0, "freed_bytes": 0, "deleted_files": 0}

    ids_to_delete = [row[0] for row in entries]

    if dry_run:
        conn.close()
        return {"deleted_rows": len(ids_to_delete), "freed_bytes": 0, "deleted_files": 0}

    # Delete database rows
    placeholders = ",".join("?" for _ in ids_to_delete)
    cursor.execute(f"DELETE FROM model_call_logs WHERE id IN ({placeholders})", ids_to_delete)
    deleted_rows = cursor.rowcount
    conn.commit()

    # Clean up corresponding disk files
    deleted_files = 0
    for row in entries:
        response_data = row[1] or ""
        if "|" in response_data:
            parts = response_data.split("|")
            for path in parts:
                path = path.strip()
                if os.path.isfile(path):
                    try:
                        os.remove(path)
                        deleted_files += 1
                    except Exception:
                        pass

    conn.close()
    return {"deleted_rows": deleted_rows, "freed_bytes": 0, "deleted_files": deleted_files}


def vacuum_database() -> dict:
    """Run VACUUM to reclaim unused space. Returns dict with bytes_freed."""
    db_path = get_data_path("chat_history.db")
    if not os.path.exists(db_path):
        return {"bytes_freed": 0}

    before = os.path.getsize(db_path)

    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
    conn.execute("PRAGMA incremental_vacuum(1000)")
    conn.execute("VACUUM")
    conn.close()

    after = os.path.getsize(db_path)
    freed = max(0, before - after)
    return {"bytes_freed": freed}


def cleanup_stale_kg_data(days: int = 90) -> dict:
    """Delete stale knowledge graph entities and reflections older than `days` days."""
    db_path = get_data_path("agent.db")
    if not os.path.exists(db_path):
        return {"deleted_entities": 0, "deleted_relations": 0, "deleted_reflections": 0}

    conn = sqlite3.connect(db_path, timeout=5)
    cursor = conn.cursor()

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    result = {}

    try:
        cursor.execute("DELETE FROM kg_entities WHERE last_seen < ?", (cutoff,))
        result["deleted_entities"] = cursor.rowcount
    except Exception:
        result["deleted_entities"] = 0

    try:
        cursor.execute("DELETE FROM kg_relations WHERE last_seen < ?", (cutoff,))
        result["deleted_relations"] = cursor.rowcount
    except Exception:
        result["deleted_relations"] = 0

    try:
        cursor.execute("DELETE FROM reflections WHERE created_at < ?", (cutoff,))
        result["deleted_reflections"] = cursor.rowcount
    except Exception:
        result["deleted_reflections"] = 0

    conn.commit()
    conn.close()
    return result


def cleanup_old_data(days: int = 30, min_cost: float = 0.0) -> dict:
    """Run all cleanup tasks and return a summary.

    Call this on server startup or via a periodic timer.
    """
    results = {}

    # 1. Model call logs
    log_result = cleanup_model_logs(days=days, min_cost=min_cost)
    results["model_logs"] = log_result
    if log_result["deleted_rows"] > 0:
        print(f"[DB] Cleaned {log_result['deleted_rows']} old model call logs")

    # 2. Vacuum
    try:
        vac_result = vacuum_database()
        results["vacuum"] = vac_result
        if vac_result["bytes_freed"] > 0:
            mb = vac_result["bytes_freed"] / 1024 / 1024
            print(f"[DB] Vacuum freed {mb:.1f} MB")
    except Exception as e:
        print(f"[DB] Vacuum failed: {e}")

    # 3. Stale KG / reflections cleanup (agent.db)
    try:
        kg_result = cleanup_stale_kg_data(days=90)
        results["kg_cleanup"] = kg_result
        total_kg = kg_result.get("deleted_entities", 0) + kg_result.get("deleted_relations", 0) + kg_result.get("deleted_reflections", 0)
        if total_kg > 0:
            print(f"[DB] Cleaned {total_kg} stale KG/reflection entries")
    except Exception as e:
        print(f"[DB] KG cleanup error: {e}")

    return results
