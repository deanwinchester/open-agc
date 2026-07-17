"""Database initialization and schema management for Open-AGC."""
import os
import sqlite3
from core.paths import get_data_path

DB_PATH = get_data_path("chat_history.db")


def db_connect():
    """Open a connection to the main DB with busy_timeout and Row factory.

    Use this instead of bare ``sqlite3.connect(DB_PATH)`` so that writers
    wait on locks (up to 5s) instead of failing with "database is locked".
    ``sqlite3.Row`` supports both index (``row[0]``) and name access.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create all tables and run schema migrations."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    cursor = conn.cursor()

    # Sessions table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            session_id INTEGER DEFAULT 1,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            user_query TEXT NOT NULL,
            status TEXT DEFAULT 'running',
            task_type TEXT DEFAULT 'oneshot',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            result_summary TEXT,
            output_files TEXT DEFAULT '[]',
            total_tokens INTEGER DEFAULT 0,
            total_cost REAL DEFAULT 0.0,
            schedule_cron TEXT,
            schedule_enabled INTEGER DEFAULT 0,
            next_run_at DATETIME,
            last_run_at DATETIME,
            run_count INTEGER DEFAULT 0,
            max_resume_count INTEGER DEFAULT 10,
            resume_count INTEGER DEFAULT 0,
            context_snapshot TEXT,
            interruption_reason TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS task_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            step_number INTEGER NOT NULL,
            tool_name TEXT NOT NULL,
            tool_label TEXT,
            args_preview TEXT,
            result_preview TEXT,
            full_result TEXT,
            success INTEGER DEFAULT 1,
            thinking_content TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL DEFAULT 'model',
            label TEXT NOT NULL DEFAULT '',
            repo_id TEXT,
            filename TEXT,
            source TEXT DEFAULT 'huggingface',
            url TEXT,
            target_path TEXT NOT NULL DEFAULT '',
            partial_path TEXT NOT NULL DEFAULT '',
            total_size INTEGER DEFAULT 0,
            downloaded_bytes INTEGER DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'downloading',
            progress REAL DEFAULT 0.0,
            error_message TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS download_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            download_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            message TEXT DEFAULT '',
            details TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (download_id) REFERENCES downloads(id) ON DELETE CASCADE
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS benchmark_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_id TEXT NOT NULL,
            model_source TEXT DEFAULT 'online',
            benchmark_type TEXT NOT NULL,
            metrics_json TEXT NOT NULL DEFAULT '{}',
            num_questions INTEGER DEFAULT 0,
            avg_latency_ms REAL DEFAULT 0,
            tokens_per_second REAL DEFAULT 0,
            status TEXT DEFAULT 'completed',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            session_id INTEGER,
            task_id INTEGER,
            provider TEXT,
            model TEXT,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            cost_estimate REAL DEFAULT 0.0
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS model_call_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            session_id INTEGER,
            task_id INTEGER,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            request_data TEXT,
            response_data TEXT,
            cache_hit TEXT DEFAULT 'unknown',
            latency_ms INTEGER DEFAULT 0,
            cost_estimate REAL DEFAULT 0.0
        )
    ''')

    # Ensure datasets storage directory exists
    datasets_dir = os.path.join(os.path.dirname(DB_PATH), "datasets")
    os.makedirs(datasets_dir, exist_ok=True)

    # ── Migrations ──
    _run_migrations(cursor)
    conn.commit()
    conn.close()
    create_indexes()


def _run_migrations(cursor):
    """Run all schema migrations (ALTER TABLE ADD COLUMN, etc.)."""
    from datetime import datetime

    # session_id to messages
    try:
        cursor.execute("ALTER TABLE messages ADD COLUMN session_id INTEGER DEFAULT 1")
    except Exception:
        pass

    # Ensure at least one default session exists
    cursor.execute("SELECT COUNT(*) FROM sessions")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO sessions (name) VALUES (?)", ("默认会话",))

    # Per-table migration lists: each (col_name, col_def) targets ONE specific table.
    # This prevents every table from accidentally inheriting columns from every other table.
    table_migrations: dict = {
        "tasks": [
            ("category", "TEXT DEFAULT 'model'"),
            ("task_type", "TEXT DEFAULT 'oneshot'"),
            ("schedule_cron", "TEXT"),
            ("schedule_enabled", "INTEGER DEFAULT 0"),
            ("next_run_at", "DATETIME"),
            ("last_run_at", "DATETIME"),
            ("run_count", "INTEGER DEFAULT 0"),
            ("max_resume_count", "INTEGER DEFAULT 10"),
            ("resume_count", "INTEGER DEFAULT 0"),
            ("context_snapshot", "TEXT"),
            ("total_tokens", "INTEGER DEFAULT 0"),
            ("total_cost", "REAL DEFAULT 0.0"),
            ("interruption_reason", "TEXT"),
            ("session_id", "INTEGER DEFAULT 1"),
            ("prompt_tokens", "INTEGER DEFAULT 0"),
            ("completion_tokens", "INTEGER DEFAULT 0"),
            ("cached_tokens", "INTEGER DEFAULT 0"),
            ("plan_id", "TEXT DEFAULT ''"),
            ("task_goal", "TEXT DEFAULT ''"),
        ],
        "sessions": [
            ("session_id", "INTEGER DEFAULT 1"),
            ("email_enabled", "INTEGER DEFAULT 0"),
            ("email_account", "TEXT DEFAULT ''"),
            ("email_password", "TEXT DEFAULT ''"),
            ("email_imap_server", "TEXT DEFAULT ''"),
            ("email_smtp_server", "TEXT DEFAULT ''"),
            ("owner_email", "TEXT DEFAULT ''"),
        ],
        "task_steps": [
            ("session_id", "INTEGER DEFAULT 1"),
            ("tool_call_id", "TEXT"),
            ("full_args", "TEXT"),
            ("generated_files", "TEXT DEFAULT ''"),
        ],
        "downloads": [
            ("task_id", "INTEGER"),
            ("background_resumed", "INTEGER DEFAULT 0"),
        ],
        "model_call_logs": [
            ("cached_tokens", "INTEGER DEFAULT 0"),
        ],
    }

    for table, cols in table_migrations.items():
        for col, dtype in cols:
            try:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col} {dtype}")
            except Exception:
                pass  # Column already exists

    # Specific migrations
    try:
        cursor.execute("UPDATE tasks SET task_type='goal_resume' WHERE task_type='todo_resume'")
    except Exception:
        pass

    # Ensure wake_at exists on tasks (not just task_steps)
    try:
        cursor.execute("ALTER TABLE tasks ADD COLUMN wake_at DATETIME")
    except Exception:
        pass  # Already exists

    # Add task_id to messages for bidirectional chat-task binding
    try:
        cursor.execute("ALTER TABLE messages ADD COLUMN task_id INTEGER")
    except Exception:
        pass  # Already exists


def create_indexes():
    """Create indexes for query performance."""
    conn = db_connect()
    cursor = conn.cursor()
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_task_steps_task_id ON task_steps(task_id)",
        "CREATE INDEX IF NOT EXISTS idx_task_steps_session_id ON task_steps(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_session_id ON tasks(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_task_type_status ON tasks(task_type, status)",
        "CREATE INDEX IF NOT EXISTS idx_downloads_task_id ON downloads(task_id)",
        "CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status)",
        "CREATE INDEX IF NOT EXISTS idx_downloads_type ON downloads(type)",
        "CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_task_steps_created ON task_steps(created_at)",
    ]
    for sql in indexes:
        try:
            cursor.execute(sql)
        except Exception as e:
            print(f"[DB] Index error: {e}")
    conn.commit()
    conn.close()
    print(f"[DB] Created {len(indexes)} indexes")
