"""Database initialization and migration for open-agc-train plugin.

Uses an independent SQLite database (training.db) separate from
the main Open-AGC chat_history.db.
"""
import sqlite3
import os


SCHEMA = """
CREATE TABLE IF NOT EXISTS model_configs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    architecture    TEXT NOT NULL,
    config_json     TEXT NOT NULL,
    param_count_estimate INTEGER DEFAULT 0,
    is_custom       INTEGER DEFAULT 1,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS datasets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    source          TEXT DEFAULT 'manual',
    source_path     TEXT,
    storage_path    TEXT NOT NULL,
    format          TEXT DEFAULT 'jsonl',
    sample_count    INTEGER DEFAULT 0,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS training_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    model_config_id     INTEGER,
    dataset_id          INTEGER,
    base_model_id       TEXT NOT NULL DEFAULT '',
    base_model_source   TEXT DEFAULT 'huggingface',
    training_params_json TEXT NOT NULL DEFAULT '{}',
    status              TEXT DEFAULT 'pending',
    checkpoint_dir      TEXT,
    current_epoch       REAL DEFAULT 0,
    current_step        INTEGER DEFAULT 0,
    total_steps         INTEGER DEFAULT 0,
    best_loss           REAL,
    total_time_seconds  REAL DEFAULT 0,
    error_message       TEXT,
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS training_metrics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL,
    epoch           INTEGER,
    step            INTEGER,
    global_step     INTEGER,
    loss            REAL,
    grad_norm       REAL,
    learning_rate   REAL,
    act_mean        REAL DEFAULT 0,
    act_std         REAL DEFAULT 0,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES training_runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS benchmark_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id        TEXT NOT NULL,
    model_source    TEXT DEFAULT 'online',
    benchmark_type  TEXT NOT NULL,
    metrics_json    TEXT,
    num_questions   INTEGER DEFAULT 0,
    avg_latency_ms  REAL DEFAULT 0,
    tokens_per_second REAL DEFAULT 0,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS downloads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    type            TEXT DEFAULT 'model',
    label           TEXT,
    repo_id         TEXT,
    filename        TEXT,
    source          TEXT DEFAULT 'huggingface',
    url             TEXT,
    target_path     TEXT,
    partial_path    TEXT,
    total_size      INTEGER DEFAULT 0,
    downloaded_bytes INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'downloading',
    progress        REAL DEFAULT 0.0,
    error_message   TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


def init_db(db_path: str) -> sqlite3.Connection:
    """Initialize the training database. Creates tables if they don't exist."""
    db_dir = os.path.dirname(db_path)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def migrate_from(legacy_db_path: str, new_db_path: str) -> dict:
    """Copy training data from legacy chat_history.db into the new training.db.

    Returns counts of migrated rows per table.
    """
    if not os.path.exists(legacy_db_path):
        return {"error": f"Legacy DB not found: {legacy_db_path}"}

    new_conn = init_db(new_db_path)
    legacy_conn = sqlite3.connect(legacy_db_path)
    legacy_conn.row_factory = sqlite3.Row

    counts = {}
    tables = ["model_configs", "datasets", "training_runs",
              "training_metrics", "benchmark_results", "downloads"]

    for table in tables:
        try:
            rows = legacy_conn.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                counts[table] = 0
                continue
            columns = [c[0] for c in legacy_conn.execute(
                f"PRAGMA table_info({table})").fetchall()]
            placeholders = ",".join(["?"] * len(columns))
            col_names = ",".join(columns)
            for row in rows:
                values = [row[c] for c in columns]
                try:
                    new_conn.execute(
                        f"INSERT OR IGNORE INTO {table} ({col_names}) VALUES ({placeholders})",
                        values)
                except Exception:
                    pass
            new_conn.commit()
            counts[table] = len(rows)
        except Exception as e:
            counts[table] = f"error: {e}"

    legacy_conn.close()
    new_conn.close()
    return counts
