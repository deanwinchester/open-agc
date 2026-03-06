"""
Skill Manager — Import, validate, auto-learn, and optimize skills.
Skills are markdown files in the skills/ directory.
"""
import os
import re
import json
import shutil
from datetime import datetime
from typing import List, Dict, Optional, Tuple


# ---- Dangerous patterns for malicious skill detection ----

DANGER_PATTERNS = [
    # Destructive commands
    (r'rm\s+-rf\s+/', "Recursive delete of system root"),
    (r'rm\s+-rf\s+\*', "Recursive delete wildcard"),
    (r'mkfs\b', "Disk formatting command"),
    (r'dd\s+if=.*of=/dev/', "Raw disk overwrite"),
    (r':(){ :\|:& };:', "Fork bomb"),
    # Privilege escalation
    (r'sudo\s+chmod\s+777\s+/', "Root permission change"),
    (r'sudo\s+su\b', "Switch to root user"),
    (r'chmod\s+\+s\b', "Set SUID bit"),
    # Data exfiltration
    (r'curl\s+.*\|\s*sh', "Download and execute remote script"),
    (r'wget\s+.*\|\s*sh', "Download and execute remote script"),
    (r'curl\s+.*-d\s+@', "Upload local file via curl"),
    (r'nc\s+-l', "Netcat listener (reverse shell)"),
    (r'bash\s+-i\s+>&\s*/dev/tcp/', "Reverse shell"),
    # Credential theft
    (r'cat\s+.*(passwd|shadow|\.ssh|\.env|credentials|\.aws)', "Reading credential files"),
    (r'export\s+.*API_KEY.*=', "Overwriting API keys"),
    (r'echo\s+.*>>\s*~/\.bashrc', "Modifying shell profile"),
    # Crypto mining
    (r'xmrig|cryptonight|minerd', "Cryptocurrency mining"),
    # Network attacks
    (r'nmap\s+-s', "Network port scanning"),
    (r'hydra|medusa|john\b', "Password cracking tools"),
]

WARNING_PATTERNS = [
    (r'sudo\b', "Uses sudo (elevated privileges)"),
    (r'eval\(', "Uses eval (code injection risk)"),
    (r'exec\(', "Uses exec (code injection risk)"),
    (r'os\.system\(', "Direct OS command execution"),
    (r'subprocess\.', "Subprocess execution"),
    (r'requests\.post\(', "Makes HTTP POST requests"),
    (r'open\(.*[\'"]w', "Writes to files"),
    (r'shutil\.rmtree', "Removes directory trees"),
]


class SkillManager:
    """Manages skill lifecycle: import, validate, learn, optimize."""
    
    def __init__(self, skills_dir: str = None):
        if skills_dir is None:
            from core.paths import get_skills_dir
            skills_dir = get_skills_dir()
        self.skills_dir = skills_dir
        os.makedirs(skills_dir, exist_ok=True)
    
    def list_skills(self) -> List[Dict]:
        """List all available skills."""
        skills = []
        for filename in sorted(os.listdir(self.skills_dir)):
            if filename.endswith(".md"):
                filepath = os.path.join(self.skills_dir, filename)
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        content = f.read()
                    
                    # Extract title from first heading
                    title_match = re.search(r'^#\s+(.+)', content, re.MULTILINE)
                    title = title_match.group(1) if title_match else filename
                    
                    size = os.path.getsize(filepath)
                    modified = datetime.fromtimestamp(os.path.getmtime(filepath)).isoformat()
                    
                    skills.append({
                        "filename": filename,
                        "title": title,
                        "size": size,
                        "modified": modified,
                        "lines": content.count('\n') + 1
                    })
                except Exception as e:
                    skills.append({"filename": filename, "error": str(e)})
        
        return skills

    def validate_skill(self, content: str) -> Dict:
        """
        Validate a skill for security. Returns:
        {
            "safe": bool,
            "level": "safe" | "warning" | "danger",
            "issues": [{"pattern": ..., "description": ..., "severity": ...}]
        }
        """
        issues = []
        
        # Check dangerous patterns
        for pattern, desc in DANGER_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                issues.append({
                    "pattern": pattern,
                    "description": desc,
                    "severity": "danger"
                })
        
        # Check warning patterns
        for pattern, desc in WARNING_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                issues.append({
                    "pattern": pattern,
                    "description": desc,
                    "severity": "warning"
                })
        
        # Determine overall level
        has_danger = any(i["severity"] == "danger" for i in issues)
        has_warning = any(i["severity"] == "warning" for i in issues)
        
        if has_danger:
            level = "danger"
        elif has_warning:
            level = "warning"
        else:
            level = "safe"
        
        return {
            "safe": not has_danger,
            "level": level,
            "issues": issues
        }

    def import_skill(self, filename: str, content: str, force: bool = False) -> Dict:
        """
        Import a skill file after security validation.
        Returns {"success": bool, "message": ..., "validation": ...}
        """
        # Normalize filename
        if not filename.endswith(".md"):
            filename += ".md"
        
        # Validate
        validation = self.validate_skill(content)
        
        if validation["level"] == "danger" and not force:
            return {
                "success": False,
                "message": f"Skill rejected: contains {len([i for i in validation['issues'] if i['severity']=='danger'])} dangerous patterns. Use force=True to override.",
                "validation": validation
            }
        
        # Save the skill
        filepath = os.path.join(self.skills_dir, filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        
        return {
            "success": True,
            "message": f"Skill '{filename}' imported successfully.",
            "validation": validation
        }

    def delete_skill(self, filename: str) -> bool:
        filepath = os.path.join(self.skills_dir, filename)
        if os.path.exists(filepath):
            os.remove(filepath)
            return True
        return False

    def learn_from_conversation(self, messages: list, llm_client=None) -> Optional[str]:
        """
        Analyze a conversation and propose a skill if a reusable pattern is found.
        
        Returns the proposed skill content as markdown, or None if nothing to learn.
        Uses simple heuristics if no LLM client is provided.
        """
        # Extract tool usage patterns
        tool_calls = []
        for msg in messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    tool_calls.append({
                        "name": tc.get("function", {}).get("name", ""),
                        "args": tc.get("function", {}).get("arguments", "{}")
                    })
        
        if len(tool_calls) < 2:
            return None  # Not enough tool usage to form a skill
        
        # Count tool usage frequency
        tool_freq = {}
        for tc in tool_calls:
            name = tc["name"]
            tool_freq[name] = tool_freq.get(name, 0) + 1
        
        # If LLM is available, ask it to generate a skill
        if llm_client:
            try:
                conversation_summary = []
                for msg in messages:
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    if role in ("user", "assistant") and content:
                        conversation_summary.append(f"{role}: {content[:200]}")
                
                summary_text = "\n".join(conversation_summary[-10:])
                tools_used = ", ".join(f"{k}(x{v})" for k, v in tool_freq.items())
                
                prompt = [
                    {"role": "system", "content": (
                        "You are a skill generator. Analyze the conversation and extract a REUSABLE skill. "
                        "Output a markdown skill file with: title, when-to-use, step-by-step instructions. "
                        "Only generate a skill if there's a clear, repeatable pattern. "
                        "If nothing worth saving, respond with 'NO_SKILL'. "
                        "Write in the same language as the conversation."
                    )},
                    {"role": "user", "content": (
                        f"Conversation summary:\n{summary_text}\n\n"
                        f"Tools used: {tools_used}\n\n"
                        "Generate a reusable skill markdown, or respond NO_SKILL."
                    )}
                ]
                
                response, _ = llm_client.chat(prompt)
                skill_content = response.choices[0].message.content
                
                if "NO_SKILL" in skill_content:
                    return None
                
                return skill_content
                
            except Exception as e:
                print(f"[SkillManager] LLM skill generation failed: {e}")
                return None
        
        # Heuristic-based skill generation (no LLM)
        if len(tool_calls) >= 3:
            # Generate a simple template
            steps = []
            for i, tc in enumerate(tool_calls[:5], 1):
                name = tc["name"]
                try:
                    args = json.loads(tc["args"])
                    arg_desc = ", ".join(f"{k}={v}" for k, v in list(args.items())[:2])
                except:
                    arg_desc = ""
                steps.append(f"{i}. 使用 `{name}` 工具{': ' + arg_desc if arg_desc else ''}")
            
            steps_text = '\n'.join(steps)
            skill_md = f"""# 自动学习的技能
当用户需要执行类似任务时：
{steps_text}
"""
            return skill_md
        
        return None

    def auto_save_skill(self, skill_content: str) -> Optional[str]:
        """Auto-save a learned skill with a timestamped filename."""
        if not skill_content:
            return None
        
        # Validate before saving
        validation = self.validate_skill(skill_content)
        if validation["level"] == "danger":
            print("[SkillManager] Auto-learned skill rejected: contains dangerous patterns")
            return None
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"auto_learned_{timestamp}.md"
        filepath = os.path.join(self.skills_dir, filename)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(skill_content)
        
        return filename
