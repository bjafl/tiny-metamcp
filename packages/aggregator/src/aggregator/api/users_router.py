"""
Admin-only user management API: list/toggle User accounts, and manage the
AllowedIdentity pre-approval list (the DB-backed replacement for
GITHUB_ALLOWED_USERS/STEAM_ALLOWED_USERS/ADMIN_USERS -- see
docs/superpowers/specs/2026-08-24-account-linking-admin-management-design.md).
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import database, identity_providers
from ..admin_auth import require_admin
from ..models import AllowedIdentity, User, UserIdentity

router = APIRouter(dependencies=[Depends(require_admin)])


def _identity_out(identity: UserIdentity) -> dict:
    return {
        "id": identity.id,
        "provider": identity.provider,
        "raw_id": identity.raw_id,
        "display_name": identity.display_name,
    }


async def _user_out(user: User) -> dict:
    identities = await database.list_user_identities(user.id)
    return {
        "id": user.id,
        "is_admin": user.is_admin,
        "allowed": user.allowed,
        "created_at": user.created_at,
        "identities": [_identity_out(i) for i in identities],
    }


@router.get("/users")
async def api_list_users():
    return [await _user_out(u) for u in await database.list_users()]


class UpdateUserRequest(BaseModel):
    is_admin: bool | None = None
    allowed: bool | None = None


@router.patch("/users/{user_id}")
async def api_update_user(
    user_id: int, req: UpdateUserRequest, current: str = Depends(require_admin)
):
    current_id = int(current.removeprefix("user:"))
    if user_id == current_id:
        if req.is_admin is False:
            raise HTTPException(status_code=400, detail="Cannot remove your own admin rights")
        if req.allowed is False:
            raise HTTPException(status_code=400, detail="Cannot disable your own account")
    updated = await database.update_user_flags(user_id, is_admin=req.is_admin, allowed=req.allowed)
    if updated is None:
        raise HTTPException(status_code=404, detail="User not found")
    return await _user_out(updated)


class AllowedIdentityRequest(BaseModel):
    provider: str
    raw_id: str
    grant_admin: bool = False


def _allowed_out(row: AllowedIdentity) -> dict:
    return {
        "id": row.id,
        "provider": row.provider,
        "raw_id": row.raw_id,
        "grant_admin": row.grant_admin,
    }


@router.get("/allowed-identities")
async def api_list_allowed_identities():
    return [_allowed_out(r) for r in await database.list_pending_allowed_identities()]


@router.post("/allowed-identities", status_code=201)
async def api_add_allowed_identity(req: AllowedIdentityRequest):
    if req.provider not in identity_providers.PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider {req.provider!r}")
    raw_id = req.raw_id.strip()
    if not raw_id:
        raise HTTPException(status_code=400, detail="raw_id must not be blank")
    existing = await database.get_allowed_identity(req.provider, raw_id)
    if existing is not None:
        raise HTTPException(status_code=400, detail="Already on the allow-list")
    row = await database.create_allowed_identity(req.provider, raw_id, req.grant_admin)
    return _allowed_out(row)


@router.delete("/allowed-identities/{allowed_id}")
async def api_delete_allowed_identity(allowed_id: int):
    await database.delete_allowed_identity(allowed_id)
    return {"deleted": allowed_id}
