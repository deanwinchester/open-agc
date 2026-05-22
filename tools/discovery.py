"""Tool discovery with cross-language keyword matching.

Supports Chinese query terms by translating them to English equivalents
and using CJK character overlap scoring as fallback.
"""
import re
from typing import Callable, Dict, Any, List
from tools.base import BaseTool

# Chinese → English keyword translation for common tech/search terms
_CJK_KEYWORDS = {
    # Search & browse
    "搜索": "search",
    "搜寻": "search",
    "查找": "find",
    "检索": "search",
    "查询": "query",
    "网页": "web",
    "网络": "web internet network",
    "浏览": "browse",
    "浏览器": "browser",
    "上网": "browse internet",
    # File operations
    "文件": "file",
    "读取": "read",
    "写入": "write write_file",
    "编辑": "edit",
    "修改": "edit modify",
    "删除": "delete remove",
    "目录": "directory dir folder",
    "文件夹": "directory folder",
    "路径": "path",
    # Code / Python
    "代码": "code python",
    "脚本": "script python",
    "编程": "programming code python",
    "运行": "run execute",
    "执行": "execute run",
    # Shell / system
    "终端": "terminal shell",
    "命令": "command shell",
    "控制台": "console terminal shell",
    "系统": "system",
    # Memory & knowledge
    "记忆": "memory remember",
    "记住": "memory remember",
    "知识": "knowledge",
    "技能": "skill",
    # Communication
    "邮件": "email mail",
    "发送": "send",
    "消息": "message",
    # Download
    "下载": "download",
    "上传": "upload",
    # Browser automation
    "自动化": "automation auto",
    "自动": "auto automation",
    "点击": "click",
    "打开": "open",
    # Ask / question
    "提问": "ask question",
    "询问": "ask question",
    "用户": "user",
    # History
    "历史": "history",
    "会话": "session history chat",
    # Other
    "工具": "tool",
    "功能": "tool function capability",
    "信息": "information info",
    "内容": "content",
    "电脑": "computer",
    "截图": "screenshot capture screen",
    "屏幕": "screen display",
    "图片": "image picture screenshot",
    "暂停": "pause wait",
    "等待": "wait",
    "后台": "background pause",
    "沙箱": "sandbox",
    "隔离": "sandbox isolate",
}


def _translate_cjk(query: str) -> str:
    """Translate CJK terms in a query to English equivalents.

    Returns the original query with Chinese terms replaced/appended
    with their English translations, enabling keyword matching against
    English tool descriptions.
    """
    result = query
    for cn, en in _CJK_KEYWORDS.items():
        if cn in query:
            result += " " + en
    return result


def _cjk_char_overlap(query: str, target: str) -> float:
    """Score CJK character overlap between query and target.

    Returns ratio of unique CJK chars in query that also appear in target.
    Useful for matching Chinese queries against English tool names/descriptions
    when direct keyword translation fails.
    """
    query_chars = set(c for c in query if '一' <= c <= '鿿')
    if not query_chars:
        return 0.0
    target_lower = target.lower()
    matches = sum(1 for c in query_chars if c in target_lower)
    return matches / len(query_chars)


def _has_cjk(text: str) -> bool:
    """Check if text contains CJK characters."""
    return bool(re.search(r'[一-鿿]', text))


class ToolDiscoveryTool(BaseTool):
    model_config = {"extra": "allow", "arbitrary_types_allowed": True}

    name: str = "search_available_tools"
    description: str = (
        "Search and discover advanced tools based on your current needs. "
        "Use this tool when you need capabilities that are not in your current tool list "
        "(e.g., 'browser', 'web search', 'email', 'python', etc.). "
        "It will search the system's deferred tools pool and enable the matching tools. "
        "You can then use them in your NEXT step."
    )

    def __init__(self, full_tools: Dict[str, BaseTool], enable_callback: Callable[[List[str]], None], **kwargs):
        super().__init__(**kwargs)
        self.full_tools = full_tools
        self.enable_callback = enable_callback

    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "A natural language query describing the capability you need (e.g., 'search web', 'browser automation', 'execute python')."
                        }
                    },
                    "required": ["query"]
                }
            }
        }

    def execute(self, query: str, **kwargs) -> str:
        # ── Expand Chinese terms to English equivalents ──
        expanded_query = _translate_cjk(query)
        query_lower = query.lower()
        query_terms = set(expanded_query.lower().replace("_", " ").split())

        has_cjk = _has_cjk(query)
        scored_tools = []

        for name, tool in self.full_tools.items():
            if name == self.name:
                continue

            # Get tool description (from attr or schema)
            tool_desc = getattr(tool, "description", "")
            if not tool_desc and hasattr(tool, "get_openai_schema"):
                try:
                    schema = tool.get_openai_schema()
                    tool_desc = schema.get("function", {}).get("description", "")
                except Exception:
                    pass
            if not tool_desc:
                continue

            desc_lower = tool_desc.lower()
            name_lower = name.lower()
            score = 0

            # Score 1: exact term in tool name (+5) or description (+1)
            for term in query_terms:
                if len(term) < 2:
                    continue
                if term in name_lower:
                    score += 5
                if term in desc_lower:
                    score += 1

            # Score 2: tool name appears in original query (+10)
            if name_lower in query_lower:
                score += 10

            # Score 3: for CJK queries, character overlap with name/description
            if has_cjk and score == 0:
                name_overlap = _cjk_char_overlap(query, name_lower)
                if name_overlap > 0:
                    score += int(name_overlap * 5)
                desc_overlap = _cjk_char_overlap(query, desc_lower)
                if desc_overlap > 0:
                    score += int(desc_overlap * 2)

            if score > 0:
                scored_tools.append((score, name, tool_desc))

        if not scored_tools:
            return f"No matching tools found for query '{query}'. Try different keywords."

        # Sort by score descending and take top 5
        scored_tools.sort(key=lambda x: x[0], reverse=True)
        top_tools = scored_tools[:5]

        tool_names_to_enable = [name for _, name, _ in top_tools]

        try:
            self.enable_callback(tool_names_to_enable)
        except Exception as e:
            return f"Error enabling tools: {str(e)}"

        result_lines = [
            f"Successfully discovered and enabled the following {len(top_tools)} tools for you. "
            f"You can call them in your NEXT action:"
        ]
        for _, name, desc in top_tools:
            result_lines.append(f"- {name}: {desc[:100]}...")

        return "\n".join(result_lines)
