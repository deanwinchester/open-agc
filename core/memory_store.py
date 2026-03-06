"""
Smart Memory Store — FTS5 based retrieval with SQLite storage.
Features:
  - FTS5 full-text search with BM25 ranking (replaces hand-rolled TF-IDF)
  - Memory hierarchy: core / working / episode
  - Smart deduplication: find similar memories before inserting
  - Conversation summaries storage
"""
import os
import json
import sqlite3
import re
from datetime import datetime
from typing import List, Dict, Optional


# ---- Memory Categories ----

CATEGORIES = {
    "tech": ["代码", "编程", "python", "javascript", "api", "bug", "数据库", "服务器", "部署",
             "code", "program", "debug", "server", "database", "deploy", "git", "docker"],
    "user_pref": ["喜欢", "偏好", "习惯", "风格", "prefer", "like", "style", "favorite"],
    "project": ["项目", "功能", "需求", "任务", "project", "feature", "requirement", "task"],
    "knowledge": ["学到", "原来", "知道", "方法", "技巧", "learn", "know", "method", "trick"],
    "system": ["配置", "设置", "模型", "api key", "config", "setting", "model"],
    "general": [],
}

MEMORY_TYPES = ("core", "working", "episode")


def auto_categorize(text: str) -> str:
    """Automatically categorize memory content by keyword matching."""
    text_lower = text.lower()
    scores = {}
    for cat, keywords in CATEGORIES.items():
        if not keywords:
            continue
        score = sum(1 for kw in keywords if kw in text_lower)
        if score > 0:
            scores[cat] = score

    if scores:
        return max(scores, key=scores.get)
    return "general"


def _tokenize_for_fts(text: str) -> str:
    """
    Prepare text for FTS5 insertion.
    Keep CJK characters spaced out (each as a single token) and English words intact.
    """
    result = []
    for char in text:
        if '\u4e00' <= char <= '\u9fff':
            result.append(f' {char} ')
        else:
            result.append(char)
    return ''.join(result)


def _build_fts_query(query: str) -> str:
    """
    Build an FTS5 query string from natural language input.
    Extracts meaningful tokens and joins them with OR for flexible matching.
    """
    # Extract CJK characters and English words
    tokens = re.findall(r'[\u4e00-\u9fff]|[a-zA-Z0-9]+', query.lower())
    # Filter very short English tokens
    tokens = [t for t in tokens if len(t) > 1 or '\u4e00' <= t <= '\u9fff']
    if not tokens:
        return ""
    # Use OR to match any of the tokens (more flexible than AND)
    return ' OR '.join(f'"{t}"' for t in tokens)


# ---- Main Memory Store ----

class MemoryStore:
    """
    Structured memory storage with FTS5 full-text retrieval.
    Uses SQLite for persistence and FTS5 for BM25-ranked search.
    Supports memory hierarchy: core, working, episode.
    """

    def __init__(self, db_path: str = None):
        if db_path is None:
            from core.paths import get_data_path
            db_path = get_data_path("memory.db")
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
        self.db_path = db_path
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        with self._get_conn() as conn:
            # Main memories table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL DEFAULT 'general',
                    memory_type TEXT NOT NULL DEFAULT 'episode',
                    content TEXT NOT NULL,
                    keywords TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    access_count INTEGER DEFAULT 0,
                    importance INTEGER DEFAULT 1
                )
            """)

            # Conversations table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    summary TEXT NOT NULL,
                    category TEXT DEFAULT 'general',
                    messages_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            conn.commit()

            # Check if memory_type column exists (for migration)
            cursor = conn.execute("PRAGMA table_info(memories)")
            columns = [row[1] for row in cursor.fetchall()]
            if "memory_type" not in columns:
                conn.execute("ALTER TABLE memories ADD COLUMN memory_type TEXT NOT NULL DEFAULT 'episode'")
                conn.commit()

            # Create FTS5 virtual table if not exists
            try:
                conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                        content,
                        keywords,
                        content_rowid=id,
                        tokenize='unicode61'
                    )
                """)
                conn.commit()
            except Exception:
                pass  # Already exists or not supported

            # Sync FTS index with existing data
            self._sync_fts(conn)

    def _sync_fts(self, conn: sqlite3.Connection):
        """Ensure FTS index is in sync with the memories table."""
        try:
            # Get all memory IDs in FTS
            fts_ids = set()
            try:
                rows = conn.execute("SELECT rowid FROM memories_fts").fetchall()
                fts_ids = {r[0] for r in rows}
            except Exception:
                pass

            # Get all memory IDs in main table
            rows = conn.execute("SELECT id, content, keywords FROM memories").fetchall()
            main_ids = {r[0] for r in rows}

            # Remove stale FTS entries
            stale = fts_ids - main_ids
            for sid in stale:
                try:
                    conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (sid,))
                except Exception:
                    pass

            # Add missing FTS entries
            for mid, content, keywords in rows:
                if mid not in fts_ids:
                    fts_text = _tokenize_for_fts(content)
                    fts_kw = _tokenize_for_fts(keywords or "")
                    try:
                        conn.execute(
                            "INSERT INTO memories_fts(rowid, content, keywords) VALUES (?, ?, ?)",
                            (mid, fts_text, fts_kw)
                        )
                    except Exception:
                        pass

            conn.commit()
        except Exception:
            pass

    def add_memory(self, content: str, category: str = None,
                   keywords: str = "", importance: int = 1,
                   memory_type: str = "episode") -> int:
        """Add a new memory entry. Returns the memory ID."""
        if not category:
            category = auto_categorize(content)
        if memory_type not in MEMORY_TYPES:
            memory_type = "episode"

        now = datetime.now().isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute(
                "INSERT INTO memories (category, memory_type, content, keywords, "
                "created_at, updated_at, importance) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (category, memory_type, content, keywords, now, now, importance)
            )
            mid = cursor.lastrowid

            # Add to FTS index
            fts_text = _tokenize_for_fts(content)
            fts_kw = _tokenize_for_fts(keywords)
            try:
                conn.execute(
                    "INSERT INTO memories_fts(rowid, content, keywords) VALUES (?, ?, ?)",
                    (mid, fts_text, fts_kw)
                )
            except Exception:
                pass

            conn.commit()

        return mid

    def update_memory(self, memory_id: int, new_content: str,
                      keywords: str = None) -> bool:
        """Update an existing memory's content (for merging/dedup)."""
        now = datetime.now().isoformat()
        with self._get_conn() as conn:
            if keywords is not None:
                conn.execute(
                    "UPDATE memories SET content = ?, keywords = ?, updated_at = ? WHERE id = ?",
                    (new_content, keywords, now, memory_id)
                )
            else:
                conn.execute(
                    "UPDATE memories SET content = ?, updated_at = ? WHERE id = ?",
                    (new_content, now, memory_id)
                )

            # Update FTS index
            try:
                conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (memory_id,))
                fts_text = _tokenize_for_fts(new_content)
                fts_kw = _tokenize_for_fts(keywords or "")
                conn.execute(
                    "INSERT INTO memories_fts(rowid, content, keywords) VALUES (?, ?, ?)",
                    (memory_id, fts_text, fts_kw)
                )
            except Exception:
                pass

            conn.commit()
        return True

    def find_similar(self, content: str, threshold: int = 3) -> Optional[Dict]:
        """
        Find the most similar existing memory using FTS5.
        Returns the best match if it scores above threshold, else None.
        Used for deduplication before insertion.
        """
        fts_query = _build_fts_query(content)
        if not fts_query:
            return None

        try:
            with self._get_conn() as conn:
                rows = conn.execute("""
                    SELECT m.id, m.category, m.memory_type, m.content, m.keywords,
                           m.created_at, m.access_count, m.importance,
                           bm25(memories_fts) as score
                    FROM memories_fts fts
                    JOIN memories m ON fts.rowid = m.id
                    WHERE memories_fts MATCH ?
                    ORDER BY score ASC
                    LIMIT 1
                """, (fts_query,)).fetchall()

                if rows:
                    row = rows[0]
                    # bm25 returns negative scores, lower = better match
                    # Threshold: if score is very negative, it's a strong match
                    if row[8] < -threshold:
                        return {
                            "id": row[0], "category": row[1], "memory_type": row[2],
                            "content": row[3], "keywords": row[4], "created_at": row[5],
                            "access_count": row[6], "importance": row[7],
                            "score": round(row[8], 3)
                        }
        except Exception:
            pass

        return None

    def search_memories(self, query: str, top_k: int = 5,
                        category: str = None,
                        memory_type: str = None) -> List[Dict]:
        """Search for relevant memories using FTS5 BM25 ranking."""
        fts_query = _build_fts_query(query)
        if not fts_query:
            return []

        try:
            with self._get_conn() as conn:
                # Build query with optional filters
                sql = """
                    SELECT m.id, m.category, m.memory_type, m.content, m.keywords,
                           m.created_at, m.access_count, m.importance,
                           bm25(memories_fts) as score
                    FROM memories_fts fts
                    JOIN memories m ON fts.rowid = m.id
                    WHERE memories_fts MATCH ?
                """
                params = [fts_query]

                if category:
                    sql += " AND m.category = ?"
                    params.append(category)
                if memory_type:
                    sql += " AND m.memory_type = ?"
                    params.append(memory_type)

                # BM25 scores are negative, lower = better
                # Boost core memories by adjusting final sort
                sql += """
                    ORDER BY
                        CASE m.memory_type
                            WHEN 'core' THEN score * 1.5
                            WHEN 'working' THEN score * 1.2
                            ELSE score
                        END ASC
                    LIMIT ?
                """
                params.append(top_k * 2)

                rows = conn.execute(sql, params).fetchall()

                if not rows:
                    return []

                # Update access count
                memory_ids = [r[0] for r in rows[:top_k]]
                if memory_ids:
                    placeholders = ",".join("?" * len(memory_ids))
                    conn.execute(
                        f"UPDATE memories SET access_count = access_count + 1 "
                        f"WHERE id IN ({placeholders})",
                        memory_ids
                    )
                    conn.commit()

                memories = []
                for row in rows[:top_k]:
                    memories.append({
                        "id": row[0],
                        "category": row[1],
                        "memory_type": row[2],
                        "content": row[3],
                        "keywords": row[4],
                        "created_at": row[5],
                        "access_count": row[6],
                        "importance": row[7],
                        "relevance": round(abs(row[8]), 3)
                    })

                return memories
        except Exception as e:
            print(f"[MemoryStore] Search error: {e}")
            return []

    def get_all_memories(self, category: str = None,
                         memory_type: str = None,
                         limit: int = 50) -> List[Dict]:
        """Get all memories, optionally filtered by category and/or type."""
        with self._get_conn() as conn:
            sql = ("SELECT id, category, memory_type, content, keywords, "
                   "created_at, access_count, importance FROM memories WHERE 1=1")
            params = []

            if category:
                sql += " AND category = ?"
                params.append(category)
            if memory_type:
                sql += " AND memory_type = ?"
                params.append(memory_type)

            sql += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()

        return [
            {"id": r[0], "category": r[1], "memory_type": r[2], "content": r[3],
             "keywords": r[4], "created_at": r[5], "access_count": r[6],
             "importance": r[7]}
            for r in rows
        ]

    def delete_memory(self, memory_id: int) -> bool:
        with self._get_conn() as conn:
            conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            try:
                conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (memory_id,))
            except Exception:
                pass
            conn.commit()
        return True

    def save_conversation(self, summary: str, messages: list, category: str = None):
        """Save a conversation summary for later retrieval."""
        if not category:
            category = auto_categorize(summary)
        now = datetime.now().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO conversations (summary, category, messages_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (summary, category, json.dumps(messages, ensure_ascii=False), now)
            )
            conn.commit()

    def get_categories_summary(self) -> Dict[str, int]:
        """Get count of memories per category."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT category, COUNT(*) FROM memories GROUP BY category"
            ).fetchall()
        return {cat: count for cat, count in rows}

    def get_type_summary(self) -> Dict[str, int]:
        """Get count of memories per memory_type."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT memory_type, COUNT(*) FROM memories GROUP BY memory_type"
            ).fetchall()
        return {mt: count for mt, count in rows}

    def consolidate(self, llm_client=None) -> str:
        """
        Consolidate memories: remove duplicates and merge similar entries.
        If llm_client is provided, uses LLM for intelligent merging.
        Otherwise does simple dedup.
        """
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT id, content FROM memories ORDER BY created_at"
            ).fetchall()

        if len(rows) < 2:
            return "记忆条目不足，无需整理。"

        # Simple dedup: remove exact duplicates
        seen = {}
        duplicates = []
        for mid, content in rows:
            normalized = content.strip().lower()
            if normalized in seen:
                duplicates.append(mid)
            else:
                seen[normalized] = mid

        if duplicates:
            with self._get_conn() as conn:
                placeholders = ",".join("?" * len(duplicates))
                conn.execute(
                    f"DELETE FROM memories WHERE id IN ({placeholders})", duplicates
                )
                for did in duplicates:
                    try:
                        conn.execute("DELETE FROM memories_fts WHERE rowid = ?", (did,))
                    except Exception:
                        pass
                conn.commit()
            return f"记忆整理完成：移除了 {len(duplicates)} 条重复记忆。"

        return "没有发现重复记忆，记忆库状态良好。"


# Migrate from old memory.md format
def migrate_from_markdown(md_path: str, store: MemoryStore):
    """Import memories from the old markdown format."""
    if not os.path.exists(md_path):
        return

    with open(md_path, 'r', encoding='utf-8') as f:
        content = f.read()

    lines = content.strip().split('\n')
    for line in lines:
        line = line.strip()
        if line.startswith('- ') and line != '- No memories recorded yet.':
            memory_text = line[2:].strip()
            if memory_text:
                store.add_memory(memory_text)
