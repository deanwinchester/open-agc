import json
import re
import time as _time
import threading
import hashlib
from typing import List, Dict, Any, Optional, Callable
import os

from core.paths import get_data_path, get_skills_dir

from core.llm_client import LLMClient, build_user_message, extract_screenshot_data
from core.logger import SessionLogger
from core.memory_store import MemoryStore
from core.skill_store import SkillStore
from core.token_budget import TokenBudget, estimate_messages_tokens
from core.reflection import ReflectionEngine
from core.knowledge_graph import KnowledgeGraph
from tools.shell import ShellTool
from tools.filesystem import ReadFileTool, WriteFileTool, EditFileTool
from tools.search import GrepSearchTool, GlobTool
from tools.python_repl import PythonREPLTool
from tools.computer import ComputerTool
from tools.memory import MemoryTool
from tools.web_search import WebSearchTool
from tools.system_mac import MacSystemTool
from tools.save_skill import SaveSkillTool
from tools.browser import BrowserAutomationTool
from tools.download import DownloadTool
from tools.email_tool import SearchEmailTool, SendEmailTool
from tools.auto_tool import (DynamicTool, load_all_dynamic_tools,
                              generate_tool_code, validate_tool_code,
                              save_tool_code, init_auto_tools)
from agent.sub_agent import SubAgent, TOOL_SETS

class OpenAGCAgent:
    """
    Main Agent Loop handling context, Tool calling, and orchestration.
    Supports real-time progress callbacks for task tracking.
    Features smart memory with TF-IDF semantic retrieval.
    """
    def __init__(self, model: str = "gpt-4o", session_id: Optional[int] = None,
                 logger: Optional[SessionLogger] = None):
        self.session_id = session_id
        self.logger = logger
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

        # Load skill index (progressive — full content is retrieved on demand)
        self.skill_store = SkillStore(skills_dir=get_skills_dir())

        # Inject current date/time so the LLM knows "today"
        from datetime import datetime
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        current_date = datetime.now().strftime("%Y年%m月%d日")

        # Store config for later use
        self.sandbox_dir = None
        self.browser_headless = False
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    if config.get("sandbox_mode", True):
                        self.sandbox_dir = config.get("sandbox_dir", os.path.abspath(os.path.join(os.getcwd(), "workspace")))
                    self.browser_headless = config.get("browser_headless", False)
                    # Initialize token budget from config if available
                    budget_cfg = config.get("context_budget", {})
            except Exception:
                budget_cfg = {}

        self.token_budget = TokenBudget(config=budget_cfg if budget_cfg else None)

        # Initialize smart memory store before reflection/knowledge engines
        self.memory_store = MemoryStore(
            db_path=get_data_path("memory.db"),
            session_id=self.session_id
        )

        self.reflection_engine = ReflectionEngine(
            db_path=get_data_path("agent.db"),
            memory_store=self.memory_store,
            llm_client=self.llm,
            session_id=self.session_id,
        )
        self.knowledge_graph = KnowledgeGraph(
            db_path=get_data_path("agent.db"),
            session_id=self.session_id,
        )

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
            f"\n【工具调用规范（极其重要）】："
            f"1. 仅当你决定使用工具时，才输出包含 'name' 和 'arguments' 的 JSON 对象。对于正常的对话回复，直接输出纯文本，严禁使用 JSON 格式。\n"
            f"2. 工具调用格式：`{{\"name\": \"execute_shell\", \"arguments\": {{\"command\": \"ls -l\"}}}}`，不要带多余的前缀或后缀。\n"
            f"3. 如果你想在调用工具前表达思考过程，请将其放在 JSON 之前的独立段落中（不带 JSON 结构）。\n"
            f"\n【文件生成与显示规范（极其重要）】："
            f"1. 你生成的所有文件（脚本、文档、尤其是图片等），如果用户没有显式指定绝对路径，必须统一保存在沙箱工作目录（Sandbox Directory: {{cwd_dir}}）中，严禁写在 /tmp 下。\n"
            f"2. 当你生成了一张图片供用户查看时，请在最终回复中使用 Markdown 语法直观地渲染出来，图片链接使用：`![图片描述](/api/files/生成的文件名.png)` 的格式。这个内部 API 能将你沙箱里的图片直接推送到网页前端显示。\n"
            f"3. 关于网页文件上传：优先使用 `browser_automation`（虚拟浏览器）工具的 `upload` 动作将文件填入网页。但如果遇到了必须通过操作系统原生文件选择框处理的情况，你可以临时切换使用 `computer_control`（键鼠控制工具 / pyautogui）来操作系统的上传弹窗完成文件选择和上传。\n"
            f"\n记忆系统：你拥有智能记忆系统。每次对话开始时，系统会自动检索并展示过去交互中的"
            f"相关记忆。你也可以使用 manage_memory 工具主动管理记忆："
            f"action='add' 保存重要事实、用户偏好和学到的知识；"
            f"action='search' 搜索过去的特定记忆。\n"
            f"\n技能系统：我拥有丰富的技能库。在每次任务开始时，系统会根据任务内容自动检索"
            f"并注入相关技能供你参考执行。你也可以主动使用 manage_memory 工具查询和管理技能。\n"
            f"如果你成功完成了一项之前未完成过的复杂任务，并且得到了用户的正面反馈，"
            f"必须主动询问用户是否需要将过程保存为新技能。"
            f"如用户同意，请使用 `save_learned_skill` 工具。\n"
        )
        
        self.messages: List[Dict[str, Any]] = [
            {
                "role": "system",
                "content": self._build_system_prompt()
            }
        ]
        
        # Instantiate tools (MemoryTool shares the same store)
        memory_tool = MemoryTool(
            db_path=get_data_path("memory.db"),
            session_id=self.session_id
        )
        self.available_tools = {
            "execute_shell": ShellTool(),
            "read_file": ReadFileTool(),
            "write_file": WriteFileTool(),
            "edit_file": EditFileTool(),
            "search_file_content": GrepSearchTool(),
            "find_files": GlobTool(),
            "execute_python": PythonREPLTool(),
            "computer_control": ComputerTool(),
            "manage_memory": memory_tool,
            "search_web": WebSearchTool(),
            "mac_system_action": MacSystemTool(),
            "save_learned_skill": SaveSkillTool(),
            "browser_automation": BrowserAutomationTool(headless=self.browser_headless),
            "search_emails": SearchEmailTool(),
            "send_email": SendEmailTool(),
            "queue_download": DownloadTool()
        }

        # Tool display names (Chinese-friendly)
        self.tool_display_names = {
            "execute_shell": "执行终端命令",
            "read_file": "读取文件",
            "write_file": "写入文件",
            "edit_file": "局部修改文件",
            "search_file_content": "搜索文件内容",
            "find_files": "查找文件",
            "execute_python": "运行 Python 代码",
            "computer_control": "操控电脑",
            "manage_memory": "管理记忆",
            "search_web": "搜索网页",
            "mac_system_action": "系统操作",
            "save_learned_skill": "保存技能",
            "browser_automation": "虚拟浏览器控制",
            "search_emails": "搜索邮件",
            "send_email": "发送邮件",
            "queue_download": "下载文件"
        }

        # Load auto-generated tools (persisted from previous sessions)
        # Store in data/auto_tools/{session_id} to isolate per session
        if self.session_id is not None:
            user_gen_dir = get_data_path(f"auto_tools/{self.session_id}")
        else:
            user_gen_dir = get_data_path("auto_tools")
        init_auto_tools(user_gen_dir)
        loaded = load_all_dynamic_tools(user_gen_dir)
        for tool_name, tool_instance in loaded.items():
            if tool_name not in self.available_tools:
                self.available_tools[tool_name] = tool_instance
                self.tool_display_names[tool_name] = tool_instance.description[:20]

        # Prepare OpenAI format tool schema
        self.tool_schemas = [tool.get_openai_schema() for tool in self.available_tools.values()]

        # Track which skills were injected in the current turn for feedback loop
        self._active_skills: List[str] = []

    def _build_system_prompt(self, memory_context: str = "", skill_context: str = "",
                             experience_context: str = "", kg_context: str = "") -> str:
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

        # Inject relevant skills (progressive — only what's needed for this turn)
        if skill_context:
            prompt += f"\n{skill_context}"

        # Inject prior experience (reflections + trajectories)
        if experience_context:
            prompt += f"\n\n{experience_context}"

        # Inject knowledge graph context
        if kg_context:
            prompt += f"\n\n{kg_context}"

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

    def _record_skill_feedback(self, success: bool, task_input: str = "",
                                duration: float = 0):
        """Update skill usage stats and generate reflection based on task outcome."""
        if self._active_skills:
            for filename in self._active_skills:
                try:
                    self.skill_store.record_usage(filename, success=success)
                except Exception as e:
                    print(f"[Agent] Skill feedback error for {filename}: {e}")
        # Generate reflection in background thread
        if task_input and self.reflection_engine:
            try:
                self.reflection_engine.generate_reflection(
                    task_input=task_input,
                    messages=self.messages,
                    success=success,
                    duration_seconds=duration,
                )
            except Exception as e:
                print(f"[Agent] Reflection error: {e}")

    # Task categories for adaptive config
    TASK_CATEGORIES = {
        "code": {
            "keywords": ["写代码", "编程", "实现", "开发", "python", "javascript",
                         "create", "implement", "coding", "programming"],
            "config": {"max_iterations": 20, "temperature": 0.1}
        },
        "deploy": {
            "keywords": ["部署", "上线", "发布", "deploy", "release", "publish",
                         "启动服务", "安装"],
            "config": {"max_iterations": 50, "temperature": 0.2}
        },
        "analysis": {
            "keywords": ["分析", "检查", "审查", "review", "analyze", "audit",
                         "统计", "报告"],
            "config": {"max_iterations": 15, "temperature": 0.3}
        },
        "research": {
            "keywords": ["搜索", "查找", "研究", "调查", "search", "research",
                         "find", "what is", "how to"],
            "config": {"max_iterations": 10, "temperature": 0.5}
        },
        "creative": {
            "keywords": ["写文章", "设计", "创作", "write", "design", "create content",
                         "生成图片"],
            "config": {"max_iterations": 15, "temperature": 0.7}
        },
        "filesystem": {
            "keywords": ["整理文件", "重命名", "移动", "复制", "organize", "rename",
                         "move", "copy", "clean"],
            "config": {"max_iterations": 10, "temperature": 0.1}
        },
    }

    def _classify_task(self, user_input: str) -> dict:
        """Classify user input into a task category and return adaptive config."""
        text = user_input.lower()
        for category, rules in self.TASK_CATEGORIES.items():
            if any(kw in text for kw in rules["keywords"]):
                return rules["config"]
        return {"max_iterations": 30, "temperature": 0.3}

    def _register_dynamic_tool(self, tool_name: str, tool_instance) -> bool:
        """Register a dynamically created tool so the LLM can call it."""
        if tool_name in self.available_tools:
            return False
        self.available_tools[tool_name] = tool_instance
        self.tool_display_names[tool_name] = tool_instance.description[:20]
        self.tool_schemas = [t.get_openai_schema() for t in self.available_tools.values()]
        return True

    def _auto_generate_tool(self, task_input: str, trajectory, llm_client) -> Optional[str]:
        """Try to generate a reusable tool from a successful trajectory."""
        tool_sequence = trajectory.get("tool_sequence", "")
        tool_count = tool_sequence.count("\n→ ")
        if tool_count < 5:
            return None

        code = generate_tool_code(task_input, tool_sequence,
                                   "Success", llm_client)
        if not code or not validate_tool_code(code):
            return None

        # Extract name from TOOL_SCHEMA
        import ast
        try:
            tree = ast.parse(code)
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "TOOL_SCHEMA":
                            if isinstance(node.value, ast.Dict):
                                for k, v in zip(node.value.keys, node.value.values):
                                    if isinstance(k, ast.Str) and k.s == "name" and isinstance(v, ast.Str):
                                        tool_name = v.s
                                        break
        except Exception:
            return None

        if not tool_name:
            return None

        filepath = save_tool_code(code, tool_name)
        if not filepath:
            return None

        from tools.auto_tool import load_dynamic_tool
        tool_instance = load_dynamic_tool(filepath)
        if not tool_instance:
            return None

        self._register_dynamic_tool(tool_name, tool_instance)
        return tool_name

    def _should_delegate(self, user_input: str) -> bool:
        """Assess whether a task is complex enough to warrant sub-agent delegation."""
        text = user_input.lower()
        # Check for decomposition keywords
        complexity_keywords = [
            "分别", "同时", "多个", "所有", "each", "all", "every",
            "first.*then", "先.*再", "先.*然后",
            "部署", "deploy", "migrate", "迁移",
        ]
        match_count = 0
        for kw in complexity_keywords:
            if re.search(kw, text):
                match_count += 1

        # Check if it spans multiple areas
        area_count = 0
        for area_kws in TOOL_SETS.values():
            if any(kw in text for kw in area_kws):
                area_count += 1

        # Delegate if high complexity or multi-domain
        return match_count >= 2 or area_count >= 3 or len(text) > 200

    def _decompose_task(self, task_input: str) -> List[Dict]:
        """Use LLM to decompose a complex task into sub-tasks."""
        prompt = f"""将以下任务分解为可执行子任务。

任务：{task_input}

要求：
- 每个子任务应独立、可完成
- 为每个子任务标注需要的工具类型（可选：filesystem, code, web, analysis, deploy, research）
- 标注子任务间的依赖关系（depends_on 为依赖的子任务 id 列表）

输出 JSON 数组，格式：
[{{"id": 1, "task": "子任务描述", "tools": ["filesystem"], "depends_on": [], "max_iterations": 10}}]

只输出 JSON 数组，不要额外说明。"""

        try:
            response, _ = self.llm.chat(
                messages=[{"role": "user", "content": prompt}]
            )
            text = response.choices[0].message.content.strip()

            # Extract JSON from markdown if present
            json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
            if json_match:
                text = json_match.group(1).strip()

            plans = json.loads(text)
            if isinstance(plans, list):
                # Resolve tool set names to actual tool names
                for plan in plans:
                    tool_names = plan.get("tools", ["filesystem"])
                    resolved = []
                    for t in tool_names:
                        resolved.extend(TOOL_SETS.get(t, [t]))
                    plan["tools"] = list(set(resolved))
                return plans
        except Exception as e:
            print(f"[Agent] Task decomposition failed: {e}")

        return []

    def _synthesize_results(self, user_input: str,
                            sub_results: List[Dict]) -> str:
        """Combine sub-agent results into a coherent final answer."""
        parts = [f"## 任务完成报告\n\n原始任务：{user_input}\n"]

        for i, result in enumerate(sub_results, 1):
            status = "✅ 成功" if result.get("success") else "❌ 失败"
            summary = result.get("summary", "无摘要")[:500]
            duration = result.get("duration", 0)
            parts.append(f"### 子任务 {i} {status}（{duration:.1f}s）\n{summary}\n")

        return "\n".join(parts)

    def _compress_tool_result(self, result: str, tool_name: str) -> str:
        """Compress long tool results to preserve context window.

        Strategy (tiered):
          1. < 3000 chars → keep as-is
          2. 3000–15000 → extractive: keep head + key lines (error, traceback, etc.)
          3. > 15000    → extractive compression (guaranteed < 8000 chars)
        """
        COMPRESS_THRESHOLD = 3000    # chars — below this, no compression needed
        EXTRACTIVE_TARGET = 8000     # chars — target for extractive pass

        if len(result) <= COMPRESS_THRESHOLD:
            return result

        lines = result.split("\n")

        # Scoring function for important lines
        def _line_score(line: str) -> int:
            low = line.lower()
            score = 0
            if any(kw in low for kw in ("error", "exception", "traceback", "fail", "trace ")):
                score += 5
            if any(kw in low for kw in ("exit code", "returncode", "status", "result")):
                score += 3
            if any(kw in low for kw in ("file", "path", "dir", "found", "missing")):
                score += 2
            if any(c.isdigit() for c in line):
                score += 1  # lines with numbers tend to carry metrics
            # Penalize very long lines (often raw data dumps)
            if len(line) > 300:
                score -= 2
            return score

        # Always keep first 15 lines (command echo, header)
        head = lines[:15]
        tail = lines[-5:]  # last 5 lines (exit code, summary)

        # Score and pick important lines from the middle
        middle = lines[15:-5] if len(lines) > 20 else []
        scored_lines = [(i, _line_score(l), l) for i, l in enumerate(middle, start=15)]
        scored_lines.sort(key=lambda x: -x[1])

        # Keep lines with score ≥ 3 (high importance), limit to avoid blowup
        important = [(i, l) for i, s, l in scored_lines if s >= 3]

        # Also keep any line matching common output patterns (table borders, section headers)
        section_lines = []
        for i, l in enumerate(middle):
            if i + 15 not in {idx for idx, _ in important}:
                if re.search(r'^[-|=+|]{5,}|^#{1,3}\s', l):
                    section_lines.append((i + 15, l))

        # Build compressed result
        compressed_lines = []

        # Head section
        original_head = len(head)
        compressed_lines.extend(head)

        # If there's important middle content, annotate
        if important or section_lines:
            compressed_lines.append(f"─── key output ({len(important)} important lines) ───")
            seen = set()
            for idx, l in sorted(important + section_lines):
                if l not in seen:
                    compressed_lines.append(l)
                    seen.add(l)

            # Show count of omitted lines
            omitted = len(lines) - original_head - len(tail) - len(seen)
            if omitted > 0:
                compressed_lines.append(f"─── {omitted} lines omitted ───")

        # Tail section
        compressed_lines.extend(tail)

        compressed = "\n".join(compressed_lines)

        # If still over target, do a second pass: just keep head + tail
        if len(compressed) > EXTRACTIVE_TARGET:
            compressed = "\n".join(head + [f"─── {len(lines) - original_head - len(tail)} lines omitted ───"] + tail)

        # If still over COMPRESS_THRESHOLD after extraction (edge case), brute-force
        if len(compressed) > COMPRESS_THRESHOLD * 2:
            half = COMPRESS_THRESHOLD
            compressed = (compressed[:half] +
                          f"\n...[truncated {len(result)} chars to {half * 2}]...\n" +
                          compressed[-half:])

        return (
            f"[Compressed: {len(result)} chars → {len(compressed)} chars | "
            f"original tool: {tool_name}]\n"
            f"{compressed}"
        )

    def _fold_tool_calls(self, messages: List[Dict]) -> List[Dict]:
        """Fold consecutive tool-call rounds exceeding the threshold into a summary.

        Long chains of tool_call → tool_result → tool_call → tool_result consume
        context window without adding decision value.  Older rounds are replaced
        by a concise execution log.
        """
        FOLD_AFTER_N = 8       # total rounds before folding kicks in
        KEEP_LAST_N = 4        # always keep this many most-recent rounds intact

        # Identify tool-call round boundaries [(start, end), ...]
        bounds: List[tuple] = []
        i = 0
        while i < len(messages):
            m = messages[i]
            if m.get("role") == "assistant" and m.get("tool_calls"):
                start = i
                i += 1
                while i < len(messages) and messages[i].get("role") == "tool":
                    i += 1
                bounds.append((start, i))
            else:
                i += 1

        if len(bounds) <= FOLD_AFTER_N:
            return messages

        fold_bounds = bounds[:-KEEP_LAST_N]   # rounds to summarise
        keep_bounds = bounds[-KEEP_LAST_N:]   # rounds to preserve verbatim

        # Build summary lines from the rounds being folded
        summary_lines = []
        for idx, (start, end) in enumerate(fold_bounds, 1):
            msg = messages[start]
            for tc in (msg.get("tool_calls") or []):
                if not isinstance(tc, dict):
                    continue
                name = tc.get("function", {}).get("name", "?")
                args_raw = tc.get("function", {}).get("arguments", "{}")
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                except Exception:
                    args = {}
                preview = ", ".join(
                    f"{k}={str(v)[:40]}" for k, v in list(args.items())[:2]
                )
                summary_lines.append(f"  {idx}. {name}({preview})")
            # Check tool results in this round for errors
            for j in range(start + 1, end):
                content = str(messages[j].get("content", ""))
                if content.startswith("Error") or "traceback" in content.lower()[:300]:
                    if summary_lines:
                        summary_lines[-1] += " ⚠️"

        cut_idx = fold_bounds[0][0]          # first message of the oldest folded round
        keep_idx = keep_bounds[0][0]          # first message of the first kept round

        summary_text = (
            f"[以下 {len(fold_bounds)} 轮工具调用已折叠为摘要，保留最近 {len(keep_bounds)} 轮]\n"
            + "\n".join(summary_lines)
        )

        pruned = messages[:cut_idx]
        pruned.append({"role": "assistant", "content": summary_text})
        pruned.extend(messages[keep_idx:])
        return pruned

    def run_turn(self, user_input: str, verbose: bool = False,
                 progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
                 images: Optional[List[str]] = None) -> str:
        """
        Run a single conversational turn. Will loop until the LLM returns a final text message.

        Args:
            user_input: The user's message.
            verbose: If true, print debug info.
            progress_callback: Optional callback for real-time progress updates.
            images: Optional list of image file paths or data URLs to include as vision input.
        """
        self.is_interrupted = False
        self.messages.append(build_user_message(user_input, images))
        _task_start = _time.time()

        if self.logger:
            self.logger.log_user_query(user_input)

        # Auto-retrieve relevant memories for this query
        def _msg_text(m):
            c = m.get("content", "")
            if isinstance(c, list):
                return " ".join(p.get("text", "") for p in c if p.get("type") == "text")
            return c

        recent_context = "\n".join([_msg_text(m) for m in self.messages[-3:] if m["role"] == "user"])
        memory_context = ""
        try:
            results = self.memory_store.search_memories(recent_context, top_k=3)
            if results:
                memory_context = "\n".join([f"- {r['content']} (Type: {r['memory_type']})" for r in results])
        except Exception as e:
            if verbose: print(f"Memory retrieval error: {e}")

        # Auto-retrieve relevant skills for this query
        skill_context = ""
        self._active_skills = []
        try:
            self.skill_store.refresh()
            matched_skills = self.skill_store.retrieve(recent_context, top_k=3)
            if matched_skills:
                self._active_skills = [s["filename"] for s in matched_skills]
                skill_context = self.skill_store.format_skills_for_prompt(matched_skills)
        except Exception as e:
            if verbose: print(f"Skill retrieval error: {e}")

        # Retrieve relevant past experience (reflections + trajectories)
        experience_context = ""
        try:
            experience = self.reflection_engine.retrieve_experience(recent_context, top_k=2)
            if experience.get("reflections") or experience.get("trajectories"):
                experience_context = self.reflection_engine.format_experience_for_prompt(experience)
        except Exception as e:
            if verbose: print(f"Experience retrieval error: {e}")

        # Retrieve knowledge graph context
        kg_context = ""
        try:
            kg_results = self.knowledge_graph.retrieve_context(recent_context, top_k=5)
            if kg_results:
                kg_context = self.knowledge_graph.format_context(kg_results)
        except Exception as e:
            if verbose: print(f"Knowledge graph retrieval error: {e}")

        # Ensure System Prompt is always fresh and has the latest MEMORY.md and episodic context
        if self.messages and self.messages[0]["role"] == "system":
            self.messages[0]["content"] = self._build_system_prompt(
                memory_context=memory_context,
                skill_context=skill_context,
                experience_context=experience_context,
                kg_context=kg_context,
            )

        # Sub-agent delegation for complex tasks
        if self._should_delegate(user_input):
            if verbose: print(f"[Agent] Delegating to sub-agents...")
            plans = self._decompose_task(user_input)
            if plans:
                sub_results = []
                completed = set()
                # Execute sub-agents respecting dependency order
                remaining = list(plans)
                max_rounds = len(plans) * 2
                for _ in range(max_rounds):
                    if not remaining:
                        break
                    batch = [p for p in remaining if all(d in completed for d in p.get("depends_on", []))]
                    if not batch:
                        break  # Circular dependency or unresolvable
                    for plan in batch:
                        remaining.remove(plan)
                        sub = SubAgent(
                            task=plan["task"],
                            tools=plan.get("tools", ["execute_shell"]),
                            parent_tools=self.available_tools,
                            max_iterations=plan.get("max_iterations", 10),
                            progress_callback=progress_callback,
                            llm_client=self.llm,
                        )
                        result = sub.run()
                        sub_results.append(result)
                        if result.get("success"):
                            completed.add(plan["id"])
                        else:
                            # Abort on failure if dependencies chain
                            dep_ids = {plan["id"]}
                            remaining = [p for p in remaining if not (dep_ids & set(p.get("depends_on", [])))]
                            break
                result_text = self._synthesize_results(user_input, sub_results)
                self.messages.append({"role": "assistant", "content": result_text})
                return result_text

        step = 1

        # Adaptive config: task-category-based tuning (overridable in config.json)
        adaptive = self._classify_task(user_input)
        max_iterations = adaptive.get("max_iterations", 30)
        config_path = get_data_path("config.json")
        try:
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    # Explicit config value always wins
                    if "max_iterations" in config:
                        max_iterations = config["max_iterations"]
        except Exception:
            pass

        current_iter = 0
        step_counter = 0

        # Tool loop detection state
        recent_tool_calls = []
        MAX_REPEATED_TOOL_CALLS = 3
        
        while current_iter < max_iterations:
            if self.is_interrupted:
                self._record_skill_feedback(success=False, task_input=user_input,
                                            duration=_time.time() - _task_start)
                return "Task interrupted by user."

            current_iter += 1
            if verbose:
                print(f"[Agent Loop Iteration {current_iter}/{max_iterations}] Calling LLM...")
            
            # Notify: thinking
            if progress_callback:
                progress_callback({"event": "thinking", "iteration": current_iter})
            
            response, actual_model = self.llm.chat(messages=self.messages, tools=self.tool_schemas)
            message = response.choices[0].message
            
            # Detect empty response (no content and no tool calls)
            # This can happen with some models like Ollama's Qwen when they malfunction or refuse to answer.
            if not message.content and not message.tool_calls:
                error_msg = f"[Agent] Model {actual_model} returned an empty response (no content and no tool calls). Breaking loop."
                if verbose:
                    print(error_msg)
                # Save as a system message to history to explain why it stopped
                self.messages.append({"role": "assistant", "content": "Error: Empty response from model. Please check the model logs or try a different model."})
                return "Agent stopped: Received an empty response from the model. This usually indicates a model failure or refusal."
            
            # Notify if model was switched
            if progress_callback:
                if actual_model != self.llm.default_model:
                    progress_callback({
                        "event": "model_switched",
                        "from": self.llm.default_model,
                        "to": actual_model
                    })
                
                # Check for reasoning_content (Thinking process)
                reasoning = getattr(message, 'reasoning_content', None)
                if reasoning:
                    progress_callback({
                        "event": "thinking",
                        "iteration": current_iter,
                        "content": reasoning
                    })
            
            # Append model's response to history
            message_dict = message.model_dump()
            self.messages.append(message_dict)
            
            # 1. Check if model decided to use tools
            tool_calls = message.tool_calls
            
            # --- Tool Call Rescue Logic ---
            # If the model failed to use the API correctly and returned raw JSON text instead
            if not tool_calls and message.content:
                try:
                    # Look for JSON block wrapped in markdown or raw JSON
                    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', message.content, re.DOTALL)
                    json_str = match.group(1) if match else message.content.strip()
                    
                    if json_str.startswith('{') and json_str.endswith('}'):
                        parsed = json.loads(json_str)
                        # Check if it looks like a tool call signature
                        if "name" in parsed and "arguments" in parsed:
                            import uuid
                            
                            class MockFunction:
                                def __init__(self, name, arguments):
                                    self.name = name
                                    # LiteLLM expects arguments as stringified JSON
                                    self.arguments = json.dumps(arguments) if isinstance(arguments, dict) else str(arguments)
                            
                            class MockToolCall:
                                def __init__(self, id, function):
                                    self.id = id
                                    self.function = function
                                    self.type = "function"
                                    
                            call_id = f"call_{uuid.uuid4().hex[:10]}"
                            mock_call = MockToolCall(id=call_id, function=MockFunction(parsed["name"], parsed["arguments"]))
                            tool_calls = [mock_call]
                            message.tool_calls = tool_calls  # Patch the original message so history is consistent
                            if verbose:
                                print(f"[Agent] 🚨 Rescued raw JSON into tool call: {parsed['name']}")
                except Exception as e:
                    pass
            # ------------------------------
            if tool_calls:
                screenshot_urls = []
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
                                import inspect
                                sig = inspect.signature(tool_instance.execute)
                                if 'interrupt_check' in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                                    result = tool_instance.execute(
                                        interrupt_check=lambda: self.is_interrupted,
                                        **function_args
                                    )
                                else:
                                    result = tool_instance.execute(**function_args)
                                tool_success = True
                            except Exception as e:
                                result = f"Error executing tool: {str(e)}"
                                tool_success = False
                        else:
                            result = f"Error: Tool {function_name} not found."
                            tool_success = False

                    if self.logger:
                        self.logger.log_tool_call(function_name, function_args)
                        self.logger.log_tool_result(function_name, str(result), tool_success)

                    result_str = str(result)

                    # Context Compaction: compress long tool results to preserve context window
                    result_str = self._compress_tool_result(result_str, function_name)

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

                    # Collect screenshot data for vision injection
                    url = extract_screenshot_data(result_str)
                    if url:
                        screenshot_urls.append(url)

                # After all tool results in this iteration, inject screenshot vision observations
                for url in screenshot_urls:
                    self.messages.append({
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "[工具执行截图 — 请根据此截图内容继续后续操作]"},
                            {"type": "image_url", "image_url": {"url": url}}
                        ]
                    })

                # Context budget: prune messages if approaching token limit
                try:
                    pruned = self.token_budget.prune_messages(self.messages)
                    if len(pruned) < len(self.messages):
                        pruned_count = len(self.messages) - len(pruned)
                        if verbose:
                            print(f"[Agent] Budget pruned {pruned_count} messages")
                        self.messages = pruned
                except Exception as e:
                    if verbose:
                        print(f"[Agent] Budget pruning error: {e}")

                # Fold excessive tool call rounds into a summary
                try:
                    folded = self._fold_tool_calls(self.messages)
                    if len(folded) < len(self.messages):
                        folded_count = len(self.messages) - len(folded)
                        if verbose:
                            print(f"[Agent] Folded {folded_count} tool-call messages")
                        self.messages = folded
                except Exception as e:
                    if verbose:
                        print(f"[Agent] Tool folding error: {e}")

                # After appending all tool results, the loop continues to send them back to LLM
                continue
                
            # 2. Check if model provided a text response (final answer)
            if message.content:
                final_answer = message.content
                if self.logger:
                    self.logger.log_agent_response(final_answer)
                # Auto-extract & save memories in background thread
                thread = threading.Thread(
                    target=self._auto_save_memories,
                    args=(user_input, final_answer),
                    daemon=True
                )
                thread.start()
                # Extract knowledge graph entities from this turn's messages
                try:
                    self.knowledge_graph.extract_from_messages(self.messages)
                except Exception as e:
                    print(f"[Agent] KG extraction error: {e}")
                self._record_skill_feedback(success=True, task_input=user_input,
                                            duration=_time.time() - _task_start)
                # Auto-generate tool from successful complex trajectory
                try:
                    tool_seq = self.reflection_engine._extract_tool_sequence(self.messages)
                    traj = {"tool_sequence": tool_seq}
                    tool_name = self._auto_generate_tool(user_input, traj, self.llm)
                    if tool_name:
                        print(f"[Agent] Auto-generated tool: {tool_name}")
                except Exception as e:
                    print(f"[Agent] Auto-tool generation error: {e}")
                return final_answer

        # Extract knowledge graph entities even on failure
        try:
            self.knowledge_graph.extract_from_messages(self.messages)
        except Exception as e:
            print(f"[Agent] KG extraction error: {e}")
        self._record_skill_feedback(success=False, task_input=user_input,
                                    duration=_time.time() - _task_start)
        return "[MAX_ITERATIONS_REACHED] Agent stopped: Reached maximum iterations without a final answer. The task may be incomplete."
