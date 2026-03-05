import os
from core.memory_store import MemoryStore, migrate_from_markdown


class MemoryTool:
    """
    Smart memory management tool. Uses TF-IDF semantic search to store
    and retrieve relevant memories from past conversations.
    """

    def __init__(self, db_path: str = "data/memory.db"):
        self.store = MemoryStore(db_path=db_path)
        
        # Migrate from old markdown format if it exists
        old_md_path = "data/memory.md"
        if os.path.exists(old_md_path):
            migrate_from_markdown(old_md_path, self.store)

    def execute(self, action: str, content: str = "", query: str = "",
                category: str = "", importance: int = 1) -> str:
        """
        Execute memory operations.
        
        Args:
            action: 'search', 'add', 'read', 'consolidate', 'categories', or legacy 'append'/'overwrite'
            content: Memory content to add (for 'add'/'append')
            query: Search query (for 'search')
            category: Optional category filter
            importance: Priority level 1-5 (for 'add')
        """
        if action == "search":
            search_query = query or content
            if not search_query:
                return "Error: Please provide a 'query' for search."
            
            results = self.store.search_memories(search_query, top_k=5, category=category or None)
            if not results:
                return "No relevant memories found."
            
            formatted = []
            for m in results:
                formatted.append(
                    f"[{m['category']}] (relevance: {m['relevance']}) {m['content']}"
                )
            return "Found relevant memories:\n" + "\n".join(formatted)

        elif action in ("add", "append"):
            if not content:
                return "Error: Please provide 'content' to add."
            mid = self.store.add_memory(content, category=category or None, importance=importance)
            return f"Memory added (ID: {mid}, category: auto-detected)."

        elif action == "read":
            memories = self.store.get_all_memories(category=category or None, limit=20)
            if not memories:
                return "No memories stored yet."
            
            formatted = []
            for m in memories:
                formatted.append(f"[{m['category']}] {m['content']}")
            return "All memories:\n" + "\n".join(formatted)

        elif action == "consolidate":
            result = self.store.consolidate()
            return result

        elif action == "categories":
            cats = self.store.get_categories_summary()
            if not cats:
                return "No memories stored yet."
            lines = [f"  {cat}: {count} memories" for cat, count in cats.items()]
            return "Memory categories:\n" + "\n".join(lines)

        elif action == "overwrite":
            # Legacy: clear all and add new content
            if content:
                # Delete all existing
                all_mems = self.store.get_all_memories(limit=1000)
                for m in all_mems:
                    self.store.delete_memory(m["id"])
                self.store.add_memory(content)
                return "Memory overwritten with new content."
            return "Error: No content provided."

        else:
            return (
                f"Error: Unknown action '{action}'. "
                "Available: 'search', 'add', 'read', 'consolidate', 'categories'."
            )

    def get_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "manage_memory",
                "description": (
                    "Manage your long-term memory with intelligent search. "
                    "Use 'search' to find relevant past memories by semantic similarity. "
                    "Use 'add' to store important facts, user preferences, or learned knowledge. "
                    "Use 'read' to list all memories. Use 'consolidate' to clean up duplicates. "
                    "Use 'categories' to see memory categories and counts."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["search", "add", "read", "consolidate", "categories"],
                            "description": "The action to perform.",
                        },
                        "content": {
                            "type": "string",
                            "description": "Memory content to add (for 'add' action).",
                        },
                        "query": {
                            "type": "string",
                            "description": "Search query (for 'search' action).",
                        },
                        "category": {
                            "type": "string",
                            "description": "Category filter (optional). Values: tech, user_pref, project, knowledge, system, general.",
                        },
                        "importance": {
                            "type": "integer",
                            "description": "Priority level 1-5 for 'add' (default 1).",
                        },
                    },
                    "required": ["action"],
                },
            },
        }
