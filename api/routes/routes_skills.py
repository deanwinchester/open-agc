"""Skills management API endpoints."""
import os
from fastapi import APIRouter, HTTPException
from core.paths import get_data_path, get_skills_dir
from core.security import resolve_under
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


@router.post("/api/skills/install")
async def install_skill(data: dict):
    """Install a directory-style skill package from a GitHub URL / zip link."""
    url = data.get("url") or ""
    if not isinstance(url, str) or not url.strip():
        raise HTTPException(status_code=400, detail="url is required")
    import asyncio
    from core.skill_installer import install_skill_from_url, SkillInstallError
    try:
        # 下载/解压/copytree 是阻塞操作（最长 60s），移执行器线程，
        # 不在事件循环上跑（参照 routes_tasks.list_processes 的做法）。
        return await asyncio.get_running_loop().run_in_executor(
            None, install_skill_from_url, url.strip())
    except SkillInstallError as e:
        raise HTTPException(status_code=400, detail=str(e))


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
    try:
        resolve_under(get_skills_dir(), filename)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid filename: {filename}")
    result = manager.import_skill(filename, content, force=force)
    return result


@router.post("/api/skills/validate")
async def validate_skill(data: dict):
    """Validate a skill for security without importing."""
    from core.skill_manager import SkillManager
    manager = SkillManager()
    content = data.get("content", "")
    return manager.validate_skill(content)


@router.get("/api/skills/stats")
async def get_skill_stats():
    """Read-only skill usage statistics from skills/index.json (SkillStore usage tracking)."""
    import json
    index_path = os.path.join(get_skills_dir(), "index.json")
    if not os.path.exists(index_path):
        return {"skills": []}
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"skills": []}
    skills = [
        {
            "filename": s.get("filename", ""),
            "title": s.get("title", s.get("filename", "")),
            "usage_count": s.get("usage_count", 0),
            "success_rate": s.get("success_rate", 1.0),
            "last_used": s.get("last_used"),
        }
        for s in data.get("skills", [])
    ]
    skills.sort(key=lambda s: -s["usage_count"])
    return {"skills": skills}


@router.get("/api/skills/{filename}")
async def get_skill_content(filename: str):
    """Get the content of a specific skill (SKILL.md for directory skills)."""
    try:
        filepath = resolve_under(get_skills_dir(), filename)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid filename: {filename}")
    if os.path.isdir(filepath):
        filepath = os.path.join(filepath, "SKILL.md")
    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="Skill not found")
    with open(filepath, 'r', encoding='utf-8') as f:
        return {"filename": filename, "content": f.read()}


@router.delete("/api/skills/{filename}")
async def delete_skill(filename: str):
    """Delete a skill file."""
    from core.skill_manager import SkillManager
    try:
        resolve_under(get_skills_dir(), filename)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid filename: {filename}")
    manager = SkillManager()
    if manager.delete_skill(filename):
        try:
            from core.skill_store import SkillStore
            SkillStore().build_index()
        except Exception as e:
            print(f"[API] SkillStore index rebuild after deletion failed: {e}")
        return {"success": True, "message": f"Skill '{filename}' deleted."}
    raise HTTPException(status_code=404, detail="Skill not found")
