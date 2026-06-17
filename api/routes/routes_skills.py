"""Skills management API endpoints."""
import os
from fastapi import APIRouter, HTTPException
from core.paths import get_data_path, get_skills_dir
from api.config import load_config

router = APIRouter()


@router.get("/api/skills")
async def get_skills():
    """List available skills with details."""
    from core.skill_manager import SkillManager
    manager = SkillManager()
    skills = manager.list_skills()
    config = load_config()
    disabled = config.get("disabled_skills", [])
    for s in skills:
        s["enabled"] = s.get("filename", "") not in disabled
    return {"skills": skills}


@router.post("/api/skills/import")
async def import_skill(data: dict):
    """Import a skill file with security validation."""
    from core.skill_manager import SkillManager
    manager = SkillManager()
    filename = data.get("filename", "")
    content = data.get("content", "")
    force = data.get("force", False)
    if not filename or not content:
        raise HTTPException(status_code=400, detail="filename and content are required")
    result = manager.import_skill(filename, content, force=force)
    return result


@router.post("/api/skills/validate")
async def validate_skill(data: dict):
    """Validate a skill for security without importing."""
    from core.skill_manager import SkillManager
    manager = SkillManager()
    content = data.get("content", "")
    return manager.validate_skill(content)


@router.get("/api/skills/{filename}")
async def get_skill_content(filename: str):
    """Get the content of a specific skill."""
    filepath = os.path.join(get_skills_dir(), filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Skill not found")
    with open(filepath, 'r', encoding='utf-8') as f:
        return {"filename": filename, "content": f.read()}


@router.delete("/api/skills/{filename}")
async def delete_skill(filename: str):
    """Delete a skill file."""
    from core.skill_manager import SkillManager
    manager = SkillManager()
    if manager.delete_skill(filename):
        try:
            from core.skill_store import SkillStore
            SkillStore().build_index()
        except Exception as e:
            print(f"[API] SkillStore index rebuild after deletion failed: {e}")
        return {"success": True, "message": f"Skill '{filename}' deleted."}
    raise HTTPException(status_code=404, detail="Skill not found")
