"""SearXNG search integration API endpoints."""
from fastapi import APIRouter, HTTPException

from core.searxng_manager import get_searxng_manager

router = APIRouter()


@router.get("/api/searxng/status")
async def get_searxng_status():
    sm = get_searxng_manager()
    return sm.get_status()


@router.post("/api/searxng/install")
async def install_searxng():
    sm = get_searxng_manager()
    ok = sm.install()
    if not ok:
        raise HTTPException(status_code=500, detail="Installation failed")
    return {"status": "ok", "message": "SearXNG installed"}


@router.post("/api/searxng/control")
async def control_searxng(body: dict):
    sm = get_searxng_manager()
    action = body.get("action", "")
    if action == "start":
        sm.start()
    elif action == "stop":
        sm.stop()
    else:
        raise HTTPException(status_code=400, detail="Invalid action")
    return {"status": "ok"}
