"""
Single source of truth for who can see and manage which MCP servers, and
for personal API token hashing/validation. Imported by the REST API
(routers.py), the MCP meta-tools (meta_tools.py), and the /mcp tool-list/
dispatch handlers (aggregator.py) -- the rule lives here exactly once.
"""

import hashlib
import secrets

from .config import ADMIN_USERS
from .database import (
    get_username_by_token_hash,
    list_servers,
    set_personal_token,
)
from .models import Server, ServerVisibility


def is_admin(username: str) -> bool:
    return username in ADMIN_USERS


def can_manage(server: Server, username: str) -> bool:
    return is_admin(username) or server.owner_username == username


def _is_visible(server: Server, username: str) -> bool:
    if is_admin(username):
        return True
    if server.visibility == ServerVisibility.EVERYONE.value:
        return True
    return server.owner_username == username


async def visible_servers(username: str) -> list[Server]:
    return [s for s in await list_servers() if _is_visible(s, username)]


async def visible_server_names(username: str) -> set[str]:
    return {s.name for s in await visible_servers(username)}


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def generate_personal_token(username: str) -> str:
    token = secrets.token_urlsafe(32)
    await set_personal_token(username, _hash_token(token))
    return token


async def validate_personal_token(token: str) -> str | None:
    return await get_username_by_token_hash(_hash_token(token))
