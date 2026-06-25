from typing import Any, Dict
from pydantic import PrivateAttr
from tools.base import BaseTool


class MemoryTool(BaseTool):
    name: str = "manage_memory"
    description: str = (
        "管理长期记忆。\n\n"
        "使用 'add' 保存重要信息（需指定 topic 话题标签，如 '车票'、'偏好'、'项目配置'）\n"
        "使用 'read' 查看已记住的内容\n"
        "使用 'update' 按 ID 替换记忆内容\n"
        "使用 'forget' 按 ID 删除记忆\n\n"
        "注意：查找记忆请用 search_history 工具（支持多源搜索和渐进展开），不要用 manage_memory 来搜索。"
    )

    _store: Any = PrivateAttr()

    def __init__(self, db_path: str = None, session_id: int = None):
        super().__init__()
        from core.memory_store import MemoryStore
        object.__setattr__(self, '_store', MemoryStore(db_path=db_path, session_id=session_id))

    def execute(self, action: str, content: str = "", query: str = "",
                category: str = "", memory_type: str = "",
                topic: str = "", cross_session: int = None,
                **kwargs) -> str:
        """
        Args:
            action: 'add', 'read', 'update', 'forget'
            content: 记忆内容（add/update 时必填）
            query: 记忆 ID（update/forget 时必填）
            topic: 话题标签（add 时建议填写，如 "车票"、"偏好"）
            category: 类别（tech/user_pref/project/knowledge/system/general）
            memory_type: core(长期核心事实)/working(工作记忆)/episode(事件经验)
        """
        if memory_type not in ("core", "working", "episode", ""):
            return "错误：memory_type 必须是 'core'、'working' 或 'episode'。"

        # ── Add ──
        if action == "add":
            if not content:
                return "错误：请提供记忆内容 'content'。"
            if not topic:
                topic = "general"
            kwargs_for_add = {
                "content": content,
                "category": category or None,
                "memory_type": memory_type or "working",
                "topic": topic,
                "source": "manual",
            }
            mid = self._store.add_memory(**kwargs_for_add)
            return f"✅ 记忆已添加（ID: {mid}，话题: {topic}，类型: {memory_type or 'working'}）"

        # ── Read ──
        elif action == "read":
            memories = self._store.get_all_memories(
                category=category or None,
                memory_type=memory_type or None,
                limit=20,
                session_id=cross_session,
            )
            if not memories:
                return "还没有存储任何记忆。"
            formatted = []
            for m in memories:
                type_label = {"core": "核心", "working": "工作", "episode": "事件"}.get(
                    m.get("memory_type", ""), ""
                )
                topic_tag = f" [{m.get('topic', '')}]" if m.get('topic') else ""
                status_tag = " [归档]" if m.get('status') == 'archived' else ""
                formatted.append(
                    f"[ID:{m['id']}]{topic_tag} ({m['category']}/{type_label}){status_tag} {m['content']}"
                )
            return "所有记忆：\n" + "\n".join(formatted)

        # ── Update ──
        elif action == "update":
            if not query:
                return "错误：请提供要更新的记忆 ID（query 参数）。"
            try:
                memory_id = int(query)
            except (ValueError, TypeError):
                return "错误：请在 'query' 中提供记忆 ID（数字）。"
            if not content:
                return "错误：请提供更新后的 'content'。"
            # Read old content first for reference
            old = self._store.get_memory(memory_id)
            self._store.update_memory(memory_id, content)
            old_preview = f" （原: {old['content'][:80]}）" if old else ""
            return f"✅ 记忆 ID {memory_id} 已更新{old_preview}。"

        # ── Forget ──
        elif action == "forget":
            if not query:
                return "错误：请提供要删除的记忆 ID（query 参数）。"
            try:
                memory_id = int(query)
            except (ValueError, TypeError):
                return "错误：请在 'query' 中提供记忆 ID（数字）。"
            old = self._store.get_memory(memory_id)
            if self._store.delete_memory(memory_id):
                return f"✅ 记忆 ID {memory_id} 已删除。"
            return f"错误：未找到 ID {memory_id} 的记忆。"

        return (
            f"错误：未知操作 '{action}'。"
            "可用操作：'add'（添加）、'read'（列表）、'update'（按 ID 更新）、'forget'（按 ID 删除）。"
        )

    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["add", "read", "update", "forget"],
                            "description": "操作类型：'add'=添加，'read'=查看全部，'update'=按ID更新，'forget'=按ID删除"
                        },
                        "content": {
                            "type": "string",
                            "description": "记忆内容（add/update 时必填，最长 2000 字）。"
                        },
                        "query": {
                            "type": "string",
                            "description": "记忆 ID（update/forget 时必填，数字）。"
                        },
                        "topic": {
                            "type": "string",
                            "description": "话题标签（add 时建议填写，如 '车票'、'偏好'、'项目配置'）。"
                                   "同话题的记忆会按相关性排序返回。"
                        },
                        "category": {
                            "type": "string",
                            "enum": ["", "tech", "user_pref", "project", "knowledge", "system", "general"],
                            "description": "类别（可选）"
                        },
                        "memory_type": {
                            "type": "string",
                            "enum": ["", "core", "working", "episode"],
                            "description": "记忆类型：core=长期核心事实，working=短期工作记忆（默认），episode=事件经验"
                        },
                    },
                    "required": ["action"],
                },
            },
        }
