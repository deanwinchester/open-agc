import os

class MemoryTool:
    """
    A tool that allows the Agent to read, write, and update its own long-term memory.
    The memory is stored in a markdown file (data/memory.md).
    """
    def __init__(self, memory_path: str = "data/memory.md"):
        self.memory_path = memory_path
        os.makedirs(os.path.dirname(self.memory_path), exist_ok=True)
        # Initialize if not exists
        if not os.path.exists(self.memory_path):
            with open(self.memory_path, 'w', encoding='utf-8') as f:
                f.write("# Agent Long-Term Memory\n\n- No memories recorded yet.\n")

    def execute(self, action: str, content: str = "") -> str:
        """
        Execute memory operations.
        
        Args:
            action: "read" to read the memory file, "append" to add a new memory bullet, "overwrite" to completely replace the memory content.
            content: The text to append or overwrite. Ignored if action is "read".
            
        Returns:
            The execution result or the file content.
        """
        if action == "read":
            try:
                with open(self.memory_path, 'r', encoding='utf-8') as f:
                    return f.read()
            except Exception as e:
                return f"Error reading memory: {str(e)}"
                
        elif action == "append":
            try:
                with open(self.memory_path, 'a', encoding='utf-8') as f:
                    # Automatically format as a bullet point if not already
                    formatted_content = content if content.startswith("- ") else f"- {content}"
                    f.write(f"{formatted_content}\n")
                return "Successfully appended to memory."
            except Exception as e:
                return f"Error appending to memory: {str(e)}"

        elif action == "overwrite":
            try:
                with open(self.memory_path, 'w', encoding='utf-8') as f:
                    f.write(content)
                return "Successfully overwrote memory."
            except Exception as e:
                return f"Error overwriting memory: {str(e)}"
                
        else:
            return f"Error: Unknown memory action '{action}'. Use 'read', 'append', or 'overwrite'."

    def get_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "manage_memory",
                "description": (
                    "Manage your long-term memory. Use this tool proactively to 'append' important facts "
                    "about the user, their preferences, or the system configuration so you remember them in future conversations. "
                    "You can 'read' the current memory, 'append' new bullet points, or 'overwrite' the entire memory if it gets too messy."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": "The action to perform: 'read', 'append', or 'overwrite'."
                        },
                        "content": {
                            "type": "string",
                            "description": "The text to add or overwrite. Leave empty if action is 'read'."
                        }
                    },
                    "required": ["action"]
                }
            }
        }
