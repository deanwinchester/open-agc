from typing import Any, Dict
from tools.base import BaseTool

class SaveSkillTool(BaseTool):
    """
    保存或更新一个大模型学习到的技能。
    """
    name: str = "save_learned_skill"
    description: str = "保存或更新一个大模型学习到或整理出的复合技能流程（Markdown 格式），并落盘固化为长期技能。"

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
                            "description": "保存的文件名（例如：check_logs.md，强烈建议以 .md 结尾）",
                        },
                        "skill_content": {
                            "type": "string",
                            "description": "技能的 Markdown 格式全文。需包含明确的触发条件和分步实施指令。",
                        }
                    },
                    "required": ["skill_filename", "skill_content"],
                },
            },
        }

    def execute(self, skill_filename: str, skill_content: str) -> str:
        from core.skill_manager import SkillManager
        manager = SkillManager()
        
        # We enforce force=True here because the agent has verified user consent before calling this tool
        result = manager.import_skill(skill_filename, skill_content, force=True)
        
        if result["success"]:
            return f"技能 {skill_filename} 保存成功！已载入系统的技能图鉴中。"
        else:
            return f"技能保存失败：{result['message']}"
