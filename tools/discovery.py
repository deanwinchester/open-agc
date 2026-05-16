import json
from typing import Callable, Dict, Any, List
from tools.base import BaseTool

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

    def execute(self, query: str) -> str:
        # Simple keyword matching algorithm
        # In a production system this could be replaced with TF-IDF or Vector Embeddings
        query_terms = set(query.lower().replace("_", " ").split())
        
        scored_tools = []
        for name, tool in self.full_tools.items():
            if name == self.name:
                continue
                
            # Use getattr for robustness in case some legacy tools lack description
            tool_desc = getattr(tool, "description", "")
            if not tool_desc and hasattr(tool, "get_openai_schema"):
                try:
                    schema = tool.get_openai_schema()
                    tool_desc = schema.get("function", {}).get("description", "")
                except Exception:
                    pass
            
            if not tool_desc:
                continue
                
            score = 0
            desc_lower = tool_desc.lower()
            name_lower = name.lower()
            
            for term in query_terms:
                if len(term) < 2: continue # Ignore very short terms
                if term in name_lower:
                    score += 5
                if term in desc_lower:
                    score += 1
            
            # Additional heuristic: if tool name is exactly in query
            if name_lower in query.lower():
                score += 10
                
            if score > 0:
                scored_tools.append((score, name, tool_desc))
                
        if not scored_tools:
            return f"No matching tools found for query '{query}'. Try different keywords."
            
        # Sort by score descending and take top 5
        scored_tools.sort(key=lambda x: x[0], reverse=True)
        top_tools = scored_tools[:5]
        
        tool_names_to_enable = [name for _, name, _ in top_tools]
        
        # Invoke the callback to update the agent's active tools
        try:
            self.enable_callback(tool_names_to_enable)
        except Exception as e:
            return f"Error enabling tools: {str(e)}"
            
        result_lines = [f"Successfully discovered and enabled the following {len(top_tools)} tools for you. You can call them in your NEXT action:"]
        for _, name, desc in top_tools:
            result_lines.append(f"- {name}: {desc[:100]}...")
            
        return "\n".join(result_lines)
