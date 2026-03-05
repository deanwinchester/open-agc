import os
from core.memory_store import MemoryStore, migrate_from_markdown


class MemoryTool:
    """
    智能记忆管理工具。使用 FTS5 全文搜索来存储和检索相关记忆。
    支持记忆层次：core（核心事实）、working（工作记忆）、episode（事件记录）。
    """

    def __init__(self, db_path: str = "data/memory.db"):
        self.store = MemoryStore(db_path=db_path)

        # Migrate from old markdown format if it exists
        old_md_path = "data/memory.md"
        if os.path.exists(old_md_path):
            migrate_from_markdown(old_md_path, self.store)

    def execute(self, action: str, content: str = "", query: str = "",
                category: str = "", memory_type: str = "",
                importance: int = 1) -> str:
        """
        Execute memory operations.

        Args:
            action: 'search', 'add', 'read', 'update', 'consolidate', 'categories', 'types'
            content: Memory content to add (for 'add'/'update')
            query: Search query (for 'search')
            category: Optional category filter
            memory_type: Memory type: 'core', 'working', or 'episode'
            importance: Priority level 1-5 (for 'add')
        """
        if action == "search":
            search_query = query or content
            if not search_query:
                return "错误：请提供搜索关键词 'query'。"

            results = self.store.search_memories(
                search_query, top_k=5,
                category=category or None,
                memory_type=memory_type or None
            )
            if not results:
                return "没有找到相关记忆。"

            formatted = []
            for m in results:
                type_label = {"core": "核心", "working": "工作", "episode": "事件"}.get(
                    m.get("memory_type", ""), ""
                )
                formatted.append(
                    f"[{m['category']}/{type_label}] (相关度: {m['relevance']}) {m['content']}"
                )
            return "找到相关记忆：\n" + "\n".join(formatted)

        elif action in ("add", "append"):
            if not content:
                return "错误：请提供要添加的 'content'。"
            mid = self.store.add_memory(
                content, category=category or None,
                importance=importance,
                memory_type=memory_type or "episode"
            )
            cat = category or "自动检测"
            mt = memory_type or "episode"
            return f"记忆已添加（ID: {mid}，类别: {cat}，类型: {mt}）。"

        elif action == "update":
            if not query and not content:
                return "错误：请提供要更新的记忆 ID（query）和新内容（content）。"
            try:
                memory_id = int(query)
            except (ValueError, TypeError):
                return "错误：请在 'query' 中提供记忆 ID（数字）。"
            if not content:
                return "错误：请提供更新后的 'content'。"
            self.store.update_memory(memory_id, content)
            return f"记忆 ID {memory_id} 已更新。"

        elif action == "read":
            memories = self.store.get_all_memories(
                category=category or None,
                memory_type=memory_type or None,
                limit=20
            )
            if not memories:
                return "还没有存储任何记忆。"

            formatted = []
            for m in memories:
                type_label = {"core": "核心", "working": "工作", "episode": "事件"}.get(
                    m.get("memory_type", ""), ""
                )
                formatted.append(f"[{m['category']}/{type_label}] {m['content']}")
            return "所有记忆：\n" + "\n".join(formatted)

        elif action == "consolidate":
            result = self.store.consolidate()
            return result

        elif action == "categories":
            cats = self.store.get_categories_summary()
            if not cats:
                return "还没有存储任何记忆。"
            lines = [f"  {cat}: {count} 条" for cat, count in cats.items()]
            return "记忆分类统计：\n" + "\n".join(lines)

        elif action == "types":
            types = self.store.get_type_summary()
            if not types:
                return "还没有存储任何记忆。"
            type_names = {"core": "核心事实", "working": "工作记忆", "episode": "事件记录"}
            lines = [f"  {type_names.get(mt, mt)}: {count} 条" for mt, count in types.items()]
            return "记忆类型统计：\n" + "\n".join(lines)

        elif action == "overwrite":
            if content:
                all_mems = self.store.get_all_memories(limit=1000)
                for m in all_mems:
                    self.store.delete_memory(m["id"])
                self.store.add_memory(content)
                return "记忆已全部替换为新内容。"
            return "错误：未提供内容。"

        elif action == "forget":
            if not query:
                return "错误：请提供要删除的记忆 ID（query）。"
            try:
                memory_id = int(query)
            except (ValueError, TypeError):
                return "错误：请在 'query' 中提供记忆 ID（数字）。"
            self.store.delete_memory(memory_id)
            return f"记忆 ID {memory_id} 已删除。"

        else:
            return (
                f"错误：未知操作 '{action}'。"
                "可用操作：'search'、'add'、'read'、'update'、'forget'、"
                "'consolidate'、'categories'、'types'。"
            )

    def get_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "manage_memory",
                "description": (
                    "管理你的长期记忆系统。"
                    "使用 'search' 通过语义相似度搜索相关记忆。"
                    "使用 'add' 保存重要事实、用户偏好或学到的知识。"
                    "使用 'read' 列出所有记忆。"
                    "使用 'update' 更新已有记忆。"
                    "使用 'forget' 删除某条记忆。"
                    "使用 'consolidate' 整理去重。"
                    "使用 'categories' 查看分类统计，'types' 查看类型统计。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["search", "add", "read", "update", "forget",
                                     "consolidate", "categories", "types"],
                            "description": "要执行的操作。",
                        },
                        "content": {
                            "type": "string",
                            "description": "记忆内容（用于 'add'、'update'）。",
                        },
                        "query": {
                            "type": "string",
                            "description": "搜索关键词（用于 'search'），或记忆 ID（用于 'update'、'forget'）。",
                        },
                        "category": {
                            "type": "string",
                            "description": "分类过滤（可选）。值：tech, user_pref, project, knowledge, system, general。",
                        },
                        "memory_type": {
                            "type": "string",
                            "enum": ["core", "working", "episode"],
                            "description": "记忆类型：core=长期核心事实，working=近期工作记忆，episode=事件记录。",
                        },
                        "importance": {
                            "type": "integer",
                            "description": "优先级 1-5（用于 'add'，默认 1）。",
                        },
                    },
                    "required": ["action"],
                },
            },
        }
