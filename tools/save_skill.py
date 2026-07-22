from typing import Any, Dict
from tools.base import BaseTool

class SaveSkillTool(BaseTool):
    """
    保存或更新一个大模型学习到的技能。
    """
    name: str = "save_learned_skill"
    description: str = "把学会的可复用流程保存为长期技能（Markdown 格式）并落盘固化。用户要求记住做法或沉淀经验时用。"

    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill_filename": {
                            "type": "string",
                            "description": "保存的文件名，建议以 .md 结尾（如 check_logs.md）。",
                        },
                        "skill_content": {
                            "type": "string",
                            "description": "技能 Markdown 全文，须含明确的触发条件和分步指令。",
                        }
                    },
                    "required": ["skill_filename", "skill_content"],
                },
            },
        }

    def execute(self, skill_filename: str, skill_content: str, **kwargs) -> str:
        from core.skill_manager import SkillManager
        manager = SkillManager()
        
        # We enforce force=True here because the agent has verified user consent before calling this tool
        result = manager.import_skill(skill_filename, skill_content, force=True)
        
        if result["success"]:
            # Rebuild the skill index so progressive retrieval can find it
            try:
                from core.skill_store import SkillStore
                SkillStore().build_index()
            except Exception as e:
                print(f"[SaveSkill] Index rebuild failed: {e}")
            return f"技能 {skill_filename} 保存成功！已载入系统的技能图鉴中。"
        else:
            return f"技能保存失败：{result['message']}"
