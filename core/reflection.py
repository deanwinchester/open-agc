"""
ReflectionEngine — Reflexion-style self-improvement for agent tasks.

After each task:
  - Success → store trajectory for future few-shot reuse
  - Failure → generate reflection text, store as episodic memory

On next task start:
  - Retrieve relevant past reflections and trajectories
  - Inject into context as prior experience
"""
import json
import sqlite3
import re
from datetime import datetime
from typing import List, Dict, Optional, Callable, Any


# Minimum tool-call count to bother reflecting on
MIN_TOOL_CALLS_FOR_REFLECTION = 2

# Maximum characters per trajectory entry
MAX_TRAJECTORY_CHARS = 3000

# Reflection generation prompt (English for LLM consistency)
REFLECTION_PROMPT = """You are a reflection generator. Analyze this agent task execution.

Task: {task_input}

Execution Summary:
{tool_sequence}

Result: {result}

{'The task SUCCEEDED. Extract 1-2 key insights: what patterns worked well, what decisions were critical.' if success else 'The task FAILED. Identify the root cause, what went wrong, and how to fix it next time.'}

Respond in JSON format:
```json
{{
  "insight": "One-sentence summary of what was learned",
  "detail": "2-3 sentence detailed explanation",
  "actionable": "What to do differently next time or what pattern to repeat"
}}
```"""


class ReflectionEngine:
    """Manages task reflection, trajectory storage, and experience retrieval."""

    def __init__(self, db_path: str, memory_store=None, llm_client=None):
        """
        Args:
            db_path: Path to SQLite database for trajectories.
            memory_store: MemoryStore instance for storing/retrieving reflections.
            llm_client: LLMClient instance for generating reflections.
        """
        self.db_path = db_path
        self.memory_store = memory_store
        self.llm_client = llm_client
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_trajectories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_input TEXT NOT NULL,
                    tool_sequence TEXT NOT NULL,
                    success INTEGER NOT NULL DEFAULT 0,
                    reflection_id INTEGER DEFAULT NULL,
                    duration_seconds REAL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trajectories_success
                ON task_trajectories(success)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trajectories_created
                ON task_trajectories(created_at)
            """)
            conn.commit()

    def _extract_tool_sequence(self, messages: List[Dict]) -> str:
        """Extract a concise tool-call sequence from messages."""
        steps = []
        for msg in messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    if isinstance(tc, dict):
                        name = tc.get("function", {}).get("name", "?")
                        args_raw = tc.get("function", {}).get("arguments", "{}")
                        try:
                            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                        except Exception:
                            args = {}
                        # Show first 2 args as preview
                        preview = ", ".join(f"{k}={str(v)[:50]}" for k, v in list(args.items())[:2])
                        steps.append(f"→ {name}({preview})")
            elif msg.get("role") == "tool":
                content = str(msg.get("content", ""))
                # Just note error/success, don't dump full content
                is_error = content.startswith("Error") or "traceback" in content.lower()
                if is_error:
                    if steps:
                        steps[-1] += " ❌"
        if not steps:
            return "No tool calls"
        return "\n".join(steps)

    def generate_reflection(self, task_input: str, messages: List[Dict],
                            success: bool, duration_seconds: float = 0) -> Optional[str]:
        """
        Analyze a completed task and generate/store a reflection.

        Returns the reflection text if generated, None if skipped.
        """
        tool_sequence = self._extract_tool_sequence(messages)
        tool_count = tool_sequence.count("\n→ ")

        # Skip trivial tasks with few tool calls
        if tool_count < MIN_TOOL_CALLS_FOR_REFLECTION:
            # Still store trajectory for successes that used tools
            if tool_count > 0 and success:
                self._store_trajectory(task_input, tool_sequence, success, None, duration_seconds)
            return None

        # Generate reflection via LLM
        reflection_text = None
        if self.llm_client and tool_count >= MIN_TOOL_CALLS_FOR_REFLECTION:
            reflection_text = self._llm_reflection(task_input, tool_sequence, success)

        # Store trajectory
        traj_id = self._store_trajectory(task_input, tool_sequence, success,
                                          reflection_text, duration_seconds)

        # Store reflection as episodic memory
        if reflection_text and self.memory_store:
            kw_parts = re.findall(r'\w+', task_input[:50])
            kw = f"reflection {'failure' if not success else 'success'} {' '.join(kw_parts)}"
            self.memory_store.add_memory(
                content=(
                    f"[Reflection] {reflection_text}\n"
                    f"Task: {task_input[:200]}"
                ),
                category="reflection",
                memory_type="episode",
                importance=3 if not success else 1,
                keywords=kw
            )

        return reflection_text

    def _llm_reflection(self, task_input: str, tool_sequence: str, success: bool) -> Optional[str]:
        """Call LLM to generate a structured reflection."""
        try:
            prompt = REFLECTION_PROMPT.format(
                task_input=task_input[:300],
                tool_sequence=tool_sequence[:2000],
                result="Success" if success else "Failed",
                success=str(success).lower()
            )
            response, _ = self.llm_client.chat(
                messages=[{"role": "user", "content": prompt}]
            )
            text = response.choices[0].message.content.strip()

            # Try to extract JSON from markdown code blocks
            json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
            if json_match:
                text = json_match.group(1).strip()

            # Try to parse as JSON to validate
            try:
                parsed = json.loads(text)
                parsed = {k: v for k, v in parsed.items() if k in ("insight", "detail", "actionable")}
                parts = []
                if parsed.get("insight"):
                    parts.append(f"💡 {parsed['insight']}")
                if parsed.get("detail"):
                    parts.append(parsed["detail"])
                if parsed.get("actionable"):
                    parts.append(f"→ {parsed['actionable']}")
                return "\n".join(parts) if parts else text
            except (json.JSONDecodeError, TypeError):
                return text[:500]

        except Exception as e:
            print(f"[Reflection] LLM reflection failed: {e}")
            return None

    def _store_trajectory(self, task_input: str, tool_sequence: str,
                           success: bool, reflection_text: Optional[str],
                           duration_seconds: float) -> int:
        """Store a task trajectory for future retrieval."""
        if len(tool_sequence) > MAX_TRAJECTORY_CHARS:
            tool_sequence = tool_sequence[:MAX_TRAJECTORY_CHARS] + "\n...(truncated)"

        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO task_trajectories "
                "(task_input, tool_sequence, success, duration_seconds, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_input[:500], tool_sequence, 1 if success else 0,
                 duration_seconds, datetime.now().isoformat())
            )
            conn.commit()
            return cur.lastrowid

    def retrieve_experience(self, query: str, top_k: int = 2) -> Dict:
        """
        Retrieve relevant past experiences for a new task.

        Returns:
          {
            "reflections": [...],  # relevant reflections from memory store
            "trajectories": [...]   # similar successful trajectories
          }
        """
        result: Dict[str, Any] = {"reflections": [], "trajectories": []}

        # Retrieve reflections from MemoryStore
        if self.memory_store:
            try:
                reflections = self.memory_store.search_memories(
                    query, top_k=top_k, category="reflection"
                )
                result["reflections"] = [
                    {"content": r["content"], "relevance": r.get("relevance", 0)}
                    for r in reflections
                ]
            except Exception as e:
                print(f"[Reflection] Memory search error: {e}")

        # Retrieve similar successful trajectories
        try:
            with sqlite3.connect(self.db_path) as conn:
                # Simple keyword-based trajectory search
                keywords = re.findall(r'[一-鿿\w]+', query.lower())
                keywords = [k for k in keywords if len(k) > 1]

                # FTS5 search on trajectories if available
                rows = None
                try:
                    # Try FTS5
                    fts_query = " OR ".join(f'"{k}"' for k in keywords[:5])
                    if fts_query:
                        rows = conn.execute("""
                            SELECT task_input, tool_sequence, success, created_at
                            FROM task_trajectories
                            WHERE task_input LIKE ?
                            ORDER BY created_at DESC
                            LIMIT ?
                        """, (f"%{query[:30]}%", top_k)).fetchall()
                except Exception:
                    pass

                if not rows:
                    # Fallback: just get recent successful ones
                    rows = conn.execute("""
                        SELECT task_input, tool_sequence, success, created_at
                        FROM task_trajectories
                        WHERE success = 1
                        ORDER BY created_at DESC
                        LIMIT ?
                    """, (top_k,)).fetchall()

                result["trajectories"] = [
                    {
                        "task_input": r[0],
                        "tool_sequence": r[1][:300],
                        "success": bool(r[2]),
                        "created_at": r[3],
                    }
                    for r in rows
                ]
        except Exception as e:
            print(f"[Reflection] Trajectory search error: {e}")

        return result

    def format_experience_for_prompt(self, experience: Dict) -> str:
        """Format retrieved experience into injectable prompt text."""
        parts = []

        if experience.get("reflections"):
            parts.append("【历史经验参考】")
            for r in experience["reflections"]:
                content = r.get("content", "")
                # Clean up the reflection prefix for display
                content = content.replace("[Reflection] ", "")
                parts.append(f"- {content[:300]}")

        if experience.get("trajectories"):
            parts.append("【相似成功轨迹参考】")
            for t in experience["trajectories"]:
                parts.append(
                    f"- 任务「{t['task_input'][:100]}」: "
                    f"{t['tool_sequence'][:200]}"
                )

        if parts:
            return "\n\n".join(parts)
        return ""

    def get_stats(self) -> Dict:
        """Get reflection engine statistics."""
        with sqlite3.connect(self.db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM task_trajectories").fetchone()[0]
            successes = conn.execute(
                "SELECT COUNT(*) FROM task_trajectories WHERE success = 1"
            ).fetchone()[0]
        reflections = 0
        if self.memory_store:
            reflections = len(self.memory_store.get_all_memories(
                category="reflection", limit=9999
            ))
        return {
            "total_trajectories": total,
            "successful_trajectories": successes,
            "total_reflections": reflections,
        }
