import json
from typing import List, Dict, Any

from core.llm_client import LLMClient
from tools.shell import ShellTool
from tools.filesystem import ReadFileTool, WriteFileTool
from tools.python_repl import PythonREPLTool
from tools.computer import ComputerTool
from tools.memory import MemoryTool

class OpenAGCAgent:
    """
    Main Agent Loop handling context, Tool calling, and orchestration.
    """
    def __init__(self, model: str = "gpt-4o"):
        self.llm = LLMClient(default_model=model)
        
        # Load skills from the skills directory
        skills_text = ""
        skills_dir = "skills"
        import os
        if os.path.exists(skills_dir):
            for filename in os.listdir(skills_dir):
                if filename.endswith(".md") or filename.endswith(".py"):
                    filepath = os.path.join(skills_dir, filename)
                    try:
                        with open(filepath, 'r', encoding='utf-8') as f:
                            skills_text += f"\n--- Skill: {filename} ---\n{f.read()}\n"
                    except Exception as e:
                        print(f"Error loading skill {filename}: {str(e)}")

        system_prompt = (
            "You are Open-AGC, a powerful AI agent capable of executing commands, "
            "running python code, interacting with the file system, and physically "
            "controlling the computer mouse and keyboard. "
            "Always verify assumptions explicitly using your tools.\n"
        )
        if skills_text:
            system_prompt += f"\nHere are some learned skills you should follow:\n{skills_text}"

        # Load long-term memory
        memory_path = "data/memory.md"
        if os.path.exists(memory_path):
            try:
                with open(memory_path, 'r', encoding='utf-8') as f:
                    memory_text = f.read()
                    system_prompt += f"\n\n--- LONG-TERM MEMORY ---\n{memory_text}\n"
            except Exception as e:
                print(f"Error loading memory: {str(e)}")

        self.messages: List[Dict[str, Any]] = [
            {
                "role": "system",
                "content": system_prompt
            }
        ]
        
        # Instantiate tools
        self.available_tools = {
            "execute_shell": ShellTool(),
            "read_file": ReadFileTool(),
            "write_file": WriteFileTool(),
            "execute_python": PythonREPLTool(),
            "computer_control": ComputerTool(),
            "manage_memory": MemoryTool()
        }
        
        # Prepare OpenAI format tool schema
        self.tool_schemas = [tool.get_openai_schema() for tool in self.available_tools.values()]

    def run_turn(self, user_input: str, verbose: bool = False) -> str:
        """
        Run a single conversational turn. Will loop until the LLM returns a final text message.
        """
        self.messages.append({"role": "user", "content": user_input})
        
        max_iterations = 15
        current_iter = 0
        
        while current_iter < max_iterations:
            current_iter += 1
            if verbose:
                print(f"[Agent Loop Iteration {current_iter}] Calling LLM...")
                
            response = self.llm.chat(messages=self.messages, tools=self.tool_schemas)
            message = response.choices[0].message
            
            # Append model's response to history
            # Convert message to dict format and append
            message_dict = message.model_dump()
            self.messages.append(message_dict)
            
            # 1. Check if model decided to use tools
            tool_calls = message.tool_calls
            if tool_calls:
                for tool_call in tool_calls:
                    function_name = tool_call.function.name
                    function_args = json.loads(tool_call.function.arguments)
                    
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
                    
                    if verbose:
                        print(f"[Tool Result]\n{result}\n")
                        
                    # Append tool result to messages
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": function_name,
                        "content": str(result)
                    })
                
                # After appending all tool results, the loop continues to send them back to LLM
                continue
                
            # 2. Check if model provided a text response (final answer)
            if message.content:
                return message.content
                
        return "Agent stopped: Reached maximum iterations without a final answer."
