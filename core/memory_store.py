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

    def __init__(self, db_path: str = None, session_id: Optional[int] = None):
        if db_path is None:
            from core.paths import get_data_path
            db_path = get_data_path("memory.db")
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
        self.db_path = db_path
        self.session_id = session_id  # None = global/unfiltered
        self._init_db()
        self._vectordb = None  # Lazy-init ChromaDB
        self._embed_fn = None

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

            # Migrations
            cursor = conn.execute("PRAGMA table_info(memories)")
            columns = {row[1] for row in cursor.fetchall()}

            if "memory_type" not in columns:
                conn.execute("ALTER TABLE memories ADD COLUMN memory_type TEXT NOT NULL DEFAULT 'episode'")
            try:
                conn.execute("ALTER TABLE memories ADD COLUMN session_id INTEGER DEFAULT 1")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE conversations ADD COLUMN session_id INTEGER DEFAULT 1")
            except Exception:
                pass
            # New fields for topic, recall tracking, status, source
            for col, dtype in [
                ("topic", "TEXT DEFAULT ''"),
                ("recall_count", "INTEGER DEFAULT 0"),
                ("last_recalled_at", "TEXT"),
                ("status", "TEXT DEFAULT 'active'"),
                ("source", "TEXT DEFAULT 'manual'"),
            ]:
                if col not in columns:
                    try:
                        conn.execute(f"ALTER TABLE memories ADD COLUMN {col} {dtype}")
                    except Exception:
                        pass
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
        """Ensure FTS index is in sync with the memories table.
        When session_id is set, only sync entries for the current session
        to avoid touching other sessions' FTS index entries.
        """
        try:
            # Get all memory IDs in FTS
            fts_ids = set()
            try:
                rows = conn.execute("SELECT rowid FROM memories_fts").fetchall()
                fts_ids = {r[0] for r in rows}
            except Exception:
                pass

            # Get memory IDs — scoped to current session when applicable
            if self.session_id is not None:
                rows = conn.execute(
                    "SELECT id, content, keywords FROM memories WHERE session_id = ?",
                    (self.session_id,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT id, content, keywords FROM memories").fetchall()
            main_ids = {r[0] for r in rows}

            # Remove stale FTS entries — only for current session's IDs
            if self.session_id is not None:
                stale = fts_ids & main_ids  # IDs in FTS that are for current session
                # Check which of those no longer exist in memories
                still_exist = set()
                for mid in stale:
                    row = conn.execute("SELECT id FROM memories WHERE id = ?", (mid,)).fetchone()
                    if row:
                        still_exist.add(mid)
                stale = stale - still_exist
            else:
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
                   memory_type: str = "episode",
                   topic: str = "", source: str = "manual") -> int:
        """Add a new memory entry. Returns the memory ID.

        Args:
            topic: Topic tag set by agent (e.g. "车票", "偏好"). Searchable via search_history.
            source: 'manual' (agent-initiated), 'reflection' (task reflection), 'auto' (legacy).
        """
        if not category:
            category = auto_categorize(content)
        if memory_type not in MEMORY_TYPES:
            memory_type = "episode"

        now = datetime.now().isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute(
                "INSERT INTO memories (category, memory_type, content, keywords, "
                "created_at, updated_at, importance, session_id, topic, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (category, memory_type, content, keywords, now, now,
                 importance, self.session_id or 1, topic, source)
            )
            mid = cursor.lastrowid

            # Add to FTS index (include topic in FTS for searchability)
            fts_text = _tokenize_for_fts(f"{topic} {content} {keywords}")
            try:
                conn.execute(
                    "INSERT INTO memories_fts(rowid, content, keywords) VALUES (?, ?, ?)",
                    (mid, fts_text, _tokenize_for_fts(keywords))
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
                sql = """
                    SELECT m.id, m.category, m.memory_type, m.content, m.keywords,
                           m.created_at, m.access_count, m.importance,
                           bm25(memories_fts) as score
                    FROM memories_fts fts
                    JOIN memories m ON fts.rowid = m.id
                    WHERE memories_fts MATCH ?
                """
                params = [fts_query]
                if self.session_id is not None:
                    sql += " AND m.session_id = ?"
                    params.append(self.session_id)
                sql += " ORDER BY score ASC LIMIT 1"
                rows = conn.execute(sql, params).fetchall()

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

    def record_recall(self, memory_id: int, weight: int = 1):
        """Increment recall_count and update last_recalled_at for a memory."""
        now = datetime.now().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE memories SET recall_count = recall_count + ?, "
                "last_recalled_at = ?, access_count = access_count + ? WHERE id = ?",
                (weight, now, weight, memory_id)
            )
            conn.commit()

    def _memory_score(self, row, created_dt, now_dt) -> float:
        """Compute composite score for a memory based on recency, frequency, type."""
        from datetime import datetime as _dt
        days_since_created = max(1, (now_dt - created_dt).days)
        # recency: when was this memory last recalled (index 10 = last_recalled_at)
        last_recalled = row[10] if len(row) > 10 and row[10] else row[5]  # fallback to created_at
        try:
            lr_dt = _dt.fromisoformat(last_recalled) if isinstance(last_recalled, str) else _dt.now()
        except Exception:
            lr_dt = now_dt
        days_since_recalled = max(0, (now_dt - lr_dt).days)
        # type weight: core=1.0, working=0.7, episode=0.4
        type_w = {"core": 1.0, "working": 0.7, "episode": 0.4}.get(row[2], 0.4)
        recall_count = row[9] if len(row) > 9 else 0
        recency = 1.0 / (1.0 + days_since_recalled * 0.1)
        frequency = recall_count / max(1, days_since_created)
        return recency * 0.35 + frequency * 0.35 + type_w * 0.30

    def search_memories(self, query: str, top_k: int = 5,
                        category: str = None,
                        memory_type: str = None,
                        session_id: Optional[int] = None,
                        topic: str = "",
                        include_archived: bool = False) -> List[Dict]:
        """Search for relevant memories using FTS5 BM25 ranking + composite scoring.

        Args:
            session_id: Override instance's session_id filter.
                        None=instance default, -1=global.
            topic: Filter by topic tag.
            include_archived: If True, also search archived (old) memories.
        """
        fts_query = _build_fts_query(query)
        if not fts_query:
            return []

        from datetime import datetime as _dt_now
        now_dt = _dt_now.now()

        try:
            with self._get_conn() as conn:
                sql = """
                    SELECT m.id, m.category, m.memory_type, m.content, m.keywords,
                           m.created_at, m.access_count, m.importance,
                           bm25(memories_fts) as bm25_score,
                           m.recall_count, m.last_recalled_at, m.topic, m.status
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
                if topic:
                    sql += " AND m.topic = ?"
                    params.append(topic)
                if not include_archived:
                    sql += " AND m.status = 'active'"

                effective_session = session_id if session_id is not None else self.session_id
                if effective_session is not None and effective_session != -1:
                    sql += " AND m.session_id = ?"
                    params.append(effective_session)

                sql += " LIMIT ?"
                params.append(top_k * 3)

                rows = conn.execute(sql, params).fetchall()
                if not rows:
                    return []

                # Compute composite score in Python
                scored = []
                for row in rows:
                    created_at_str = row[5]
                    try:
                        created_dt = _dt_now.fromisoformat(created_at_str)
                    except Exception:
                        created_dt = now_dt
                    composite = self._memory_score(row, created_dt, now_dt)
                    scored.append((composite, row))

                scored.sort(key=lambda x: -x[0])
                top_rows = scored[:top_k]

                # Record recall for returned memories
                memory_ids = [r[1][0] for r in top_rows]
                if memory_ids:
                    placeholders = ",".join("?" * len(memory_ids))
                    now_s = _dt_now.now().isoformat()
                    conn.execute(
                        f"UPDATE memories SET recall_count = recall_count + 1, "
                        f"last_recalled_at = ?, access_count = access_count + 1 "
                        f"WHERE id IN ({placeholders})",
                        (now_s, *memory_ids)
                    )
                    conn.commit()

                memories = []
                for composite, row in top_rows:
                    memories.append({
                        "id": row[0], "category": row[1], "memory_type": row[2],
                        "content": row[3], "keywords": row[4], "created_at": row[5],
                        "access_count": row[6], "importance": row[7],
                        "relevance": round(composite, 3),
                        "recall_count": row[9], "topic": row[11], "status": row[12],
                    })

                return memories
        except Exception as e:
            print(f"[MemoryStore] Search error: {e}")
            return []

    def get_memory(self, memory_id: int) -> Optional[Dict]:
        """Get a single memory by ID with all fields."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT id, category, memory_type, content, keywords, "
                "created_at, updated_at, access_count, importance, "
                "recall_count, last_recalled_at, topic, status, source "
                "FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "category": row[1], "memory_type": row[2],
            "content": row[3], "keywords": row[4],
            "created_at": row[5], "updated_at": row[6],
            "access_count": row[7], "importance": row[8],
            "recall_count": row[9], "last_recalled_at": row[10],
            "topic": row[11], "status": row[12], "source": row[13],
        }

    def get_all_memories(self, category: str = None,
                         memory_type: str = None,
                         limit: int = 50,
                         session_id: Optional[int] = None,
                         status: str = "active") -> List[Dict]:
        """Get all memories, optionally filtered."""
        with self._get_conn() as conn:
            sql = ("SELECT id, category, memory_type, content, keywords, "
                   "created_at, access_count, importance, recall_count, "
                   "last_recalled_at, topic, status, source "
                   "FROM memories WHERE 1=1")
            params = []

            if status:
                sql += " AND status = ?"
                params.append(status)
            if category:
                sql += " AND category = ?"
                params.append(category)
            if memory_type:
                sql += " AND memory_type = ?"
                params.append(memory_type)

            effective_session = session_id if session_id is not None else self.session_id
            if effective_session is not None:
                sql += " AND session_id = ?"
                params.append(effective_session)

            sql += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()

        return [
            {"id": r[0], "category": r[1], "memory_type": r[2], "content": r[3],
             "keywords": r[4], "created_at": r[5], "access_count": r[6],
             "importance": r[7], "recall_count": r[8], "last_recalled_at": r[9],
             "topic": r[10], "status": r[11], "source": r[12]}
            for r in rows
        ]

    def delete_memory(self, memory_id: int) -> bool:
        with self._get_conn() as conn:
            if self.session_id is not None:
                row = conn.execute(
                    "SELECT id FROM memories WHERE id = ? AND session_id = ?",
                    (memory_id, self.session_id)
                ).fetchone()
                if not row:
                    return False
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
                "INSERT INTO conversations (summary, category, messages_json, created_at, session_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (summary, category, json.dumps(messages, ensure_ascii=False), now, self.session_id or 1)
            )
            conn.commit()

    def get_categories_summary(self) -> Dict[str, int]:
        """Get count of memories per category."""
        with self._get_conn() as conn:
            if self.session_id is not None:
                rows = conn.execute(
                    "SELECT category, COUNT(*) FROM memories WHERE session_id = ? GROUP BY category",
                    (self.session_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT category, COUNT(*) FROM memories GROUP BY category"
                ).fetchall()
        return {cat: count for cat, count in rows}

    def get_type_summary(self) -> Dict[str, int]:
        """Get count of memories per memory_type."""
        with self._get_conn() as conn:
            if self.session_id is not None:
                rows = conn.execute(
                    "SELECT memory_type, COUNT(*) FROM memories WHERE session_id = ? GROUP BY memory_type",
                    (self.session_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT memory_type, COUNT(*) FROM memories GROUP BY memory_type"
                ).fetchall()
        return {mt: count for mt, count in rows}

    def archive_old_memories(self, max_age_days: int = 365, min_score: float = 0.1) -> int:
        """Auto-archive memories that are old and have low scores.

        Called periodically during search. Returns number of archived memories.
        """
        from datetime import datetime as _dt_now
        now_dt = _dt_now.now()
        archived = 0
        with self._get_conn() as conn:
            sql = ("SELECT id, category, memory_type, content, keywords, created_at, "
                   "access_count, importance, recall_count, last_recalled_at, topic, status "
                   "FROM memories WHERE status = 'active'")
            params = []
            if self.session_id is not None:
                sql += " AND session_id = ?"
                params.append(self.session_id)
            rows = conn.execute(sql, params).fetchall()
            for row in rows:
                created_at_str = row[5]
                try:
                    created_dt = _dt_now.fromisoformat(created_at_str)
                except Exception:
                    continue
                age_days = (now_dt - created_dt).days
                if age_days >= max_age_days:
                    score = self._memory_score(row, created_dt, now_dt)
                    if score < min_score:
                        conn.execute("UPDATE memories SET status = 'archived' WHERE id = ?", (row[0],))
                        archived += 1
            if archived:
                conn.commit()
        return archived

    def consolidate(self, llm_client=None) -> str:
        """
        Consolidate memories: remove duplicates and merge similar entries.
        If llm_client is provided, uses LLM for intelligent merging.
        Otherwise does simple dedup.
        """
        with self._get_conn() as conn:
            if self.session_id is not None:
                rows = conn.execute(
                    "SELECT id, content FROM memories WHERE session_id = ? ORDER BY created_at",
                    (self.session_id,)
                ).fetchall()
            else:
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

    # ── Vector / Semantic Search (ChromaDB) ──

    def _init_vector(self):
        """Lazy-init ChromaDB collection for semantic search."""
        if self._vectordb is not None:
            return
        try:
            import chromadb
            from chromadb.config import Settings
            from core.paths import get_data_path
            chroma_dir = get_data_path("chromadb")
            os.makedirs(chroma_dir, exist_ok=True)
            client = chromadb.PersistentClient(
                path=chroma_dir,
                settings=Settings(anonymized_telemetry=False)
            )
            self._vectordb = client.get_or_create_collection(
                name=f"memories_{self.session_id or 'global'}",
                metadata={"hnsw:space": "cosine"}
            )
        except ImportError:
            print("[MemoryStore] ChromaDB not installed — falling back to FTS5 only")
        except Exception as e:
            print(f"[MemoryStore] Vector init failed: {e}")

    @staticmethod
    def _embed(text: str) -> list:
        """Generate embedding for text using sentence-transformers."""
        try:
            from sentence_transformers import SentenceTransformer
            if not hasattr(MemoryStore, '_embed_model'):
                MemoryStore._embed_model = SentenceTransformer(
                    'all-MiniLM-L6-v2', device='cpu')
            return MemoryStore._embed_model.encode(
                text[:2000], normalize_embeddings=True).tolist()
        except ImportError:
            return None
        except Exception as e:
            print(f"[MemoryStore] Embed error: {e}")
            return None

    def add_memory_vector(self, content: str, category: str = None,
                          keywords: str = "", importance: int = 1,
                          memory_type: str = "episode") -> int:
        """Add memory with vector embedding for semantic search."""
        mid = self.add_memory(content, category, keywords, importance, memory_type)
        self._init_vector()
        if self._vectordb:
            emb = self._embed(content)
            if emb:
                try:
                    self._vectordb.add(
                        ids=[str(mid)],
                        embeddings=[emb],
                        metadatas=[{"category": category or "general",
                                     "type": memory_type}]
                    )
                except Exception as e:
                    print(f"[MemoryStore] Vector add error: {e}")
        return mid

    def search_semantic(self, query: str, top_k: int = 5,
                        session_id: Optional[int] = None) -> List[Dict]:
        """Semantic search using ChromaDB. Falls back to FTS5 if unavailable."""
        self._init_vector()
        if not self._vectordb:
            return []  # Caller should fall back to search_memories

        emb = self._embed(query)
        if not emb:
            return []

        try:
            results = self._vectordb.query(
                query_embeddings=[emb],
                n_results=top_k
            )
            ids = results.get("ids", [[]])[0]
            if not ids:
                return []

            # Fetch full memory records from SQLite
            mems = []
            with self._get_conn() as conn:
                for mid in ids:
                    row = conn.execute(
                        "SELECT id, category, memory_type, content, keywords, "
                        "created_at, access_count, importance FROM memories "
                        "WHERE id=?", (int(mid),)
                    ).fetchone()
                    if row:
                        mems.append({
                            "id": row[0], "category": row[1], "memory_type": row[2],
                            "content": row[3], "keywords": row[4], "created_at": row[5],
                            "access_count": row[6], "importance": row[7],
                            "relevance": 0.85  # Approximate
                        })
            return mems
        except Exception as e:
            print(f"[MemoryStore] Semantic search error: {e}")
            return []


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
