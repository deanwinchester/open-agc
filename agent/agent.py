import json
import threading
import hashlib
from typing import List, Dict, Any, Optional, Callable
import os

from core.paths import get_data_path, get_skills_dir

from core.llm_client import LLMClient
from core.memory_store import MemoryStore
from tools.shell import ShellTool
from tools.filesystem import ReadFileTool, WriteFileTool
from tools.python_repl import PythonREPLTool
from tools.computer import ComputerTool
from tools.memory import MemoryTool
from tools.web_search import WebSearchTool
from tools.system_mac import MacSystemTool
from tools.save_skill import SaveSkillTool
from tools.browser import BrowserAutomationTool
from tools.email_tool import SearchEmailTool, SendEmailTool

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
        config_path = get_data_path("config.json")
        if os.path.exists(config_path):
            try:
                import json
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    disabled_skills = config.get("disabled_skills", [])
            except Exception: pass

        # Load skills from the skills directory
        skills_text = ""
        skills_dir = get_skills_dir()
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

        # Store config for later use
        self.sandbox_dir = None
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    if config.get("sandbox_mode", True):
                        self.sandbox_dir = config.get("sandbox_dir", os.path.abspath(os.path.join(os.getcwd(), "workspace")))
            except Exception: pass

        self.skills_text = skills_text
        self.system_prompt_base = (
            f"你是 Open-AGC，一个强大的 AI 智能体，能够执行终端命令、运行 Python 代码、"
            f"操作文件系统，以及物理控制电脑的鼠标和键盘。"
            f"始终使用你的工具来明确验证假设，不要凭空猜测。\n"
            f"\n--- 当前日期与时间 ---\n"
            f"当前时间：{current_time}（{current_date}）\n"
            f"你的训练数据有知识截止日期。对于任何关于近期事件、当前新闻、最新动态或"
            f"时效性信息的问题，你必须使用 search_web 工具获取最新信息。"
            f"绝对不要仅依赖训练数据回答时事问题。\n"
            f"\n重要：处理涉及多个步骤的复杂任务时，先简要说明你的计划，然后逐步执行。"
            f"这样用户能了解你的进展。\n"
            f"\n【文件生成与显示规范（极其重要）】："
            f"1. 你生成的所有文件（脚本、文档、尤其是图片等），如果用户没有显式指定绝对路径，必须统一保存在沙箱工作目录（Sandbox Directory: {{cwd_dir}}）中，严禁写在 /tmp 下。\n"
            f"2. 当你生成了一张图片供用户查看时，请在最终回复中使用 Markdown 语法直观地渲染出来，图片链接使用：`![图片描述](/api/files/生成的文件名.png)` 的格式。这个内部 API 能将你沙箱里的图片直接推送到网页前端显示。\n"
            f"3. 关于网页文件上传：优先使用 `browser_automation`（虚拟浏览器）工具的 `upload` 动作将文件填入网页。但如果遇到了必须通过操作系统原生文件选择框处理的情况，你可以临时切换使用 `computer_control`（键鼠控制工具 / pyautogui）来操作系统的上传弹窗完成文件选择和上传。\n"
            f"\n记忆系统：你拥有智能记忆系统。每次对话开始时，系统会自动检索并展示过去交互中的"
            f"相关记忆。你也可以使用 manage_memory 工具主动管理记忆："
            f"action='add' 保存重要事实、用户偏好和学到的知识；"
            f"action='search' 搜索过去的特定记忆。\n"
            f"\n--- 技能学习系统 ---\n"
            f"当你成功完成了一项之前未完成过的复杂任务，并且得到了用户的正面反馈（例如表扬、感谢等）时，你必须主动询问用户：'是否需要将这次完成任务的过程整理成一项新技能保存下来？'。如果用户明确同意，请使用 `save_learned_skill` 工具将过程整理成 Markdown 格式的技能并保存。\n"
        )
        
        # Initialize smart memory store (replaces old memory.md)
        self.memory_store = MemoryStore(db_path=get_data_path("memory.db"))

        self.messages: List[Dict[str, Any]] = [
            {
                "role": "system",
                "content": self._build_system_prompt()
            }
        ]
        
        # Instantiate tools (MemoryTool shares the same store)
        memory_tool = MemoryTool(db_path=get_data_path("memory.db"))
        self.available_tools = {
            "execute_shell": ShellTool(),
            "read_file": ReadFileTool(),
            "write_file": WriteFileTool(),
            "execute_python": PythonREPLTool(),
            "computer_control": ComputerTool(),
            "manage_memory": memory_tool,
            "search_web": WebSearchTool(),
            "mac_system_action": MacSystemTool(),
            "save_learned_skill": SaveSkillTool(),
            "browser_automation": BrowserAutomationTool(headless=False),
            "search_emails": SearchEmailTool(),
            "send_email": SendEmailTool()
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
            "mac_system_action": "系统操作",
            "save_learned_skill": "保存技能",
            "browser_automation": "虚拟浏览器控制",
            "search_emails": "搜索邮件",
            "send_email": "发送邮件"
        }
        
        # Prepare OpenAI format tool schema
        self.tool_schemas = [tool.get_openai_schema() for tool in self.available_tools.values()]

    def _build_system_prompt(self, memory_context: str = "") -> str:
        # Inject current date/time so the LLM knows "today"
        from datetime import datetime
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        current_date = datetime.now().strftime("%Y年%m月%d日")
        
        prompt = self.system_prompt_base.replace("{current_time}", current_time).replace("{current_date}", current_date)
        prompt = prompt.replace("{cwd_dir}", self.sandbox_dir or os.getcwd())
        
        # Inject Episodic Memory Context
        if memory_context:
            prompt += f"\n--- 历史记忆回溯 (Episodic Memory) ---\n{memory_context}\n"
            
        # Optional: Inject MEMORY.md (Highest priority global rules)
        if self.sandbox_dir:
            memory_file_path = os.path.join(self.sandbox_dir, "MEMORY.md")
            if os.path.exists(memory_file_path):
                try:
                    with open(memory_file_path, "r", encoding="utf-8") as f:
                        content = f.read().strip()
                        if content:
                            prompt += f"\n--- 全局核心设定与事实库 (MEMORY.md) ---\n{content}\n(注意：这是最高优先级的持久化记忆。当用户想传授新规定、修改基础偏好时，请使用 write_file 覆写沙箱目录下的 MEMORY.md)\n"
                except Exception as e:
                    print(f"Failed to read MEMORY.md: {e}")

        if self.skills_text:
            prompt += f"\n以下是你已学会的技能，请遵循执行：\n{self.skills_text}"
            
        return prompt

    def _auto_save_memories(self, user_input: str, assistant_reply: str):
        """
        Automatically extract and save key memories from a conversation turn.
        Runs a lightweight LLM call to determine what's worth remembering.
        """
        # Skip very short or trivial exchanges
        if len(user_input.strip()) < 10 and len(assistant_reply.strip()) < 20:
            return

        extraction_prompt = (
            "你是一个记忆提取助手。根据以下对话内容，判断是否有值得记住的信息以供未来对话使用。\n"
            "值得记住的：用户偏好、项目细节、个人事实、重要指令、过往完成的任务/创作的产出物(如写过的文章、做过的图表、历史分析)、学到的知识。\n"
            "不值得记住的：打招呼、关于通用知识的简单问答、闲聊。\n\n"
            f"用户：{user_input[:500]}\n\n"
            f"助手：{assistant_reply[:500]}\n\n"
            "如果有值得保存的记忆，请用中文回复一个 JSON 数组，每个对象包含：\n"
            "- 'content'：记忆内容（简洁的中文描述）\n"
            "- 'category'：类别（可选值：tech, user_pref, project, knowledge, system, general）\n"
            "- 'memory_type'：记忆类型（可选值：core=长期核心事实如姓名/偏好，"
            "working=近期工作记忆如当前任务，episode=事件记录如学到的知识）\n"
            "如果没有值得保存的内容，回复空数组 []。\n"
            "只回复 JSON，不要有其他文字。"
        )

        try:
            # Use the same model the user has configured (with fallback support)
            response, _ = self.llm.chat(
                messages=[{"role": "user", "content": extraction_prompt}]
            )
            result_text = response.choices[0].message.content.strip()

            # Strip markdown code fences if present
            if result_text.startswith("```"):
                lines = result_text.split("\n")
                result_text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

            memories = json.loads(result_text)
            if isinstance(memories, list):
                for mem in memories:
                    if isinstance(mem, dict) and mem.get("content"):
                        content = mem["content"]
                        category = mem.get("category")
                        memory_type = mem.get("memory_type", "episode")
                        importance = mem.get("importance", 2)

                        # Smart dedup: check for similar existing memory
                        similar = self.memory_store.find_similar(content)
                        if similar:
                            # Merge: update existing memory with richer content
                            merged = f"{similar['content']}；{content}" \
                                if content not in similar['content'] else similar['content']
                            self.memory_store.update_memory(similar["id"], merged)
                        else:
                            self.memory_store.add_memory(
                                content=content,
                                category=category,
                                importance=importance,
                                memory_type=memory_type
                            )

            # Also save a conversation summary
            summary = user_input[:200]
            self.memory_store.save_conversation(
                summary=summary,
                messages=[{"role": "user", "content": user_input},
                          {"role": "assistant", "content": assistant_reply[:500]}]
            )
        except Exception as e:
            print(f"[Agent] Auto-save memories error: {e}")

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
        self.is_interrupted = False
        self.messages.append({"role": "user", "content": user_input})
        
        # Auto-retrieve relevant memories for this query
        recent_context = "\n".join([m["content"] for m in self.messages[-3:] if m["role"] == "user"])
        memory_context = ""
        try:
            results = self.memory_store.search(recent_context, top_k=3)
            if results:
                memory_context = "\n".join([f"- {r['content']} (Type: {r['memory_type']})" for r in results])
        except Exception as e:
            if verbose: print(f"Memory retrieval error: {e}")

        # Ensure System Prompt is always fresh and has the latest MEMORY.md and episodic context
        if self.messages and self.messages[0]["role"] == "system":
            self.messages[0]["content"] = self._build_system_prompt(memory_context=memory_context)
            
        step = 1
        
        # Read agent config for limits
        max_iterations = 30
        config_path = get_data_path("config.json")
        try:
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    max_iterations = config.get("max_iterations", 30)
        except Exception:
            pass

        current_iter = 0
        step_counter = 0

        # Tool loop detection state
        recent_tool_calls = []
        MAX_REPEATED_TOOL_CALLS = 3
        
        while current_iter < max_iterations:
            if self.is_interrupted:
                return "Task interrupted by user."

            current_iter += 1
            if verbose:
                print(f"[Agent Loop Iteration {current_iter}/{max_iterations}] Calling LLM...")
            
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
                    
                    # Tool Loop Detection Check
                    call_signature = f"{function_name}:{function_args}"
                    call_hash = hashlib.md5(call_signature.encode('utf-8')).hexdigest()
                    recent_tool_calls.append(call_hash)
                    
                    # Keep only the last 10 calls in the memory window
                    if len(recent_tool_calls) > 10:
                        recent_tool_calls.pop(0)
                        
                    # Check if the exact same tool with the exact same args was called too many times recently
                    # This often happens when the agent gets stuck in an error loop
                    loop_count = recent_tool_calls.count(call_hash)
                    
                    if loop_count >= MAX_REPEATED_TOOL_CALLS:
                        result = (f"System Guard: Blocked due to critical loop. "
                                  f"You have called `{function_name}` with these exact arguments {loop_count} times recently. "
                                  f"You are likely stuck in a loop. YOU MUST change your approach or use different parameters.")
                        if verbose:
                            print(f"[Tool Loop Detected] Blocked {function_name}")
                    else:
                        if tool_instance:
                            try:
                                result = tool_instance.execute(**function_args)
                            except Exception as e:
                                result = f"Error executing tool: {str(e)}"
                        else:
                            result = f"Error: Tool {function_name} not found."
                    
                    result_str = str(result)
                    
                    # Telemetry & Context Compaction Logic
                    # If the result is extremely long (e.g. reading a massive file), truncate it to save context window.
                    MAX_RESULT_LENGTH = 15000
                    if len(result_str) > MAX_RESULT_LENGTH:
                        half_len = MAX_RESULT_LENGTH // 2
                        result_str = (result_str[:half_len] + 
                                      f"\n\n...[System Guard: Output truncated due to excessive length ({len(result_str)} chars). "
                                      "Please use tools to filter or paginate the results (e.g., grep or head)]...\n\n" + 
                                      result_str[-half_len:])

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
                            "success": not result_str.startswith("Error") and not result_str.startswith("System Guard")
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
                final_answer = message.content
                # Auto-extract & save memories in background thread
                thread = threading.Thread(
                    target=self._auto_save_memories,
                    args=(user_input, final_answer),
                    daemon=True
                )
                thread.start()
                return final_answer
                
        return "Agent stopped: Reached maximum iterations without a final answer."
