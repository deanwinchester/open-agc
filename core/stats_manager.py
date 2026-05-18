import sqlite3
import os
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

class StatsManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _get_conn(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        try:
            with self._get_conn() as conn:
                conn.execute('''
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
                conn.commit()
        except Exception as e:
            print(f"[StatsManager] Init error: {e}")

    def record_usage(self, provider: str, model: str, prompt_tokens: int, completion_tokens: int, 
                     session_id: Optional[int] = None, task_id: Optional[int] = None):
        """Record token usage for a single request."""
        total_tokens = prompt_tokens + completion_tokens
        # Simple cost estimation: $0.01 per 1k tokens as a very rough placeholder
        # Real logic should use a rate dictionary
        cost_estimate = (total_tokens / 1000.0) * 0.01 
        
        try:
            with self._get_conn() as conn:
                conn.execute('''
                    INSERT INTO token_usage (session_id, task_id, provider, model, prompt_tokens, completion_tokens, total_tokens, cost_estimate)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (session_id, task_id, provider, model, prompt_tokens, completion_tokens, total_tokens, cost_estimate))
        except Exception as e:
            print(f"[StatsManager] Error recording usage: {e}")

    def get_usage_history(self, provider: str, days: int = 30) -> List[Dict[str, Any]]:
        """Get daily usage stats for the last N days."""
        start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
        
        query = '''
            SELECT date(timestamp) as day, 
                   SUM(prompt_tokens) as prompt, 
                   SUM(completion_tokens) as completion,
                   SUM(total_tokens) as total,
                   SUM(cost_estimate) as cost
            FROM token_usage
            WHERE provider = ? AND timestamp >= ?
            GROUP BY day
            ORDER BY day ASC
        '''
        
        results = []
        try:
            with self._get_conn() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(query, (provider, start_date))
                for row in cursor:
                    results.append(dict(row))
        except Exception as e:
            print(f"[StatsManager] Error fetching usage history: {e}")
            
        return results

    def get_task_usage(self, task_id: int) -> Dict[str, int]:
        """Get cumulative usage for a specific task."""
        query = '''
            SELECT SUM(prompt_tokens) as prompt, 
                   SUM(completion_tokens) as completion,
                   SUM(total_tokens) as total,
                   SUM(cost_estimate) as cost
            FROM token_usage
            WHERE task_id = ?
        '''
        try:
            with self._get_conn() as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(query, (task_id,)).fetchone()
                if row:
                    return dict(row)
        except Exception as e:
            print(f"[StatsManager] Error fetching task usage: {e}")
            
        return {"prompt": 0, "completion": 0, "total": 0}

# Global singleton will be initialized in api/server.py
_stats_manager = None

def get_stats_manager(db_path: Optional[str] = None) -> StatsManager:
    global _stats_manager
    if _stats_manager is None:
        if db_path is None:
            # Try to find default path if not provided
            from core.paths import get_data_path
            db_path = get_data_path("memory.db")
        _stats_manager = StatsManager(db_path)
    return _stats_manager
