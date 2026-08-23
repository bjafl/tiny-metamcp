"""
Unit tests for the visibility/ownership/personal-token rules in
access_control.py -- the single module every other access-control
enforcement point (meta_tools, routers, /mcp) delegates to.
"""

from aggregator import access_control, identity_providers
from aggregator.database import add_server, delete_server
from aggregator.models import ServerType, ServerVisibility

ADMIN = "github:test-admin"  # must be in ADMIN_USERS, set by conftest.py
OWNER = "github:ac-owner"
STRANGER = "github:ac-stranger"


async def _cleanup(server_id: int) -> None:
    await delete_server(server_id)


def test_is_admin_true_only_for_admin_users_env():
    assert access_control.is_admin(ADMIN)
    assert not access_control.is_admin(OWNER)


async def test_can_manage_true_for_owner_and_admin_false_for_stranger():
    server = await add_server(
        "ac-can-manage",
        ServerType.PROXY,
        "http://x.invalid/mcp",
        owner_username=OWNER,
        visibility=ServerVisibility.PRIVATE.value,
    )
    try:
        assert access_control.can_manage(server, OWNER)
        assert access_control.can_manage(server, ADMIN)
        assert not access_control.can_manage(server, STRANGER)
    finally:
        await _cleanup(server.id)


async def test_visible_servers_private_only_to_owner_and_admin():
    server = await add_server(
        "ac-visible-private",
        ServerType.PROXY,
        "http://x.invalid/mcp",
        owner_username=OWNER,
        visibility=ServerVisibility.PRIVATE.value,
    )
    try:
        owner_names = {s.name for s in await access_control.visible_servers(OWNER)}
        admin_names = {s.name for s in await access_control.visible_servers(ADMIN)}
        stranger_names = {s.name for s in await access_control.visible_servers(STRANGER)}
        assert server.name in owner_names
        assert server.name in admin_names
        assert server.name not in stranger_names
    finally:
        await _cleanup(server.id)


async def test_visible_servers_everyone_visible_to_all():
    server = await add_server(
        "ac-visible-everyone",
        ServerType.PROXY,
        "http://x.invalid/mcp",
        owner_username=OWNER,
        visibility=ServerVisibility.EVERYONE.value,
    )
    try:
        stranger_names = {s.name for s in await access_control.visible_servers(STRANGER)}
        assert server.name in stranger_names
    finally:
        await _cleanup(server.id)


async def test_visible_server_names_matches_visible_servers():
    server = await add_server(
        "ac-visible-names",
        ServerType.PROXY,
        "http://x.invalid/mcp",
        owner_username=OWNER,
        visibility=ServerVisibility.EVERYONE.value,
    )
    try:
        names = await access_control.visible_server_names(STRANGER)
        assert server.name in names
    finally:
        await _cleanup(server.id)


async def test_generate_and_validate_personal_token_round_trip():
    token = await access_control.generate_personal_token("github:token-user")
    assert await access_control.validate_personal_token(token) == "github:token-user"


async def test_regenerating_token_invalidates_previous_one():
    old_token = await access_control.generate_personal_token("github:token-user-2")
    new_token = await access_control.generate_personal_token("github:token-user-2")
    assert await access_control.validate_personal_token(old_token) is None
    assert await access_control.validate_personal_token(new_token) == "github:token-user-2"


async def test_validate_personal_token_unknown_token_returns_none():
    assert await access_control.validate_personal_token("not-a-real-token") is None


async def test_validate_personal_token_rejects_deprovisioned_user(monkeypatch):
    """A personal token must stop working once its owner is removed from
    GITHUB_ALLOWED_USERS -- the token store alone must not keep a
    deprovisioned user's access alive (mirrors admin_auth.get_session_user's
    same allowlist check for the session-cookie path)."""
    monkeypatch.setattr(access_control, "GITHUB_ALLOWED_USERS", {ADMIN})

    deprovisioned_token = await access_control.generate_personal_token("github:token-user-3")
    assert await access_control.validate_personal_token(deprovisioned_token) is None

    admin_token = await access_control.generate_personal_token(f"github:{ADMIN}")
    assert await access_control.validate_personal_token(admin_token) == f"github:{ADMIN}"


def test_is_allowed_true_for_github_user_in_allowlist(monkeypatch):
    monkeypatch.setattr(access_control, "GITHUB_ALLOWED_USERS", {"octocat"})
    assert access_control.is_allowed("github:octocat")


def test_is_allowed_false_for_github_user_not_in_allowlist(monkeypatch):
    monkeypatch.setattr(access_control, "GITHUB_ALLOWED_USERS", {"octocat"})
    assert not access_control.is_allowed("github:someone-else")


def test_is_allowed_true_for_github_user_when_allowlist_empty(monkeypatch):
    monkeypatch.setattr(access_control, "GITHUB_ALLOWED_USERS", set())
    assert access_control.is_allowed("github:anyone")


def test_is_allowed_true_for_steam_user_in_allowlist(monkeypatch):
    monkeypatch.setattr(identity_providers, "STEAM_API_KEY", "test-steam-key")
    monkeypatch.setattr(access_control, "STEAM_ALLOWED_USERS", {"76561198012345678"})
    assert access_control.is_allowed("steam:76561198012345678")


def test_is_allowed_false_for_steam_user_not_in_allowlist(monkeypatch):
    monkeypatch.setattr(identity_providers, "STEAM_API_KEY", "test-steam-key")
    monkeypatch.setattr(access_control, "STEAM_ALLOWED_USERS", {"76561198012345678"})
    assert not access_control.is_allowed("steam:99999999999999999")


def test_is_allowed_false_when_steam_not_configured_even_if_allowlisted(monkeypatch):
    """Un-configuring Steam (no STEAM_API_KEY) must revoke access even for a
    user still present in STEAM_ALLOWED_USERS -- the allowlist alone must
    not be sufficient once the provider itself is disabled, otherwise
    already-issued Steam session cookies/tokens keep working after an
    operator disables Steam login."""
    monkeypatch.setattr(access_control, "STEAM_ALLOWED_USERS", {"76561198012345678"})
    assert identity_providers.STEAM_API_KEY == ""  # genuinely unconfigured, not another test's leak
    assert not access_control.is_allowed("steam:76561198012345678")


def test_is_allowed_false_for_unknown_provider_prefix():
    assert not access_control.is_allowed("discord:someone")


def test_is_allowed_false_for_unprefixed_username():
    assert not access_control.is_allowed("octocat")
