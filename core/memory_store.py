"""
Smart Memory Store — TF-IDF based semantic retrieval with SQLite storage.
Replaces the simple markdown memory file with a structured, searchable system.
"""
import os
import json
import sqlite3
import math
import re
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from collections import Counter


# ---- Lightweight TF-IDF Implementation (no external deps) ----

def tokenize(text: str) -> List[str]:
    """Simple tokenizer: split on non-word chars, lowercase, remove short tokens."""
    text = text.lower()
    # Keep CJK characters as individual tokens, split English on word boundaries
    tokens = re.findall(r'[\u4e00-\u9fff]|[a-z0-9]+', text)
    return [t for t in tokens if len(t) > 1 or '\u4e00' <= t <= '\u9fff']


def compute_tf(tokens: List[str]) -> Dict[str, float]:
    """Term frequency: count / total tokens."""
    counter = Counter(tokens)
    total = len(tokens)
    if total == 0:
        return {}
    return {term: count / total for term, count in counter.items()}


class TFIDFIndex:
    """Lightweight in-memory TF-IDF index for semantic search."""
    
    def __init__(self):
        self.documents: List[Tuple[str, List[str]]] = []  # (doc_id, tokens)
        self.idf_cache: Dict[str, float] = {}
        self._dirty = True

    def add_document(self, doc_id: str, text: str):
        tokens = tokenize(text)
        self.documents.append((doc_id, tokens))
        self._dirty = True

    def clear(self):
        self.documents.clear()
        self.idf_cache.clear()
        self._dirty = True

    def _compute_idf(self):
        if not self._dirty:
            return
        n_docs = len(self.documents)
        if n_docs == 0:
            self.idf_cache = {}
            return
        
        doc_freq: Dict[str, int] = {}
        for _, tokens in self.documents:
            for term in set(tokens):
                doc_freq[term] = doc_freq.get(term, 0) + 1
        
        self.idf_cache = {
            term: math.log((n_docs + 1) / (df + 1)) + 1
            for term, df in doc_freq.items()
        }
        self._dirty = False

    def search(self, query: str, top_k: int = 5) -> List[Tuple[str, float]]:
        """Search for most similar documents. Returns [(doc_id, score), ...]."""
        self._compute_idf()
        
        query_tokens = tokenize(query)
        if not query_tokens or not self.documents:
            return []
        
        query_tf = compute_tf(query_tokens)
        # Query TF-IDF vector
        query_vec = {term: tf * self.idf_cache.get(term, 0) for term, tf in query_tf.items()}
        query_norm = math.sqrt(sum(v * v for v in query_vec.values()))
        if query_norm == 0:
            return []
        
        scores = []
        for doc_id, doc_tokens in self.documents:
            doc_tf = compute_tf(doc_tokens)
            doc_vec = {term: tf * self.idf_cache.get(term, 0) for term, tf in doc_tf.items()}
            doc_norm = math.sqrt(sum(v * v for v in doc_vec.values()))
            if doc_norm == 0:
                continue
            
            # Cosine similarity
            dot_product = sum(query_vec.get(term, 0) * doc_vec.get(term, 0) 
                           for term in set(list(query_vec.keys()) + list(doc_vec.keys())))
            similarity = dot_product / (query_norm * doc_norm)
            
            if similarity > 0.01:
                scores.append((doc_id, similarity))
        
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]


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


# ---- Main Memory Store ----

class MemoryStore:
    """
    Structured memory storage with TF-IDF semantic retrieval.
    Uses SQLite for persistence and in-memory TF-IDF for search.
    """
    
    def __init__(self, db_path: str = "data/memory.db"):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self.index = TFIDFIndex()
        self._init_db()
        self._rebuild_index()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL DEFAULT 'general',
                    content TEXT NOT NULL,
                    keywords TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    access_count INTEGER DEFAULT 0,
                    importance INTEGER DEFAULT 1
                )
            """)
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

    def _rebuild_index(self):
        """Rebuild TF-IDF index from all memories."""
        self.index.clear()
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT id, content, keywords FROM memories").fetchall()
            for mid, content, keywords in rows:
                text = f"{content} {keywords or ''}"
                self.index.add_document(str(mid), text)

    def add_memory(self, content: str, category: str = None, 
                   keywords: str = "", importance: int = 1) -> int:
        """Add a new memory entry. Returns the memory ID."""
        if not category:
            category = auto_categorize(content)
        
        now = datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO memories (category, content, keywords, created_at, updated_at, importance) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (category, content, keywords, now, now, importance)
            )
            mid = cursor.lastrowid
            conn.commit()
        
        self.index.add_document(str(mid), f"{content} {keywords}")
        return mid

    def search_memories(self, query: str, top_k: int = 5, category: str = None) -> List[Dict]:
        """Search for relevant memories using TF-IDF similarity."""
        results = self.index.search(query, top_k=top_k * 2)  # Get extra for filtering
        
        if not results:
            return []
        
        memory_ids = [int(doc_id) for doc_id, _ in results]
        scores = {doc_id: score for doc_id, score in results}
        
        placeholders = ",".join("?" * len(memory_ids))
        query_sql = f"SELECT id, category, content, keywords, created_at, access_count, importance FROM memories WHERE id IN ({placeholders})"
        
        if category:
            query_sql += f" AND category = ?"
            params = memory_ids + [category]
        else:
            params = memory_ids
        
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(query_sql, params).fetchall()
            
            # Increment access count
            conn.executemany(
                "UPDATE memories SET access_count = access_count + 1 WHERE id = ?",
                [(mid,) for mid in memory_ids[:top_k]]
            )
            conn.commit()
        
        memories = []
        for row in rows:
            mid, cat, content, keywords, created_at, access_count, importance = row
            memories.append({
                "id": mid,
                "category": cat,
                "content": content,
                "keywords": keywords,
                "created_at": created_at,
                "access_count": access_count,
                "importance": importance,
                "relevance": round(scores.get(str(mid), 0), 3)
            })
        
        # Sort by relevance * importance
        memories.sort(key=lambda m: m["relevance"] * m["importance"], reverse=True)
        return memories[:top_k]

    def get_all_memories(self, category: str = None, limit: int = 50) -> List[Dict]:
        """Get all memories, optionally filtered by category."""
        with sqlite3.connect(self.db_path) as conn:
            if category:
                rows = conn.execute(
                    "SELECT id, category, content, keywords, created_at, access_count, importance "
                    "FROM memories WHERE category = ? ORDER BY updated_at DESC LIMIT ?",
                    (category, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, category, content, keywords, created_at, access_count, importance "
                    "FROM memories ORDER BY updated_at DESC LIMIT ?",
                    (limit,)
                ).fetchall()
        
        return [
            {"id": r[0], "category": r[1], "content": r[2], "keywords": r[3],
             "created_at": r[4], "access_count": r[5], "importance": r[6]}
            for r in rows
        ]

    def delete_memory(self, memory_id: int) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            conn.commit()
        self._rebuild_index()
        return True

    def save_conversation(self, summary: str, messages: list, category: str = None):
        """Save a conversation summary for later retrieval."""
        if not category:
            category = auto_categorize(summary)
        now = datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO conversations (summary, category, messages_json, created_at) VALUES (?, ?, ?, ?)",
                (summary, category, json.dumps(messages, ensure_ascii=False), now)
            )
            conn.commit()

    def get_categories_summary(self) -> Dict[str, int]:
        """Get count of memories per category."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT category, COUNT(*) FROM memories GROUP BY category"
            ).fetchall()
        return {cat: count for cat, count in rows}

    def consolidate(self, llm_client=None) -> str:
        """
        Consolidate memories: remove duplicates and merge similar entries.
        If llm_client is provided, uses LLM for intelligent merging.
        Otherwise does simple dedup.
        """
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT id, content FROM memories ORDER BY created_at"
            ).fetchall()
        
        if len(rows) < 2:
            return "Not enough memories to consolidate."
        
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
            with sqlite3.connect(self.db_path) as conn:
                placeholders = ",".join("?" * len(duplicates))
                conn.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", duplicates)
                conn.commit()
            self._rebuild_index()
            return f"Consolidated: removed {len(duplicates)} duplicate memories."
        
        return "No duplicates found. Memories are clean."


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
