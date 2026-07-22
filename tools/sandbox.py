import os
import subprocess
import uuid
import shutil
from typing import Any, Dict, Optional
from tools.base import BaseTool

class EnterWorktreeTool(BaseTool):
    name: str = "enter_sandbox_mode"
    description: str = (
        "创建 git worktree 隔离沙箱，在其中安全修改代码或运行高风险命令。"
        "需要隔离验证、避免影响主项目时使用；完成后用 exit_sandbox_mode 退出。"
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
                        "branch_name": {
                            "type": "string",
                            "description": "可选分支名，留空则自动生成。"
                        }
                    }
                }
            }
        }

    def execute(self, **kwargs) -> str:
        agent_ctx = kwargs.get("_agent_context")
        if not agent_ctx:
            return "Error: Agent context missing."
            
        # 1. Identify the root repository path
        # Use config or the current cwd
        original_sandbox = getattr(agent_ctx, "original_sandbox_dir", None)
        if not original_sandbox:
            # First time entering, save original
            # We assume current sandbox_dir (from config) is the base repo
            from core.paths import get_data_path
            import json
            config_path = get_data_path("config.json")
            with open(config_path, "r") as f:
                config = json.load(f)
            original_sandbox = config.get("sandbox_dir", os.path.abspath("workspace"))
            agent_ctx.original_sandbox_dir = original_sandbox

        try:
            # Check if it's a git repo
            subprocess.check_call(["git", "rev-parse", "--is-inside-work-tree"], cwd=original_sandbox, 
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except:
            return f"Error: Sandbox directory '{original_sandbox}' is not a Git repository. Sandbox mode requires Git."

        # 2. Create unique sandbox path
        task_id = uuid.uuid4().hex[:8]
        branch = kwargs.get("branch_name") or f"sandbox-{task_id}"
        sandbox_parent = os.path.abspath(os.path.join(original_sandbox, "..", ".open_agc_sandboxes"))
        os.makedirs(sandbox_parent, exist_ok=True)
        sandbox_path = os.path.join(sandbox_parent, task_id)

        # 3. Add worktree
        try:
            subprocess.check_call(["git", "worktree", "add", "-b", branch, sandbox_path], cwd=original_sandbox)
        except subprocess.CalledProcessError as e:
            return f"Error creating git worktree: {str(e)}"

        # 4. Update Agent Context
        agent_ctx.sandbox_dir = sandbox_path
        
        return f"Successfully entered sandbox mode. Isolated workspace created at: {sandbox_path}. All file operations and shell commands will now run in this directory."

class ExitWorktreeTool(BaseTool):
    name: str = "exit_sandbox_mode"
    description: str = (
        "退出沙箱模式：把变更合并回主分支（merge）或整体丢弃（discard），并恢复原工作目录。"
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
                            "enum": ["merge", "discard"],
                            "description": "merge=合并变更回原分支；discard=丢弃全部变更。"
                        }
                    },
                    "required": ["action"]
                }
            }
        }

    def execute(self, **kwargs) -> str:
        agent_ctx = kwargs.get("_agent_context")
        if not agent_ctx or not getattr(agent_ctx, "sandbox_dir", None):
            return "Error: Not currently in a sandbox."
            
        sandbox_path = agent_ctx.sandbox_dir
        original_sandbox = getattr(agent_ctx, "original_sandbox_dir", None)
        action = kwargs.get("action")
        
        if not original_sandbox:
            return "Error: Cannot find original workspace path."

        try:
            # Get current branch of the sandbox
            branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=sandbox_path).decode().strip()
            
            if action == "merge":
                # 1. Commit changes in sandbox if any
                subprocess.call(["git", "add", "."], cwd=sandbox_path)
                try:
                    subprocess.check_call(["git", "commit", "-m", "Sandbox changes automated merge"], cwd=sandbox_path)
                except:
                    pass # No changes to commit
                
                # 2. Merge back to original (assumed to be current branch of original_sandbox)
                subprocess.check_call(["git", "merge", branch], cwd=original_sandbox)
            
            # 3. Remove worktree
            subprocess.check_call(["git", "worktree", "remove", "--force", sandbox_path], cwd=original_sandbox)
            
            # 4. Delete branch
            subprocess.call(["git", "branch", "-D", branch], cwd=original_sandbox)
            
        except subprocess.CalledProcessError as e:
            return f"Error during sandbox exit: {str(e)}"
        finally:
            # 5. Restore original sandbox dir
            agent_ctx.sandbox_dir = original_sandbox
            
        return f"Successfully exited sandbox mode. Action '{action}' completed. Workspace restored to original directory."
