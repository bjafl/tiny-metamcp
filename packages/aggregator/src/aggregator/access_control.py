"""
Single source of truth for who can see and manage which MCP servers, for
personal API token hashing/validation, and for whether a resolved provider
identity is allowed to log in and become (or reach) a User account -- see
resolve_login. Imported by the REST API (routers.py), the MCP meta-tools
(meta_tools.py), the /mcp tool-list/dispatch handlers (aggregator.py), and
the auth flows (admin_auth.py, oauth.py) -- the rule lives here exactly once.
"""

import hashlib
import secrets

from . import database, identity_providers
from .database import (
    get_username_by_token_hash,
    list_servers,
    set_personal_token,
)
from .models import Server, ServerVisibility, User


def _parse_user_id(username: str) -> int | None:
    prefix, sep, raw = username.partition(":")
    if prefix != "user" or not sep or not raw.isdigit():
        return None
    return int(raw)


async def _get_user(username: str) -> User | None:
    user_id = _parse_user_id(username)
    if user_id is None:
        return None
    return await database.get_user(user_id)


async def resolve_login(provider: str, raw_id: str, display_name: str) -> str | None:
    """Given a freshly-verified provider identity, return the canonical
    "user:<id>" session identity, or None if this person may not log in.
    Auto-provisions a new User on first login for identities covered by
    the allow-list (or when that provider's allow-list is empty --
    unrestricted, matching this project's pre-account-linking semantics)."""
    provider_impl = identity_providers.get_provider(provider)
    if provider_impl is None or not provider_impl.is_configured():
        return None

    identity = await database.get_user_identity(provider, raw_id)
    if identity is not None:
        user = await database.get_user(identity.user_id)
        if user is None or not user.allowed:
            return None
        await database.update_user_identity_display_name(identity.id, display_name)
        return f"user:{user.id}"

    allowed_row = await database.get_allowed_identity(provider, raw_id)
    provider_is_restricted = await database.has_any_allowed_identity(provider)
    if allowed_row is None and provider_is_restricted:
        return None

    new_user = await database.create_user(
        is_admin=allowed_row.grant_admin if allowed_row else False
    )
    await database.create_user_identity(new_user.id, provider, raw_id, display_name)
    if allowed_row is not None:
        await database.delete_allowed_identity(allowed_row.id)
    return f"user:{new_user.id}"


async def link_identity(
    current_username: str, provider: str, raw_id: str, display_name: str
) -> str:
    """Attach (provider, raw_id) to the account behind `current_username`
    ("user:<id>"). Self-service only -- called after the target provider
    has already verified the identity via its own login flow while the
    caller was already authenticated as current_username (see
    admin_auth.handle_link_callback). Returns "ok" on success (including
    an idempotent re-link of an identity already owned by this same
    account), "conflict" if the identity is already linked to a
    *different* account or this account already has a different identity
    for the same provider, or "invalid" if current_username isn't a real,
    allowed account."""
    user = await _get_user(current_username)
    if user is None or not user.allowed:
        return "invalid"

    existing = await database.get_user_identity(provider, raw_id)
    if existing is not None:
        if existing.user_id != user.id:
            return "conflict"
        await database.update_user_identity_display_name(existing.id, display_name)
        return "ok"

    identities = await database.list_user_identities(user.id)
    if any(i.provider == provider for i in identities):
        return "conflict"

    await database.create_user_identity(user.id, provider, raw_id, display_name)
    return "ok"


async def unlink_identity(current_username: str, identity_id: int) -> str:
    """Remove a linked identity from the account behind `current_username`.
    Returns "ok", "not_found" (the identity doesn't exist or belongs to a
    different account -- both look the same to the caller, deliberately,
    so this can't be used to probe which identities exist), or
    "last_identity" (refused -- would leave the account with zero ways to
    log in)."""
    user = await _get_user(current_username)
    if user is None:
        return "not_found"
    identities = await database.list_user_identities(user.id)
    target = next((i for i in identities if i.id == identity_id), None)
    if target is None:
        return "not_found"
    if len(identities) <= 1:
        return "last_identity"
    await database.delete_user_identity(identity_id)
    return "ok"


async def is_session_valid(username: str) -> bool:
    """True if `username` ("user:<id>") refers to a User that still exists
    and is allowed. Used to re-validate a standing session cookie or
    personal token on every request -- see the spec's note on why
    provider-configured-ness is no longer part of this ongoing check
    (docs/superpowers/specs/2026-08-24-account-linking-admin-management-design.md)."""
    user = await _get_user(username)
    return bool(user and user.allowed)


async def is_admin(username: str) -> bool:
    user = await _get_user(username)
    return bool(user and user.is_admin and user.allowed)


async def can_manage(server: Server, username: str) -> bool:
    if await is_admin(username):
        return True
    return server.owner_username == username


async def visible_servers(username: str) -> list[Server]:
    admin = await is_admin(username)

    def _visible(server: Server) -> bool:
        if admin:
            return True
        if server.visibility == ServerVisibility.EVERYONE.value:
            return True
        return server.owner_username == username

    return [s for s in await list_servers() if _visible(s)]


async def visible_server_names(username: str) -> set[str]:
    return {s.name for s in await visible_servers(username)}


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def generate_personal_token(username: str) -> str:
    token = secrets.token_urlsafe(32)
    await set_personal_token(username, _hash_token(token))
    return token


async def validate_personal_token(token: str) -> str | None:
    username = await get_username_by_token_hash(_hash_token(token))
    if username and not await is_session_valid(username):
        return None
    return username
