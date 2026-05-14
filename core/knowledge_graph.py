"""
KnowledgeGraph — Lightweight entity-relation graph extracted from task execution.

Extracts entities (files, commands, APIs, dependencies, projects) from tool calls
and results, mines relations between them, and provides context retrieval for
task planning augmentation.
"""
import json
import re
import sqlite3
from datetime import datetime
from typing import List, Dict, Optional, Any, Tuple


# Patterns for tool call argument extraction
ARG_COMMAND_PATTERN = re.compile(
    r'(?:(?:^|\s)(?:pip|npm|yarn|npx|git|cargo|go|make|cmake|docker|kubectl|helm|curl|wget|uvicorn|gunicorn|node|python|python3|ruby|bundle|rake|gem|brew|apt|yum|dnf|pacman|choco|scoop|poetry|conda|mamba))',
    re.IGNORECASE
)

ARG_PATH_PATTERN = re.compile(
    r'(?:[A-Za-z]:\\(?:[^\\\s]+\\)*[^\\\s]*|(?:\/[a-zA-Z_][\w\/.\-]*)+(?:\/)?)'
)

ARG_URL_PATTERN = re.compile(r'https?://[^\s\'"<>)]+')

# Import/dependency extraction
IMPORT_PATTERN = re.compile(r'(?:^|\n)\s*(?:from\s+(\S+)\s+)?import\s+(\S+)', re.MULTILINE)
PIP_INSTALL_PATTERN = re.compile(r'(?:pip|pip3)\s+install\s+(\S+)', re.IGNORECASE)
NPM_INSTALL_PATTERN = re.compile(r'npm\s+install\s+(\S+)', re.IGNORECASE)


class KnowledgeGraph:
    """Lightweight knowledge graph for entity-relation extraction and retrieval."""

    def __init__(self, db_path: str, session_id: Optional[int] = None):
        self.db_path = db_path
        self.session_id = session_id  # None = global/unfiltered
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kg_entities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL,
                    metadata TEXT DEFAULT '{}',
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    confidence REAL DEFAULT 1.0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kg_relations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER NOT NULL,
                    target_id INTEGER NOT NULL,
                    relation_type TEXT NOT NULL,
                    weight INTEGER DEFAULT 1,
                    last_seen TEXT NOT NULL,
                    FOREIGN KEY (source_id) REFERENCES kg_entities(id),
                    FOREIGN KEY (target_id) REFERENCES kg_entities(id)
                )
            """)
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_name_type
                ON kg_entities(name, type)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_relations_type
                ON kg_relations(relation_type)
            """)

            # Add session_id columns for session isolation
            try:
                conn.execute("ALTER TABLE kg_entities ADD COLUMN session_id INTEGER DEFAULT 1")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE kg_relations ADD COLUMN session_id INTEGER DEFAULT 1")
            except Exception:
                pass

            conn.commit()

    # ------------------------------------------------------------------
    # Entity extraction
    # ------------------------------------------------------------------

    def extract_from_messages(self, messages: List[Dict]):
        """Extract entities and relations from tool call messages."""
        all_entities: List[Tuple[str, str]] = []  # (name, type)

        for msg in messages:
            # Extract from tool call arguments
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    if isinstance(tc, dict):
                        args_raw = tc.get("function", {}).get("arguments", "{}")
                        func_name = tc.get("function", {}).get("name", "")
                        try:
                            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                        except Exception:
                            args = {}
                        entities = self._extract_from_args(func_name, args)
                        all_entities.extend(entities)

            # Extract from tool results
            elif msg.get("role") == "tool":
                content = str(msg.get("content", ""))
                entities = self._extract_from_text(content)
                all_entities.extend(entities)

        # Deduplicate and store
        self._store_entities_and_mine_relations(all_entities)

    def _extract_from_args(self, func_name: str, args: Dict) -> List[Tuple[str, str]]:
        """Extract entities from a single tool call's arguments."""
        entities: List[Tuple[str, str]] = []

        if func_name == "execute_shell":
            command = args.get("command", "")
            # Find command names
            for m in ARG_COMMAND_PATTERN.finditer(command):
                name = m.group(0).strip()
                if name:
                    entities.append((name, "command"))
            # Find file paths
            for m in ARG_PATH_PATTERN.finditer(command):
                entities.append((m.group(0), "file"))
            # Extract dependencies from pip/npm install
            for m in PIP_INSTALL_PATTERN.finditer(command):
                entities.append((m.group(1).strip(), "dependency"))
            for m in NPM_INSTALL_PATTERN.finditer(command):
                entities.append((m.group(1).strip(), "dependency"))

        elif func_name in ("read_file", "write_file"):
            path = args.get("path", args.get("file_path", ""))
            if path:
                entities.append((path, "file"))

        elif func_name == "browser_automation":
            url = args.get("url", "")
            if url:
                entities.append((url, "url"))

        elif func_name == "search_web":
            query = args.get("query", "")
            entities.append((query[:60], "search_query"))

        return entities

    def _extract_from_text(self, text: str) -> List[Tuple[str, str]]:
        """Extract entities from arbitrary text content."""
        entities: List[Tuple[str, str]] = []

        # URLs
        for m in ARG_URL_PATTERN.finditer(text):
            entities.append((m.group(0), "url"))

        # Dependencies from import statements
        for m in IMPORT_PATTERN.finditer(text):
            module = m.group(1) or m.group(2)
            entities.append((module.split(".")[0], "dependency"))

        return entities

    # ------------------------------------------------------------------
    # Storage and relation mining
    # ------------------------------------------------------------------

    def _store_entities_and_mine_relations(self, entities: List[Tuple[str, str]]):
        """Deduplicate entities, store them, and mine co-occurrence relations."""
        if not entities:
            return

        now = datetime.now().isoformat()
        entity_ids: List[int] = []

        with sqlite3.connect(self.db_path) as conn:
            # Deduplicate while keeping order
            seen = set()
            unique: List[Tuple[str, str]] = []
            for name, etype in entities:
                key = (name.strip()[:200], etype)
                if key not in seen:
                    seen.add(key)
                    unique.append(key)

            for name, etype in unique:
                if not name or len(name) < 2:
                    continue
                cur = conn.execute(
                    "SELECT id, confidence FROM kg_entities WHERE name = ? AND type = ? AND session_id = ?",
                    (name, etype, self.session_id or 1)
                ).fetchone()
                if cur:
                    # Update existing
                    new_conf = min(1.0, cur[1] + 0.1)
                    conn.execute(
                        "UPDATE kg_entities SET last_seen = ?, confidence = ? WHERE id = ? AND session_id = ?",
                        (now, new_conf, cur[0], self.session_id or 1)
                    )
                    entity_ids.append(cur[0])
                else:
                    cur = conn.execute(
                        "INSERT INTO kg_entities (name, type, first_seen, last_seen, confidence, session_id) "
                        "VALUES (?, ?, ?, ?, 0.5, ?)",
                        (name, etype, now, now, self.session_id or 1)
                    )
                    entity_ids.append(cur.lastrowid)

            # Mine co-occurrence relations: every pair of entities in this batch is "co_occurs_with"
            if len(entity_ids) >= 2:
                for i in range(len(entity_ids)):
                    for j in range(i + 1, len(entity_ids)):
                        self._upsert_relation(
                            conn, entity_ids[i], entity_ids[j],
                            "co_occurs_with", now
                        )

            conn.commit()

    def _upsert_relation(self, conn: sqlite3.Connection,
                         source_id: int, target_id: int,
                         relation_type: str, now: str):
        cur = conn.execute(
            "SELECT id, weight FROM kg_relations "
            "WHERE source_id = ? AND target_id = ? AND relation_type = ?",
            (source_id, target_id, relation_type)
        ).fetchone()
        if cur:
            conn.execute(
                "UPDATE kg_relations SET weight = ?, last_seen = ? WHERE id = ?",
                (cur[1] + 1, now, cur[0])
            )
        else:
            conn.execute(
                "INSERT INTO kg_relations (source_id, target_id, relation_type, weight, last_seen) "
                "VALUES (?, ?, ?, 1, ?)",
                (source_id, target_id, relation_type, now)
            )

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def retrieve_context(self, query: str, top_k: int = 5,
                         session_id: Optional[int] = None) -> List[Dict]:
        """Search entities by keyword match, return top matching entities with relations.

        Args:
            session_id: Override the instance's session_id filter.
        """
        # Extract keywords from query
        keywords = re.findall(r'[一-鿿\w]+', query.lower())
        keywords = [k for k in keywords if len(k) > 1]

        if not keywords:
            return []

        # Build LIKE pattern for any keyword match
        like_clauses = " OR ".join("e.name LIKE ?" for _ in keywords[:5])
        params = list(f"%{kw}%" for kw in keywords[:5])

        # Session isolation
        effective_session = session_id if session_id is not None else self.session_id
        if effective_session is not None:
            like_clauses = f"({like_clauses}) AND e.session_id = ?"
            params.append(effective_session)

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(f"""
                SELECT e.id, e.name, e.type, e.confidence, e.last_seen
                FROM kg_entities e
                WHERE {like_clauses}
                ORDER BY e.confidence DESC, e.last_seen DESC
                LIMIT ?
            """, params + [top_k]).fetchall()

            results = []
            for row in rows:
                eid, name, etype, confidence, last_seen = row
                # Get relations for this entity
                relations = conn.execute("""
                    SELECT r.relation_type, r.weight, e2.name, e2.type
                    FROM kg_relations r
                    JOIN kg_entities e2 ON e2.id = CASE WHEN r.source_id = ? THEN r.target_id ELSE r.source_id END
                    WHERE r.source_id = ? OR r.target_id = ?
                    ORDER BY r.weight DESC
                    LIMIT 5
                """, (eid, eid, eid)).fetchall()

                results.append({
                    "name": name,
                    "type": etype,
                    "confidence": confidence,
                    "last_seen": last_seen,
                    "relations": [
                        {"type": r[0], "weight": r[1], "target_name": r[2], "target_type": r[3]}
                        for r in relations
                    ],
                })

            return results

    def format_context(self, results: List[Dict]) -> str:
        """Format retrieved KG context into injectable prompt text."""
        if not results:
            return ""

        parts = ["【知识图谱关联】"]
        for entity in results:
            line = f"{entity['type']} {entity['name']} (置信度: {entity['confidence']:.1f})"
            if entity["relations"]:
                rels = [f"  - {r['type']}: {r['target_name']} ({r['target_type']})"
                       for r in entity["relations"][:3]]
                line += "\n" + "\n".join(rels)
            parts.append(line)

        return "\n".join(parts)

    def get_stats(self) -> Dict:
        """Get knowledge graph statistics."""
        sess = self.session_id or 1
        with sqlite3.connect(self.db_path) as conn:
            entities = conn.execute(
                "SELECT COUNT(*) FROM kg_entities WHERE session_id = ?", (sess,)
            ).fetchone()[0]
            relations = conn.execute(
                "SELECT COUNT(*) FROM kg_relations r "
                "JOIN kg_entities e ON e.id = r.source_id WHERE e.session_id = ?", (sess,)
            ).fetchone()[0]
            by_type = conn.execute(
                "SELECT type, COUNT(*) FROM kg_entities WHERE session_id = ? "
                "GROUP BY type ORDER BY COUNT(*) DESC", (sess,)
            ).fetchall()
        return {
            "total_entities": entities,
            "total_relations": relations,
            "entities_by_type": dict(by_type),
        }
