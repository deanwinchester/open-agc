from typing import Any, Dict
from tools.base import BaseTool


class InstallSkillTool(BaseTool):
    """
    从 GitHub 安装标准技能包（目录式技能）。
    """
    name: str = "install_skill"
    description: str = (
        "从 GitHub 仓库链接（https://github.com/owner/repo）或 GitHub 官方 zip 直链安装标准技能包"
        "（含 SKILL.md 的目录式技能，可带 references/ 参考资料与 scripts/ 脚本）。"
        "仅支持 GitHub 主机（github.com / codeload.github.com / raw.githubusercontent.com）的 https 链接，"
        "不支持本地路径或其他主机。安装前会做危险内容校验。"
        "安装后技能立即可被检索注入，插件也可通过 context.skill_text/skill_dir 使用。"
        "用户要求安装技能、添加现成技能包时用。"
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
                        "url": {
                            "type": "string",
                            "description": "GitHub 仓库地址（https://github.com/owner/repo）或 GitHub 官方技能包 zip 直链。",
                        },
                    },
                    "required": ["url"],
                },
            },
        }

    def execute(self, url: str, **kwargs) -> str:
        from core.skill_installer import install_skill_from_url, SkillInstallError
        try:
            result = install_skill_from_url(url)
        except SkillInstallError as e:
            return f"技能安装失败：{e}"
        except Exception as e:
            return f"技能安装失败：{e}"
        return (
            f"✅ {result['message']}\n"
            f"  名称: {result['name']}\n"
            f"  简介: {result.get('title', '')}\n"
            f"技能已载入技能图鉴，后续任务检索命中时会自动注入；"
            f"插件可用 context.skill_text(\"{result['name']}\") 获取其内容。"
        )
