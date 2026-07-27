"""Secrets vault API endpoints.

Every endpoint returns the masked view only — the password field and its value
never leave the server. Plaintext is used server-internally at execution time
(tools substitute {{secret:name.field}} placeholders locally).
"""
import re
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class SecretUpsertRequest(BaseModel):
    name: str
    # All fields except name are optional: None (not provided) preserves the
    # existing value on update; an explicit value (including "") overwrites.
    type: Optional[str] = None
    host: Optional[str] = None
    port: Optional[str] = None
    database: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    note: Optional[str] = None


@router.get("/api/secrets")
async def get_secrets():
    """List all secrets (masked view — no passwords)."""
    from core.secrets import list_secrets
    return {"secrets": list_secrets()}


@router.get("/api/secrets/for-llm")
async def get_secrets_for_llm():
    """Masked secret list for the agent: names/fields usable as {{secret:name.field}}."""
    from core.secrets import list_secrets
    return {"secrets": list_secrets()}


@router.post("/api/secrets")
async def upsert_secret_endpoint(req: SecretUpsertRequest):
    """Create or update a secret. Response is the masked entry (no password)."""
    from core.secrets import upsert_secret
    if not _NAME_RE.match(req.name or ""):
        raise HTTPException(
            status_code=400,
            detail="Invalid secret name: only letters, digits, '_' and '-' allowed (^[A-Za-z0-9_-]+$)",
        )
    entry = upsert_secret(
        name=req.name, type=req.type, host=req.host, port=req.port,
        database=req.database, username=req.username, password=req.password, note=req.note,
    )
    return {"ok": True, "secret": entry}


@router.delete("/api/secrets/{name}")
async def delete_secret_endpoint(name: str):
    """Delete a secret by name."""
    from core.secrets import delete_secret
    if not delete_secret(name):
        raise HTTPException(status_code=404, detail=f"Secret not found: {name}")
    return {"ok": True}
