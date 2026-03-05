import json
from typing import List, Dict, Any, Optional, Callable

from core.llm_client import LLMClient
from core.memory_store import MemoryStore
from tools.shell import ShellTool
from tools.filesystem import ReadFileTool, WriteFileTool
from tools.python_repl import PythonREPLTool
from tools.computer import ComputerTool
from tools.memory import MemoryTool
from tools.web_search import WebSearchTool
from tools.system_mac import MacSystemTool

class OpenAGCAgent:
    """
    Main Agent Loop handling context, Tool calling, and orchestration.
    Supports real-time progress callbacks for task tracking.
    Features smart memory with TF-IDF semantic retrieval.
    """
    def __init__(self, model: str = "gpt-4o"):
        self.llm = LLMClient(default_model=model)
        # Load config to check disabled skills
        disabled_skills = []
        config_path = "data/config.json"
        import os
        if os.path.exists(config_path):
            try:
                import json
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    disabled_skills = config.get("disabled_skills", [])
            except Exception: pass

        # Load skills from the skills directory
        skills_text = ""
        skills_dir = "skills"
        if os.path.exists(skills_dir):
            for filename in os.listdir(skills_dir):
                if filename in disabled_skills:
                    continue
                if filename.endswith(".md") or filename.endswith(".py"):
                    filepath = os.path.join(skills_dir, filename)
                    try:
                        with open(filepath, 'r', encoding='utf-8') as f:
                            skills_text += f"\n--- Skill: {filename} ---\n{f.read()}\n"
                    except Exception as e:
                        print(f"Error loading skill {filename}: {str(e)}")

        # Inject current date/time so the LLM knows "today"
        from datetime import datetime
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        current_date = datetime.now().strftime("%Y年%m月%d日")

        system_prompt = (
            f"You are Open-AGC, a powerful AI agent capable of executing commands, "
            f"running python code, interacting with the file system, and physically "
            f"controlling the computer mouse and keyboard. "
            f"Always verify assumptions explicitly using your tools.\n"
            f"\n--- CURRENT DATE & TIME ---\n"
            f"The current date and time is: {current_time} (即 {current_date}).\n"
            f"Your training data has a knowledge cutoff. For ANY question about "
            f"recent events, current news, latest updates, or anything time-sensitive, "
            f"you MUST use the search_web tool to get up-to-date information. "
            f"NEVER rely solely on your training data for current affairs.\n"
            f"\nIMPORTANT: When handling complex tasks that involve multiple steps, "
            f"explain your plan briefly BEFORE starting, then execute each step. "
            f"This helps the user understand your progress.\n"
            f"\nMEMORY: You have a smart memory system. At the start of each conversation, "
            f"relevant memories from past interactions are automatically retrieved and shown to you. "
            f"Use the manage_memory tool with action='add' to proactively save important facts, "
            f"user preferences, and learned knowledge for future reference. "
            f"Use action='search' to find specific past memories.\n"
        )
        if skills_text:
            system_prompt += f"\nHere are some learned skills you should follow:\n{skills_text}"

        # Initialize smart memory store (replaces old memory.md)
        self.memory_store = MemoryStore(db_path="data/memory.db")

        self.system_prompt = system_prompt
        self.messages: List[Dict[str, Any]] = [
            {
                "role": "system",
                "content": system_prompt
            }
        ]
        
        # Instantiate tools (MemoryTool shares the same store)
        memory_tool = MemoryTool(db_path="data/memory.db")
        self.available_tools = {
            "execute_shell": ShellTool(),
            "read_file": ReadFileTool(),
            "write_file": WriteFileTool(),
            "execute_python": PythonREPLTool(),
            "computer_control": ComputerTool(),
            "manage_memory": memory_tool,
            "search_web": WebSearchTool(),
            "mac_system_action": MacSystemTool()
        }

        # Tool display names (Chinese-friendly)
        self.tool_display_names = {
            "execute_shell": "执行终端命令",
            "read_file": "读取文件",
            "write_file": "写入文件",
            "execute_python": "运行 Python 代码",
            "computer_control": "操控电脑",
            "manage_memory": "管理记忆",
            "search_web": "搜索网页",
            "mac_system_action": "系统操作"
        }
        
        # Prepare OpenAI format tool schema
        self.tool_schemas = [tool.get_openai_schema() for tool in self.available_tools.values()]

    def run_turn(self, user_input: str, verbose: bool = False,
                 progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None) -> str:
        """
        Run a single conversational turn. Will loop until the LLM returns a final text message.
        
        Args:
            user_input: The user's message.
            verbose: If true, print debug info.
            progress_callback: Optional callback for real-time progress updates.
                Called with dicts like:
                  {"event": "thinking", "model": "..."}
                  {"event": "tool_start", "step": 1, "tool": "...", "tool_label": "...", "args_preview": "..."}
                  {"event": "tool_done", "step": 1, "tool": "...", "result_preview": "..."}
                  {"event": "model_switched", "from": "...", "to": "..."}
        """
        self.messages.append({"role": "user", "content": user_input})
        
        # Auto-retrieve relevant memories for this query
        try:
            relevant_memories = self.memory_store.search_memories(user_input, top_k=3)
            if relevant_memories:
                memory_context = "\n".join(
                    f"- [{m['category']}] {m['content']}" for m in relevant_memories
                )
                # Inject as a system message so the LLM sees it
                self.messages.append({
                    "role": "system",
                    "content": f"--- RELEVANT MEMORIES ---\n{memory_context}"
                })
        except Exception as e:
            print(f"[Agent] Memory retrieval error: {e}")
        
        max_iterations = 15
        current_iter = 0
        step_counter = 0
        
        while current_iter < max_iterations:
            current_iter += 1
            if verbose:
                print(f"[Agent Loop Iteration {current_iter}] Calling LLM...")
            
            # Notify: thinking
            if progress_callback:
                progress_callback({"event": "thinking", "iteration": current_iter})
            
            response, actual_model = self.llm.chat(messages=self.messages, tools=self.tool_schemas)
            message = response.choices[0].message
            
            # Notify if model was switched
            if progress_callback and actual_model != self.llm.default_model:
                progress_callback({
                    "event": "model_switched",
                    "from": self.llm.default_model,
                    "to": actual_model
                })
            
            # Append model's response to history
            message_dict = message.model_dump()
            self.messages.append(message_dict)
            
            # 1. Check if model decided to use tools
            tool_calls = message.tool_calls
            if tool_calls:
                for tool_call in tool_calls:
                    function_name = tool_call.function.name
                    function_args = json.loads(tool_call.function.arguments)
                    
                    step_counter += 1
                    
                    # Create a short preview of args for the UI
                    args_preview = ""
                    if "command" in function_args:
                        args_preview = function_args["command"][:80]
                    elif "path" in function_args:
                        args_preview = function_args["path"][:80]
                    elif "code" in function_args:
                        args_preview = function_args["code"][:80]
                    elif "query" in function_args:
                        args_preview = function_args["query"][:80]
                    
                    tool_label = self.tool_display_names.get(function_name, function_name)
                    
                    # Notify: tool starting
                    if progress_callback:
                        progress_callback({
                            "event": "tool_start",
                            "step": step_counter,
                            "tool": function_name,
                            "tool_label": tool_label,
                            "args_preview": args_preview
                        })
                    
                    if verbose:
                        print(f"\n[Tool Execution] {function_name}({function_args})")
                    
                    tool_instance = self.available_tools.get(function_name)
                    if tool_instance:
                        try:
                            result = tool_instance.execute(**function_args)
                        except Exception as e:
                            result = f"Error executing tool: {str(e)}"
                    else:
                        result = f"Error: Tool {function_name} not found."
                    
                    result_str = str(result)
                    
                    # Notify: tool done
                    if progress_callback:
                        # Truncate result for preview
                        preview = result_str[:120] + "..." if len(result_str) > 120 else result_str
                        progress_callback({
                            "event": "tool_done",
                            "step": step_counter,
                            "tool": function_name,
                            "tool_label": tool_label,
                            "result_preview": preview,
                            "success": not result_str.startswith("Error")
                        })
                    
                    if verbose:
                        print(f"[Tool Result]\n{result_str}\n")
                        
                    # Append tool result to messages
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": function_name,
                        "content": result_str
                    })
                
                # After appending all tool results, the loop continues to send them back to LLM
                continue
                
            # 2. Check if model provided a text response (final answer)
            if message.content:
                return message.content
                
        return "Agent stopped: Reached maximum iterations without a final answer."
